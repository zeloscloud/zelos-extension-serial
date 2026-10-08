"""One thread per port opens, reads, writes and closes its transport.

The one exception: stop(), on the caller's thread, closes a port that is blocked in a write.
"""

import concurrent.futures
import contextlib
import dataclasses
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Literal

from .clock import DeviceClock
from .config import PortConfig, Settings
from .discovery import PortNotFound, list_ports, present
from .emitter import Emitter
from .formats import (
    RepeatTracker,
    ShapeTracker,
    is_prompt,
    is_prompt_redraw,
    log_after_prompt,
    parse,
)
from .lines import Line, LineSplitter
from .transport import Fault, Transport, WriteTimeout, address, classify, open_transport

logger = logging.getLogger(__name__)

_US = 1_000
_MS = 1_000_000
_S = 1_000_000_000
_BACKOFF_MIN_NS = 500 * _MS
_BACKOFF_MAX_NS = 10 * _S
_QUIET_NS = 300 * _MS
_PRESENCE_NS = 5 * _S
_STALL_NS = 10 * _S
_LOG_EVERY_NS = 60 * _S
_ECHO_NS = 2 * _S
# Host time follows the wall clock only past this: NTP slews smaller differences.
_ANCHOR_NS = 1 * _S
_SENT = 8
_KEPT = 1000
_CLOCK_HEALTH = {
    "in use": "device clock in use",
    "not seen": "device clock not seen",
    "host": "host clock",
}

RequestKind = Literal["send", "command", "reset", "release", "acquire", "state", "sample"]

_Future = concurrent.futures.Future[dict[str, Any]]
_State = Literal["connecting", "open", "released", "failed", "stopped"]
# Why a request that needs the port cannot have it.
_NOT_OPEN: dict[_State, str] = {
    "connecting": "port is still connecting",
    "released": "port is released",
    "failed": "port failed to open; see Get State",
    "stopped": "port is stopped",
}
_NO_RESET_LINE = {
    "tcp": "a TCP port has no reset line",
    "rfc2217": "an RFC 2217 port has no reset line",
}


@dataclasses.dataclass
class _Command:
    text: str
    future: _Future
    start_ns: int
    deadline_ns: int
    reply: list[str] = dataclasses.field(default_factory=list)
    heard: bool = False


class _Throttle:
    """The first event is logged in full, then one a minute with a count of the rest."""

    def __init__(self) -> None:
        self._logged_ns: int | None = None
        self._held = 0

    def tally(self, now: int) -> int | None:
        """Count one event: 0 to log it in full, a count to log it with, or None to hold it."""
        if self._logged_ns is None:
            self._logged_ns = now
            return 0
        self._held += 1
        if now - self._logged_ns < _LOG_EVERY_NS:
            return None
        held, self._logged_ns, self._held = self._held, now, 0
        return held


class PortWorker(threading.Thread):
    """Owns one port; actions reach it through request()."""

    def __init__(
        self,
        cfg: PortConfig,
        emitter: Emitter,
        settings: Settings,
        open: Callable[[PortConfig], Transport] = open_transport,
        # Not monotonic_ns: on Windows, Python 3.11's ticks every 15.6 ms.
        now: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        # Daemon: a thread stuck in a driver call must not hold the process past the stop bound.
        super().__init__(name=f"serial-{cfg.name}", daemon=True)
        self._cfg = cfg
        self._emitter = emitter
        self._settings = settings
        self._open = open
        self._now = now
        # Host time is `now` from a wall clock reading, so the wall clock's jitter moves no line.
        self._wall_offset_ns = time.time_ns() - now()
        # Not `_stop`: threading.Thread uses that name internally.
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._requests: deque[tuple[RequestKind, dict[str, Any], _Future]] = deque()
        self._handlers: dict[RequestKind, Callable[..., dict[str, Any] | None]] = {
            "send": self._do_send,
            "command": self._do_command,
            "reset": self._do_reset,
            "release": self._do_release,
            "acquire": self._do_acquire,
            "state": self._do_state,
            "sample": self._do_sample,
        }

        self._splitter = LineSplitter(now=now)
        self._shape, self._repeat = ShapeTracker(), RepeatTracker()
        self._clock = DeviceClock()
        self._transport: Transport | None = None
        self._writing: Transport | None = None
        self._state: _State = "connecting"
        self._where = cfg.port or ("demo" if cfg.connection == "demo" else address(cfg))
        self._fault: Fault | None = None
        self._error = ""
        self._lost = False
        self._watched = False
        self._backoff_ns = _BACKOFF_MIN_NS
        self._retry_ns = 0
        self._open_log = _Throttle()
        self._error_log = _Throttle()
        self._presence_logged = False
        self._first = False
        # Whether a byte arrived since the port opened.
        self._heard = False
        self._rx_ns = self._checked_ns = 0
        self._last_ns = 0
        self._command: _Command | None = None
        self._acquiring: _Future | None = None
        self._acquire_by_ns = 0
        # How long the last open took: a host's addresses each wait out the connect timeout.
        self._open_ns = 0
        self._lines: deque[str] = deque(maxlen=_KEPT)
        self._chunks: deque[bytes] = deque(maxlen=_KEPT)
        self._sent: deque[tuple[str, int]] = deque(maxlen=_SENT)
        self._counters = dict.fromkeys(
            (
                "rx_bytes",
                "tx_bytes",
                "lines",
                "prompts",
                "echoes",
                "reconnects",
                "faults",
                "errors",
            ),
            0,
        )

    def request(self, kind: RequestKind, **args: Any) -> _Future:
        """Queue a request for the worker's thread; the Future carries the reply."""
        future: _Future = concurrent.futures.Future()
        # The lock pairs with the drain at exit, so no request is queued after it.
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("port is stopped")
            self._requests.append((kind, args, future))
        return future

    def stop(self) -> None:
        """Ask the thread to stop; join(1.0) then returns."""
        self._stopping.set()
        # The one call into the transport from another thread. The 1 s write timeout bounds a
        # write that flow control holds; closing the port ends it sooner where the driver allows.
        # A read ends within 100 ms on its own.
        writing = self._writing
        if writing is not None:
            with contextlib.suppress(Exception):
                writing.close()

    def run(self) -> None:
        """Runs until stop(); an unexpected error is logged and the port carries on."""
        buf = bytearray(4096)
        try:
            while not self._stopping.is_set():
                try:
                    data = self._read(buf)
                    self._anchor()
                    self._on_lines(self._splitter.feed(data, self._host_ns()))
                    self._on_lines(self._splitter.idle())
                    self._emitter.flush(self._cfg.name, self._host_ns())
                    self._serve()
                    self._advance_command()
                    self._advance_acquire()
                    self._check_presence()
                except Exception as e:
                    self._unexpected(e)
        finally:
            self._shut_down()

    def _host_ns(self) -> int:
        return self._now() + self._wall_offset_ns

    def _anchor(self) -> None:
        """Read the wall clock again when host time is over 1 s off it.

        `now` stops while the computer sleeps, so after a suspend host time jumps forward to the
        wall clock. A step back falls to 1 µs apart, so the lines keep their order.
        """
        wall = time.time_ns()
        if abs(wall - self._host_ns()) > _ANCHOR_NS:
            self._wall_offset_ns = wall - self._now()

    def _on_lines(self, lines: list[Line]) -> None:
        for line in lines:
            # One odd line must not cost the lines after it.
            try:
                self._on_line(line)
            except Exception as e:
                self._unexpected(e)

    def _unexpected(self, error: Exception) -> None:
        self._counters["errors"] += 1
        held = self._error_log.tally(self._now())
        name = self._cfg.name
        if held == 0:
            logger.exception("%s: unexpected error; the port carries on", name)
        elif held is not None:
            logger.error(
                "%s: %d more unexpected errors, the last: %r", name, held, error, exc_info=error
            )

    def _read(self, buf: bytearray) -> bytes:
        if self._state == "connecting" and self._now() >= self._retry_ns:
            self._connect()
        transport = self._transport
        if transport is None:
            self._stopping.wait(0.1)
            return b""
        try:
            # 100 ms bounds how late a stop is seen. The agent asks for one on Linux and macOS;
            # on Windows it terminates the job without asking.
            n = transport.readinto(buf, 0.1)
        except Exception:
            self._lose()
            return b""
        if not n:
            return b""
        data = bytes(buf[:n])
        self._rx_ns, self._heard = self._now(), True
        # Bytes, not the open, prove the link: a server that accepts and then closes backs off.
        self._backoff_ns = _BACKOFF_MIN_NS
        self._counters["rx_bytes"] += n
        self._chunks.append(data)
        return data

    def _connect(self) -> None:
        cfg, port = self._cfg, self._cfg.port
        start = self._now()
        # An open blocks the thread: one as slow as the last would settle an acquire only after
        # its caller stopped waiting, so it settles now with the reason so far.
        if self._acquiring is not None and start + self._open_ns > self._acquire_by_ns:
            self._settle_acquire(self._health())
        try:
            transport = self._open(cfg)
        except Exception as e:
            self._open_ns = self._now() - start
            fault = classify(e)
            if fault == "not_found":
                # Absence is not a failed open: look again soon, so a replugged device is open
                # in time for its boot output.
                self._retry_ns = self._now() + _BACKOFF_MIN_NS
                self._attempt_failed(fault, str(e))
                return
            if fault != "config":
                self._retry_later()
                self._attempt_failed(fault, str(e))
                return
            self._state, self._fault, self._error = "failed", fault, str(e)
            logger.error("%s: cannot open %s: %s", cfg.name, self._where, e)
            self._settle_acquire(self._health())
            return
        self._open_ns = self._now() - start
        # Judged gone later only if present now: Windows may not list a virtual port, such as
        # com0com.
        self._watched = port is not None and self._present(port)
        self._transport, self._where, self._state = transport, transport.where, "open"
        self._fault, self._open_log = None, _Throttle()
        if self._lost:
            self._counters["reconnects"] += 1
        self._lost = False
        self._first, self._heard = True, False
        self._rx_ns = self._checked_ns = self._now()
        verb = "acquired" if self._acquiring is not None else "connected to"
        self._note("info", f"{verb} {self._where}")
        self._settle_acquire(None)

    def _attempt_failed(self, fault: Fault, error: str) -> None:
        self._fault, self._error = fault, error
        held = self._open_log.tally(self._now())
        name, where = self._cfg.name, self._where
        if held == 0:
            logger.warning("%s: cannot open %s: %s", name, where, error)
        elif held is not None:
            logger.warning(
                "%s: %d more failed attempts to open %s, the last: %s", name, held, where, error
            )
        # Retrying cannot fix a permission; the other faults pass, as when a board re-enumerates
        # after a flash. Only when asked: health for a missing device lists every port.
        if self._acquiring is not None and fault == "permission":
            self._settle_acquire(self._health())

    def _lose(self) -> None:
        self._close()
        if self._stopping.is_set():
            return
        self._state, self._lost = "connecting", True
        self._retry_later()
        self._finish("aborted")
        self._note("warn", f"disconnected from {self._where}")

    def _retry_later(self) -> None:
        """Count a fault and schedule the next open after the backoff, which then doubles."""
        self._counters["faults"] += 1
        self._retry_ns = self._now() + self._backoff_ns
        self._backoff_ns = min(2 * self._backoff_ns, _BACKOFF_MAX_NS)

    def _present(self, port: str) -> bool:
        try:
            return present(port)
        except Exception as e:
            # A failed listing says nothing about the device; opening or reading it will.
            if not self._presence_logged:
                self._presence_logged = True
                logger.warning(
                    "%s: cannot check whether %s is connected (%s); assuming it is.",
                    self._cfg.name,
                    port,
                    e,
                )
            return True

    def _close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            # A port that is already gone can fail to close; it is closed either way.
            with contextlib.suppress(Exception):
                transport.close()
        # The line the device was printing as the port went, such as a crash, is logged now.
        self._on_lines(self._splitter.flush())
        self._splitter.reset()
        # Also runs on the way out, where an error must not skip failing the waiting requests.
        try:
            self._emitter.flush(self._cfg.name)
        except Exception as e:
            self._unexpected(e)

    def _on_line(self, line: Line) -> None:
        self._lines.append(line.text)
        # A piece of a long line is never a whole prompt or echo.
        if not line.cut:
            if self._prompt_or_echo(line):
                return
            if (log := log_after_prompt(line.text, self._cfg.prompt)) is not None:
                self._on_prompt(whole=True)
                line = dataclasses.replace(line, text=log)
        parsed = parse(line.text, line.colour, self._shape, self._repeat)
        first, self._first = self._first, False
        # Values only from whole lines: a pause or a cut can split `rail=13.64V` into `rail=13.6`,
        # and an unprefixed first line is probably the tail of one printed before the open.
        partial = line.partial or line.cut or (first and not parsed.prefixed)
        if parsed.values and (partial or not self._cfg.values):
            parsed = dataclasses.replace(parsed, values=())
        command = self._command
        if command is not None and not parsed.prefixed and parsed.rule != "zephyr-dropped":
            command.heard = True
            command.reply.append(line.text)
        t = line.host_ns
        if self._settings.time_source == "auto" and parsed.device_ns is not None:
            t, restarted = self._clock.map(t, parsed.device_ns)
            if restarted:
                self._note("warn", "device restarted", t)
        self._emitter.line(self._cfg.name, parsed, self._stamp(t))
        self._counters["lines"] += 1

    def _prompt_or_echo(self, line: Line) -> bool:
        """Count a prompt or the echo of a send; True when the line is one."""
        prompt = is_prompt(line.text, self._cfg.prompt)
        if prompt or is_prompt_redraw(line, self._cfg.prompt):
            self._on_prompt(whole=prompt)
            return True
        # The tx note already records what was sent; its echo would only repeat it.
        echoed = self._echo(line.text)
        if echoed is None:
            return False
        self._counters["echoes"] += 1
        if self._command is not None and echoed == self._command.text:
            self._command.heard = True
        return True

    def _on_prompt(self, whole: bool) -> None:
        self._counters["prompts"] += 1
        # A prompt erased before the echo arrives is the one that preceded the command.
        if whole and self._command is not None and self._command.heard:
            self._finish("prompt")

    def _echo(self, text: str) -> str | None:
        """The recently sent text this line echoes, alone or after a prompt; consumed."""
        now = self._now()
        while self._sent and now - self._sent[0][1] > _ECHO_NS:
            self._sent.popleft()
        for i, (sent, _) in enumerate(self._sent):
            before = text[: len(text) - len(sent)]
            if text.endswith(sent) and (not before or is_prompt(before, self._cfg.prompt)):
                # Consumed, so the device printing the same text again is logged.
                del self._sent[i]
                return sent
        return None

    def _stamp(self, t: int) -> int:
        """Strictly increasing per port, so lines from one read keep their console order.

        1 µs apart, not 1 ns: a trace reader's float seconds resolve only ~240 ns at today's epoch.
        """
        self._last_ns = max(t, self._last_ns + _US)
        return self._last_ns

    def _note(self, level: str, message: str, t: int | None = None, name: str = "serial") -> None:
        t = self._stamp(self._host_ns() if t is None else t)
        # Marked, so a Log panel that shows only the message does not show it as device output.
        mark = "> " if name == "tx" else f"[{name}] "
        self._emitter.note(self._cfg.name, level, mark + message, t, name=name)

    def _serve(self) -> None:
        while self._requests and not self._stopping.is_set():
            kind, args, future = self._requests.popleft()
            try:
                reply = self._handlers[kind](future, **args)
            except Exception as e:
                # A malformed request fails its caller, not the port.
                future.set_exception(e if isinstance(e, RuntimeError) else RuntimeError(str(e)))
            else:
                if reply is not None:
                    future.set_result(reply)

    def _advance_command(self) -> None:
        command = self._command
        if command is None:
            return
        now = self._now()
        # Quiet counts only after a reply line: the echo alone does not mean the device answered.
        if command.reply and now - self._rx_ns >= _QUIET_NS:
            self._finish("quiet")
        elif now >= command.deadline_ns:
            self._finish("timeout")

    def _finish(self, ended_by: str) -> None:
        command, self._command = self._command, None
        if command is not None:
            duration_ms = (self._now() - command.start_ns) // _MS
            command.future.set_result(
                {"reply": command.reply, "ended_by": ended_by, "duration_ms": duration_ms}
            )

    def _check_presence(self) -> None:
        port = self._cfg.port
        if self._transport is None or port is None or not self._watched:
            return
        now = self._now()
        if now - max(self._rx_ns, self._checked_ns) < _PRESENCE_NS:
            return
        self._checked_ns = now
        # Windows can leave a removed USB device's port open and silent instead of failing reads.
        if not self._present(port):
            self._lose()

    def _advance_acquire(self) -> None:
        if self._acquiring is not None and self._now() >= self._acquire_by_ns:
            self._settle_acquire(self._health())

    def _settle_acquire(self, error: str | None) -> None:
        future, self._acquiring = self._acquiring, None
        if future is None:
            return
        if error is None:
            future.set_result({"ok": True})
        else:
            future.set_exception(RuntimeError(error))

    def _shut_down(self) -> None:
        self._stopping.set()
        self._state = "stopped"
        self._finish("aborted")
        self._close()
        self._settle_acquire("port is stopped")
        with self._lock:
            while self._requests:
                self._requests.popleft()[2].set_exception(RuntimeError("port is stopped"))

    def _port(self) -> Transport:
        if self._transport is None:
            raise RuntimeError(_NOT_OPEN[self._state])
        return self._transport

    def _write(self, data: bytes, tx: Callable[[int], str]) -> int:
        """Write `data`; `tx(n)` names the first n bytes in the tx row that records them."""
        transport = self._port()
        self._writing = transport
        # stop() may have looked at _writing just before it was set.
        if self._stopping.is_set():
            self._writing = None
            raise RuntimeError("port is stopped")
        try:
            n = transport.write(data)
        except TimeoutError as e:
            # The port itself is fine. A long write at a low baud can time out part way.
            n = e.written if isinstance(e, WriteTimeout) else 0
            if not n:
                raise RuntimeError("write timed out; is flow control holding the line?") from None
            self._wrote(n, tx)
            raise RuntimeError(f"write timed out after {n} of {len(data)} bytes") from None
        except Exception as e:
            where = self._where
            self._lose()
            stopped = self._stopping.is_set()
            raise RuntimeError(
                "port is stopped" if stopped else f"disconnected from {where}"
            ) from e
        finally:
            self._writing = None
        self._wrote(n, tx)
        return n

    def _wrote(self, n: int, tx: Callable[[int], str]) -> None:
        self._counters["tx_bytes"] += n
        self._note("info", tx(n), name="tx")

    def _send_line(self, text: str) -> int:
        body = text.encode()
        n = self._write(
            body + self._cfg.line_ending,
            lambda sent: body[:sent].decode("utf-8", "replace") or "(line ending)",
        )
        self._sent.append((text, self._now()))
        return n

    def _do_send(self, future: _Future, text: str, hex: bool = False) -> dict[str, Any]:
        if not hex:
            return {"bytes_written": self._send_line(text)}
        try:
            data = bytes.fromhex(text)
        except ValueError:
            raise RuntimeError("hex must be pairs of hex digits, such as 0d0a") from None
        return {"bytes_written": self._write(data, lambda sent: f"hex: {data[:sent].hex()}")}

    def _do_command(self, future: _Future, text: str, timeout_s: float = 2.0) -> None:
        if self._command is not None:
            raise RuntimeError("command in progress")
        self._send_line(text)
        start = self._now()
        self._command = _Command(text, future, start, start + int(timeout_s * _S))

    def _do_reset(self, future: _Future) -> dict[str, Any]:
        line = self._cfg.reset_line
        if line == "none":
            raise RuntimeError(
                _NO_RESET_LINE.get(
                    self._cfg.connection, "this port has no reset line; set Reset line in Advanced"
                )
            )
        transport = self._port()
        idle = self._cfg.dtr if line == "dtr" else self._cfg.rts

        def drive(level: bool) -> None:
            transport.set_lines(level if line == "dtr" else None, level if line == "rts" else None)

        self._note("info", f"reset pulse on {line}")
        drive(not idle)
        self._stopping.wait(0.1)
        drive(idle)
        return {"ok": True}

    def _do_release(self, future: _Future) -> dict[str, Any]:
        if self._state not in ("open", "connecting"):
            raise RuntimeError(_NOT_OPEN[self._state])
        self._close()
        self._finish("aborted")
        self._settle_acquire("port is released")
        self._state, self._fault, self._lost = "released", None, False
        self._note("info", f"released {self._where}")
        return {"ok": True}

    def _do_acquire(self, future: _Future, wait_s: float) -> None:
        """Open the port again; `wait_s` later, the caller gets the reason it is not open."""
        if self._state != "released":
            raise RuntimeError("port is not released")
        self._state, self._acquiring = "connecting", future
        self._retry_ns, self._backoff_ns = self._now(), _BACKOFF_MIN_NS
        self._acquire_by_ns = self._retry_ns + int(wait_s * _S)

    def _do_state(self, future: _Future) -> dict[str, Any]:
        name = self._cfg.name
        return {
            "state": self._state,
            "health": self._health(),
            "where": self._where,
            "clock": self._clock_state(),
            "counters": {
                **self._counters,
                "long_lines": self._splitter.long_lines,
                **self._emitter.counters(name),
            },
            "signals": [dataclasses.asdict(s) for s in self._emitter.signals(name)],
        }

    def _do_sample(self, future: _Future, lines: int = 50, hex: bool = False) -> dict[str, Any]:
        if hex:
            return {"chunks": [chunk.hex() for chunk in list(self._chunks)[-lines:]]}
        return {"lines": list(self._lines)[-lines:]}

    def _health(self) -> str:
        where, fault = self._where, self._fault
        if self._state == "failed":
            return f"Unsupported setting: {self._error}. Change it in the config."
        if fault == "permission":
            if sys.platform.startswith("linux"):
                return (
                    f"Permission denied on {where}. Add the agent's user to the dialout group "
                    "(uucp on Arch): sudo usermod -aG dialout $USER, then log in again."
                )
            return f"Permission denied on {where}. Check the device's permissions."
        if fault in ("busy", "in_use_or_denied"):
            return (
                f"{where} is in use by another program, or access was denied. Close the other "
                "program. If another Zelos agent holds it, run Release Port there."
            )
        if fault == "not_found":
            port = self._cfg.port or where
            try:
                ports = list_ports()
            except Exception:
                # The health line must not fail with the listing it reports on.
                ports = []
            # Listed again: the next open, within 0.5 s, should find it.
            if any(p.named_by(port) for p in ports):
                return f"Connecting to {where}."
            return str(PortNotFound(port, [p.choice for p in ports]))
        if fault == "unresolved":
            return f"Cannot resolve {self._cfg.host}. Check the host name."
        if fault == "no_rfc2217":
            return (
                f"{where} accepted the connection but does not answer RFC 2217. "
                "Is it a raw TCP port? Use tcp instead."
            )
        if fault == "refused":
            return f"Cannot connect to {where}. Check that the server is running."
        if fault == "other":
            return f"Cannot open {where}: {self._error}."
        if self._state == "released":
            return "Released. Run Acquire Port to take the port back."
        if self._state == "connecting":
            if self._lost:
                return f"Disconnected from {where}. Waiting for it to come back."
            return f"Connecting to {where}."
        # Measured: an ST-LINK V3 bridge at a wrong baud delivers no bytes at all.
        if not self._heard and self._now() - self._rx_ns >= _STALL_NS:
            return (
                "Connected. No bytes yet. Check the baud rate and the wiring, and close any "
                "program that opened the port first."
            )
        # Text that only a line end could release: an idle release or a prompt proves the baud.
        pending = self._splitter.pending_since
        if pending is not None and self._now() - pending >= _STALL_NS:
            return (
                "Bytes arrive but no line ends. Check the baud rate, and run Sample with hex to "
                "see the raw bytes."
            )
        counters = self._emitter.counters(self._cfg.name)
        lines = self._counters["lines"]
        if self._cfg.values and lines >= 300 and not counters["value_lines"]:
            return f"No values found in {lines} lines. Run Sample to see the lines."
        return (
            f"Connected. {lines} lines, {counters['signals']} signals, "
            f"{_CLOCK_HEALTH[self._clock_state()]}."
        )

    def _clock_state(self) -> Literal["in use", "not seen", "host"]:
        if self._settings.time_source == "host":
            return "host"
        return "in use" if self._clock.in_use else "not seen"
