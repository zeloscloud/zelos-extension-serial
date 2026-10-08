"""The port worker: requests, reconnects, health, time and commands, against fakes."""

import concurrent.futures
import errno
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from types import SimpleNamespace
from typing import Any, get_args

import pytest
import serialx

from tests.conftest import FakeTransport, listed_port, port_config, wait_until
from zelos_extension_serial import discovery
from zelos_extension_serial import worker as worker_module
from zelos_extension_serial.config import PortConfig, Settings
from zelos_extension_serial.discovery import PortInfo, PortNotFound
from zelos_extension_serial.emitter import Emitter, SignalInfo
from zelos_extension_serial.formats import Parsed
from zelos_extension_serial.transport import Transport, WriteTimeout, open_transport
from zelos_extension_serial.worker import PortWorker, RequestKind

US = 1_000
MS = 1_000_000
S = 1_000_000_000
PROMPT = b"\x1b[1;32muart:~$ \x1b[m"
ERASE = b"\x1b[8D\x1b[J"
SETTINGS = Settings(ports=(), prefix="Serial", time_source="auto", log_level="INFO")


def zephyr(seconds: float, message: str = "dcdc: rail=13.65V") -> bytes:
    ms = round(seconds * 1000)
    return f"[00:00:{ms // 1000:02d}.{ms % 1000:03d},000] <inf> {message}\r\n".encode()


class Clock:
    """Monotonic time the test moves."""

    def __init__(self) -> None:
        self.ns = 0

    def __call__(self) -> int:
        return self.ns


class VirtualStop(threading.Event):
    """A stop flag whose waits move the clock instead of blocking."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits.append(timeout)
        self.clock.ns += round(timeout * S)
        return self.is_set()


class RecordingEmitter(Emitter):
    """Records what the worker writes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any, int]] = []
        self.value_lines = 0
        self.signal_list: list[SignalInfo] = []
        self.flushes: list[int | None] = []

    def line(self, port: str, parsed: Parsed, time_ns: int) -> None:
        self.events.append(("line", parsed, time_ns))

    def note(self, port: str, level: str, message: str, time_ns: int, name: str = "serial") -> None:
        self.events.append(("note", (level, message, name), time_ns))

    def flush(self, port: str, now_ns: int | None = None) -> None:
        self.flushes.append(now_ns)

    def signals(self, port: str) -> list[SignalInfo]:
        return self.signal_list

    def counters(self, port: str) -> dict[str, int]:
        return {"signals": len(self.signal_list), "late_names": 0, "value_lines": self.value_lines}

    def lines(self) -> list[Parsed]:
        return [parsed for kind, parsed, _ in list(self.events) if kind == "line"]

    def messages(self) -> list[str]:
        return [parsed.message for parsed in self.lines()]

    def notes(self) -> list[tuple[str, str, str]]:
        return [note for kind, note, _ in list(self.events) if kind == "note"]

    def times(self) -> list[int]:
        return [t for _, _, t in list(self.events)]


class Port(FakeTransport):
    """A fake port that answers writes, and can fail reads or writes."""

    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        replies: dict[bytes, list[bytes]] | None = None,
        read_error: BaseException | None = None,
        write_error: BaseException | None = None,
    ) -> None:
        super().__init__(chunks)
        self.replies = replies or {}
        self.read_error = read_error
        self.write_error = write_error

    def readinto(self, buf: bytearray, timeout: float) -> int:
        if not self.chunks and self.read_error is not None:
            raise self.read_error
        return super().readinto(buf, timeout)

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        n = super().write(data)
        self.chunks.extend(self.replies.get(data, []))
        return n


class Rig:
    """A worker on a test clock with a scripted `open`: each call takes the next item."""

    def __init__(
        self,
        *script: Transport | BaseException,
        cfg: PortConfig | None = None,
        settings: Settings = SETTINGS,
        virtual: bool = False,
        on_open: Callable[["Rig"], None] | None = None,
    ) -> None:
        self.clock = Clock()
        self.emitter = RecordingEmitter()
        self.script = list(script)
        self.opened: list[int] = []
        self.on_open = on_open
        self.worker = PortWorker(
            cfg or port_config(), self.emitter, settings, open=self._open, now=self.clock
        )
        self.stop_flag: VirtualStop | None = None
        if virtual:
            self.stop_flag = VirtualStop(self.clock)
            self.worker._stopping = self.stop_flag

    def _open(self, cfg: PortConfig) -> Transport:
        self.opened.append(self.clock.ns)
        if self.on_open:
            self.on_open(self)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def request(self, kind: Any, **args: Any) -> concurrent.futures.Future[dict[str, Any]]:
        if kind == "acquire":
            args.setdefault("wait_s", 14.0)
        return self.worker.request(kind, **args)

    def ask(self, kind: Any, **args: Any) -> dict[str, Any]:
        return self.request(kind, **args).result(timeout=2)

    def fails(self, kind: Any, **args: Any) -> str:
        with pytest.raises(RuntimeError) as raised:
            self.ask(kind, **args)
        return str(raised.value)

    def settle(self) -> None:
        """Return after one whole loop iteration that began after this call."""
        self.ask("state")
        self.ask("state")

    def health(self) -> str:
        return self.ask("state")["health"]


@pytest.fixture
def rigs() -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def make(*script: Transport | BaseException, start: bool = True, **kw: Any) -> Rig:
        rig = Rig(*script, **kw)
        made.append(rig)
        if start:
            rig.worker.start()
        return rig

    yield make
    for rig in made:
        rig.worker.stop()
    for rig in made:
        rig.worker.join(1.0)


class Held:
    """Holds the worker in its first open, so requests queued meanwhile are served in one loop."""

    def __init__(self) -> None:
        self.opening, self.go = threading.Event(), threading.Event()

    def __call__(self, rig: Rig) -> None:
        self.opening.set()
        assert self.go.wait(2)

    def serve(self, rig: Rig, *kinds: Any) -> list[concurrent.futures.Future[dict[str, Any]]]:
        assert self.opening.wait(2)
        futures = [rig.request(kind) for kind in kinds]
        self.go.set()
        return futures


@pytest.fixture(autouse=True)
def no_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "list_ports", list)


# Lines


class Wall:
    """time.time_ns(): `ns` plus `clock`, so it runs with a rig's clock once given it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, ns: int = 50 * S) -> None:
        self.ns = ns
        self.clock = Clock()
        monkeypatch.setattr(time, "time_ns", lambda: self.ns + self.clock.ns)


def test_lines_of_one_tick_are_logged_1_us_apart_in_console_order(rigs, monkeypatch) -> None:
    Wall(monkeypatch)
    rig = rigs(Port([b"one\r\ntwo\r\nthree\r\n"]))
    wait_until(lambda: len(rig.emitter.messages()) == 3)
    assert rig.emitter.messages() == ["one", "two", "three"]
    assert rig.emitter.times() == [50 * S, 50 * S + US, 50 * S + 2 * US, 50 * S + 3 * US]


def wall_rig(rigs, monkeypatch) -> tuple[Rig, Wall, Port]:
    """A rig whose wall clock runs with its clock, after a first line at 50 s."""
    wall = Wall(monkeypatch)
    port = Port([b"one\r\n"])
    rig = rigs(port)
    wall.clock = rig.clock
    wait_until(lambda: len(rig.emitter.lines()) == 1)
    return rig, wall, port


def second_line_time(rig: Rig, port: Port) -> int:
    rig.settle()
    port.chunks.append(b"two\r\n")
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    return rig.emitter.times()[-1]


def test_host_time_keeps_to_its_own_clock_through_wall_clock_drift_under_1_s(
    rigs, monkeypatch
) -> None:
    rig, wall, port = wall_rig(rigs, monkeypatch)
    wall.ns += 900 * MS
    rig.clock.ns += 1 * S
    assert second_line_time(rig, port) == 51 * S


def test_host_time_follows_the_wall_clock_past_a_suspend(rigs, monkeypatch) -> None:
    rig, wall, port = wall_rig(rigs, monkeypatch)
    # Asleep for 60 s: the wall clock moves on and the monotonic clock does not.
    wall.ns += 60 * S
    assert second_line_time(rig, port) == 110 * S


def test_a_wall_clock_step_back_over_1_s_keeps_the_order_1_us_apart(rigs, monkeypatch) -> None:
    rig, wall, port = wall_rig(rigs, monkeypatch)
    # The wall clock is the trace's time, so the next line follows it as far as order allows.
    wall.ns -= 3600 * S
    rig.clock.ns += 1 * S
    assert second_line_time(rig, port) == 50 * S + 2 * US


def test_prompts_and_redraws_are_counted_not_logged(rigs) -> None:
    chunks = [
        PROMPT + ERASE + zephyr(1.0),
        PROMPT + b"dcdc li" + ERASE + zephyr(1.02),
        b"# ",
    ]
    rig = rigs(Port(chunks))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    rig.clock.ns += 100 * MS
    wait_until(lambda: rig.ask("state")["counters"]["prompts"] == 3)
    assert rig.emitter.messages() == ["dcdc: rail=13.65V", "dcdc: rail=13.65V"]
    assert rig.ask("sample", lines=5)["lines"] == [
        "uart:~$ ",
        "[00:00:01.000,000] <inf> dcdc: rail=13.65V",
        "uart:~$ dcdc li",
        "[00:00:01.020,000] <inf> dcdc: rail=13.65V",
        "# ",
    ]


def test_a_custom_prompt_replaces_the_default(rigs) -> None:
    rig = rigs(Port([b"# \r\nok> \r\n"]), cfg=port_config(prompt=re.compile(r"ok> ")))
    wait_until(lambda: rig.ask("state")["counters"]["prompts"] == 1)
    assert rig.emitter.messages() == ["# "]


@pytest.mark.parametrize(
    ("line", "rule", "module", "device_ns", "message", "values"),
    [
        (
            PROMPT + zephyr(19.024, "pump: rpm=1042 temp=23.5C"),
            "zephyr+kv",
            "pump",
            19_024 * MS,
            "pump: rpm=1042 temp=23.5C",
            ["rpm", "temp"],
        ),
        (
            b"# [   12.345678] eth0: link up\r\n",
            "linux",
            "kernel",
            12_345_678 * US,
            "eth0: link up",
            [],
        ),
    ],
    ids=["zephyr", "linux"],
)
def test_a_prompt_run_into_a_log_line_is_counted_and_the_log_line_logged_alone(
    rigs, line: bytes, rule: str, module: str, device_ns: int, message: str, values: list[str]
) -> None:
    rig = rigs(Port([BANNER, line]))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    parsed = rig.emitter.lines()[1]
    # Values with a module go to the module's event, not to the port's `values` event.
    assert (parsed.level, parsed.rule, parsed.module) == ("info", rule, module)
    assert (parsed.device_ns, parsed.message) == (device_ns, message)
    assert [v.name for v in parsed.values] == values
    state = rig.ask("state")
    assert state["counters"]["prompts"] == 1
    assert state["counters"]["lines"] == 2
    assert rig.ask("sample", lines=1)["lines"][0].endswith(message)


@pytest.mark.parametrize(
    "release",
    [
        lambda rig: setattr(rig.clock, "ns", rig.clock.ns + 100 * MS),
        lambda rig: rig.ask("release"),
    ],
    ids=["pause", "close"],
)
def test_a_partial_prompt_run_into_a_log_line_logs_the_log_line_without_values(
    rigs, release: Callable[[Rig], object]
) -> None:
    pending = PROMPT + b"[00:00:19.024,000] <inf> pump: rpm=10"
    rig = rigs(Port([BANNER, pending]))
    wait_until(lambda: rig.ask("state")["counters"]["rx_bytes"] == len(BANNER + pending))
    release(rig)
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    parsed = rig.emitter.lines()[1]
    assert (parsed.level, parsed.module, parsed.device_ns) == ("info", "pump", 19_024 * MS)
    assert (parsed.message, parsed.values) == ("pump: rpm=10", ())
    assert rig.ask("state")["counters"]["prompts"] == 1


@pytest.mark.parametrize("text", ["$ ls -la", "# comment", ">>> 1+1"])
def test_a_prompt_and_text_that_is_no_log_line_is_logged_whole(rigs, text: str) -> None:
    rig = rigs(Port([BANNER, text.encode() + b"\r\n"]))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert rig.emitter.lines()[1].message == text
    assert rig.ask("state")["counters"]["prompts"] == 0


def test_first_line_after_each_connect_is_logged_without_values_when_unprefixed(rigs) -> None:
    first = Port([b"=13.65V in=4.3A\r\n", b"a=1\r\n"], read_error=OSError(errno.EIO, "gone"))
    second = Port([b"b=2\r\n", b"c=3\r\n"])
    rig = rigs(first, second)
    wait_until(lambda: ("warn", "[serial] disconnected from fake", "serial") in rig.emitter.notes())
    rig.clock.ns += 500 * MS
    wait_until(lambda: "c=3" in rig.emitter.messages())
    logged = [(p.message, [v.name for v in p.values]) for p in rig.emitter.lines()]
    assert logged == [("=13.65V in=4.3A", []), ("a=1", ["a"]), ("b=2", []), ("c=3", ["c"])]


def test_first_line_keeps_its_values_when_prefixed(rigs) -> None:
    rig = rigs(Port([zephyr(0.02)]))
    wait_until(lambda: len(rig.emitter.lines()) == 1)
    assert [v.name for v in rig.emitter.lines()[0].values] == ["rail"]


ZEPHYR_HALF = b"[00:00:02.000,000] <inf> dcdc: rail=13.6"


def test_a_line_split_by_a_pause_logs_its_first_half_without_values(rigs) -> None:
    port = Port([BANNER, ZEPHYR_HALF])
    rig = rigs(port)
    wait_until(lambda: rig.ask("state")["counters"]["rx_bytes"] == len(BANNER + ZEPHYR_HALF))
    rig.clock.ns += 100 * MS
    wait_until(lambda: "dcdc: rail=13.6" in rig.emitter.messages())
    assert rig.emitter.lines()[1].values == ()


def test_cut_pieces_record_no_values(rigs) -> None:
    rig = rigs(Port([BANNER, b"a=1 " * 1100 + b"\r\n"]))
    wait_until(lambda: len(rig.emitter.lines()) == 3)
    assert [len(p.values) for p in rig.emitter.lines()] == [0, 0, 0]


@pytest.mark.parametrize(
    "lost",
    [
        lambda rig, port: setattr(port, "read_error", OSError(errno.EIO, "gone")),
        lambda rig, port: rig.ask("release"),
        lambda rig, port: rig.worker.stop(),
    ],
    ids=["disconnect", "release", "stop"],
)
def test_a_line_pending_when_the_port_closes_is_logged_without_values(
    rigs, lost: Callable[[Rig, Port], object]
) -> None:
    port = Port([BANNER, ZEPHYR_HALF])
    rig = rigs(port, ConnectionRefusedError())
    wait_until(lambda: rig.ask("state")["counters"]["rx_bytes"] == len(BANNER + ZEPHYR_HALF))
    lost(rig, port)
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert rig.emitter.lines()[1].message == "dcdc: rail=13.6"
    assert rig.emitter.lines()[1].values == ()
    assert None in rig.emitter.flushes


def test_values_off_keeps_the_line_and_drops_its_values(rigs) -> None:
    rig = rigs(Port([b"a=1\r\n", b"b=2 c=3\r\n"]), cfg=port_config(values=False))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert [p.values for p in rig.emitter.lines()] == [(), ()]


def test_values_on_keeps_values(rigs) -> None:
    rig = rigs(Port([b"*** banner ***\r\n", b"b=2 c=3\r\n"]))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert [v.name for v in rig.emitter.lines()[1].values] == ["b", "c"]


class TimedPort(FakeTransport):
    """Sets the worker's clock before each chunk; with a wall clock from 0, host time is it."""

    def __init__(self, timed: list[tuple[int, bytes]]) -> None:
        super().__init__()
        self.timed = timed
        self.clock = Clock()

    def readinto(self, buf: bytearray, timeout: float) -> int:
        if self.timed:
            self.clock.ns, chunk = self.timed.pop(0)
            self.chunks.append(chunk)
        return super().readinto(buf, timeout)


def timed_rig(rigs, monkeypatch, timed: list[tuple[int, bytes]], **kw: Any) -> Rig:
    wall = Wall(monkeypatch, 0)
    port = TimedPort(timed)
    rig = rigs(port, start=False, **kw)
    port.clock = wall.clock = rig.clock
    rig.worker.start()
    return rig


def test_device_time_is_mapped_and_a_restart_noted(rigs, monkeypatch) -> None:
    chunks = [
        (100_000 * MS, zephyr(1.0)),
        (100_050 * MS, zephyr(1.01)),
        (100_100 * MS, zephyr(0.5)),
        (100_150 * MS, zephyr(0.52)),
    ]
    rig = timed_rig(rigs, monkeypatch, chunks)
    wait_until(lambda: len(rig.emitter.lines()) == 4)
    events = [(kind, t) for kind, _, t in rig.emitter.events]
    # The restart is known at the second line after it; the first keeps host time.
    assert events[1:] == [
        ("line", 100_000 * MS),
        ("line", 100_010 * MS),
        ("line", 100_100 * MS),
        ("note", 100_150 * MS),
        ("line", 100_150 * MS + US),
    ]
    assert rig.emitter.notes()[1] == ("warn", "[serial] device restarted", "serial")
    assert rig.ask("state")["clock"] == "in use"


def test_host_time_source_ignores_device_time(rigs, monkeypatch) -> None:
    chunks = [(100_000 * MS, zephyr(1.0)), (100_050 * MS, zephyr(1.01))]
    host = Settings(ports=(), prefix="Serial", time_source="host", log_level="INFO")
    rig = timed_rig(rigs, monkeypatch, chunks, settings=host)
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert rig.emitter.times()[1:] == [100_000 * MS, 100_050 * MS]
    assert rig.ask("state")["clock"] == "host"


# Requests


def test_send_text_appends_the_line_ending(rigs) -> None:
    port = Port()
    rig = rigs(port, cfg=port_config(line_ending=b"\r\n"))
    assert rig.ask("send", text="hello") == {"bytes_written": 7}
    assert port.writes == [b"hello\r\n"]
    assert rig.emitter.notes()[-1] == ("info", "> hello", "tx")
    assert rig.ask("state")["counters"]["tx_bytes"] == 7


def test_send_hex_writes_the_bytes_alone(rigs) -> None:
    port = Port()
    rig = rigs(port)
    assert rig.ask("send", text="0a0d", hex=True) == {"bytes_written": 2}
    assert port.writes == [b"\n\r"]
    assert rig.emitter.notes()[-1] == ("info", "> hex: 0a0d", "tx")
    assert rig.fails("send", text="zz", hex=True) == "hex must be pairs of hex digits, such as 0d0a"


BANNER = b"*** Booting Zephyr OS build dccb09599635 ***\r\n"


@pytest.mark.parametrize("echo", [b"dcdc limit set 2.0\r\n", b"uart:~$ dcdc limit set 2.0\r\n"])
def test_the_echo_of_a_send_is_not_logged(rigs, echo: bytes) -> None:
    port = Port([BANNER], replies={b"dcdc limit set 2.0\n": [echo + b"limit set to 2.0 A\r\n"]})
    rig = rigs(port)
    rig.ask("send", text="dcdc limit set 2.0")
    wait_until(lambda: "limit set to 2.0 A" in rig.emitter.messages())
    assert rig.emitter.messages() == [BANNER.decode().strip(), "limit set to 2.0 A"]
    assert rig.emitter.notes()[-1] == ("info", "> dcdc limit set 2.0", "tx")
    assert rig.ask("state")["counters"]["echoes"] == 1


def test_an_echo_is_consumed_once(rigs) -> None:
    rig = rigs(Port([BANNER], replies={b"ls\n": [b"ls\r\nls\r\n"]}))
    rig.ask("send", text="ls")
    wait_until(lambda: "ls" in rig.emitter.messages())
    rig.settle()
    assert rig.emitter.messages().count("ls") == 1


def test_a_second_echo_of_a_command_joins_its_reply(rigs) -> None:
    rig = rigs(Port(replies={b"ls\n": [b"ls\r\nls\r\n# " + ERASE]}))
    assert rig.ask("command", text="ls")["reply"] == ["ls"]


@pytest.mark.parametrize(("after_ns", "logged"), [(2 * S, False), (2 * S + 1, True)])
def test_a_sent_text_is_an_echo_for_2_s(rigs, after_ns: int, logged: bool) -> None:
    port = Port([BANNER])
    rig = rigs(port)
    rig.ask("send", text="hello")
    rig.clock.ns += after_ns
    port.chunks.append(b"hello\r\n")
    wait_until(lambda: not port.chunks)
    rig.settle()
    assert ("hello" in rig.emitter.messages()) is logged


def test_prompt_and_text_never_sent_is_logged(rigs) -> None:
    rig = rigs(Port([BANNER], replies={b"reboot\n": [b"uart:~$ uptime\r\n"]}))
    rig.ask("send", text="reboot")
    wait_until(lambda: "uart:~$ uptime" in rig.emitter.messages())
    assert rig.ask("state")["counters"]["echoes"] == 0


def test_hex_sends_have_no_echo(rigs) -> None:
    rig = rigs(Port([BANNER], replies={b"hi": [b"6869\r\nhi\r\n"]}))
    rig.ask("send", text="6869", hex=True)
    wait_until(lambda: "hi" in rig.emitter.messages())
    assert "6869" in rig.emitter.messages()


def test_only_the_last_8_sends_are_remembered(rigs) -> None:
    port = Port([BANNER])
    rig = rigs(port)
    for i in range(1, 10):
        rig.ask("send", text=f"t{i}")
    port.chunks.append(b"t1\r\nt2\r\n")
    wait_until(lambda: "t1" in rig.emitter.messages())
    rig.settle()
    assert rig.emitter.messages() == [BANNER.decode().strip(), "t1"]


def test_requests_need_an_open_port(rigs) -> None:
    rig = rigs(ConnectionRefusedError(), cfg=port_config(reset_line="dtr"))
    for kind in ("send", "command"):
        assert rig.fails(kind, text="x") == "port is still connecting"
    assert rig.fails("reset") == "port is still connecting"


def test_write_timeout_fails_the_request_and_keeps_the_port(rigs) -> None:
    port = Port(write_error=TimeoutError())
    rig = rigs(port)
    assert rig.fails("send", text="x") == "write timed out; is flow control holding the line?"
    assert rig.ask("state")["state"] == "open"
    assert not port.closed
    port.write_error = None
    assert rig.ask("send", text="x") == {"bytes_written": 2}


@pytest.mark.parametrize(
    ("args", "written", "error", "tx"),
    [
        ({"text": "hello"}, 4, "write timed out after 4 of 6 bytes", "> hell"),
        ({"text": "0a0d0e", "hex": True}, 1, "write timed out after 1 of 3 bytes", "> hex: 0a"),
    ],
)
def test_a_write_timeout_after_some_bytes_records_what_left(
    rigs, args: dict[str, Any], written: int, error: str, tx: str
) -> None:
    rig = rigs(Port(write_error=WriteTimeout(written)))
    assert rig.fails("send", **args) == error
    assert rig.emitter.notes()[-1] == ("info", tx, "tx")
    assert rig.ask("state")["counters"]["tx_bytes"] == written


def test_an_empty_send_is_logged_as_the_line_ending(rigs) -> None:
    port = Port()
    rig = rigs(port)
    assert rig.ask("send", text="") == {"bytes_written": 1}
    assert port.writes == [b"\n"]
    assert rig.emitter.notes()[-1] == ("info", "> (line ending)", "tx")


def test_write_error_closes_the_port_and_reconnects(rigs) -> None:
    port = Port(write_error=OSError(errno.EIO, "gone"))
    rig = rigs(port, ConnectionRefusedError())
    assert rig.fails("send", text="x") == "disconnected from fake"
    assert port.closed
    assert rig.ask("state")["state"] == "connecting"
    assert ("warn", "[serial] disconnected from fake", "serial") in rig.emitter.notes()


class LineTimes(Port):
    """Records when each line change happened."""

    def __init__(self) -> None:
        super().__init__()
        self.times: list[float] = []

    def set_lines(self, dtr: bool | None, rts: bool | None) -> None:
        self.times.append(time.perf_counter())
        super().set_lines(dtr, rts)


@pytest.mark.parametrize(
    ("reset_line", "dtr", "rts", "pulse"),
    [
        ("dtr", False, True, [(True, None), (False, None)]),
        ("rts", True, False, [(None, True), (None, False)]),
    ],
)
def test_reset_drives_the_line_away_from_idle_for_100_ms(
    rigs, reset_line: str, dtr: bool, rts: bool, pulse: list[tuple[bool | None, bool | None]]
) -> None:
    port = LineTimes()
    rig = rigs(port, cfg=port_config(reset_line=reset_line, dtr=dtr, rts=rts))
    assert rig.ask("reset") == {"ok": True}
    assert port.lines == pulse
    # Not 0.1: a Windows timed wait can end one 15.6 ms timer tick early.
    assert port.times[1] - port.times[0] >= 0.08
    assert rig.emitter.notes()[-1] == ("info", f"[serial] reset pulse on {reset_line}", "serial")


@pytest.mark.parametrize(
    ("connection", "error"),
    [
        ("serial", "this port has no reset line; set Reset line in Advanced"),
        ("tcp", "a TCP port has no reset line"),
        ("rfc2217", "an RFC 2217 port has no reset line"),
    ],
)
def test_reset_without_a_reset_line(rigs, connection: str, error: str) -> None:
    rig = rigs(Port(), cfg=port_config(connection=connection, reset_line="none"))
    assert rig.fails("reset") == error


def test_release_and_acquire(rigs) -> None:
    first, second = Port(), Port()
    rig = rigs(first, second)
    assert rig.fails("acquire") == "port is not released"
    assert rig.ask("release") == {"ok": True}
    assert first.closed
    assert rig.ask("state")["state"] == "released"
    assert rig.health() == "Released. Run Acquire Port to take the port back."
    assert rig.fails("release") == "port is released"
    assert rig.fails("send", text="x") == "port is released"
    assert rig.ask("acquire") == {"ok": True}
    assert rig.ask("state")["state"] == "open"
    assert len(rig.opened) == 2
    assert rig.emitter.notes()[-2:] == [
        ("info", "[serial] released fake", "serial"),
        ("info", "[serial] acquired fake", "serial"),
    ]


@pytest.mark.parametrize(
    ("error", "reply", "state"),
    [
        (
            PermissionError(errno.EACCES, "denied"),
            "Permission denied on fake. Add the agent's user to the dialout group (uucp on "
            "Arch): sudo usermod -aG dialout $USER, then log in again.",
            "connecting",
        ),
        (ValueError("baud 7"), "Unsupported setting: baud 7. Change it in the config.", "failed"),
    ],
)
def test_acquire_fails_at_once_on_a_fault_that_retrying_cannot_fix(
    rigs, monkeypatch, error: BaseException, reply: str, state: str
) -> None:
    monkeypatch.setattr(worker_module, "sys", SimpleNamespace(platform="linux"))
    rig = rigs(Port(), error)
    rig.ask("release")
    assert rig.fails("acquire") == reply
    assert rig.ask("state")["state"] == state


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(errno.ENOENT, "No such file or directory"),
        OSError(errno.EBUSY, "locked"),
        ConnectionRefusedError("refused"),
    ],
)
def test_acquire_waits_through_failed_opens_for_a_device_coming_back(
    rigs, error: BaseException
) -> None:
    rig = rigs(Port(), error, error, Port())
    rig.ask("release")
    acquire = rig.request("acquire")
    wait_until(lambda: len(rig.opened) == 2)
    rig.clock.ns += 500 * MS
    wait_until(lambda: len(rig.opened) == 3)
    rig.settle()
    assert not acquire.done()
    rig.clock.ns += 1 * S
    assert acquire.result(timeout=2) == {"ok": True}
    assert rig.emitter.notes()[-1] == ("info", "[serial] acquired fake", "serial")


def test_acquire_gives_the_last_reason_when_its_wait_ends(rigs) -> None:
    rig = rigs(Port(), ConnectionRefusedError("refused"))
    rig.ask("release")
    acquire = rig.request("acquire", wait_s=3.0)
    wait_until(lambda: len(rig.opened) == 2)
    rig.clock.ns = 3 * S - 1
    rig.settle()
    assert not acquire.done()
    rig.clock.ns = 3 * S
    with pytest.raises(RuntimeError, match="^Cannot connect to fake. Check that the server is"):
        acquire.result(timeout=2)
    assert rig.ask("state")["state"] == "connecting"


def test_acquire_settles_before_an_open_as_slow_as_the_last_would_end_past_its_wait(
    rigs,
) -> None:
    """A host whose three addresses each take the 2 s connect timeout: every open takes 6 s."""

    def slow(rig: Rig) -> None:
        if len(rig.opened) > 1:
            rig.clock.ns += 6 * S

    rig = rigs(Port(), ConnectionRefusedError("timed out"), virtual=True, on_open=slow)
    rig.ask("release")
    settled: list[int] = []
    acquire = rig.request("acquire")
    acquire.add_done_callback(lambda _: settled.append(rig.clock.ns))
    with pytest.raises(RuntimeError, match="^Cannot connect to fake. Check that the server is"):
        acquire.result(timeout=2)
    # Opens at 0 and 6.5 s; one more from 13.5 s would end at 19.5 s.
    assert [t - rig.opened[1] for t in rig.opened[1:3]] == [0, 6500 * MS]
    assert settled[0] - rig.opened[1] == 13500 * MS


def test_acquire_opens_at_once_with_the_backoff_reset(rigs) -> None:
    rig = rigs(ConnectionRefusedError())
    # The backoff grows to 4 s.
    for opens, t in ((1, 0), (2, 500 * MS), (3, 1500 * MS)):
        rig.clock.ns = t
        wait_until(lambda opens=opens: len(rig.opened) == opens)
    rig.ask("release")
    rig.request("acquire")
    wait_until(lambda: len(rig.opened) == 4)
    rig.clock.ns += 500 * MS
    wait_until(lambda: len(rig.opened) == 5)
    assert rig.opened == [0, 500 * MS, 1500 * MS, 1500 * MS, 2000 * MS]


def test_release_fails_a_pending_acquire(rigs) -> None:
    held = Held()
    rig = rigs(Port(), on_open=held)
    _, acquire, state, _ = held.serve(rig, "release", "acquire", "state", "release")
    assert state.result(timeout=2)["health"] == "Connecting to fake."
    with pytest.raises(RuntimeError, match="^port is released$"):
        acquire.result(timeout=2)


def test_release_and_acquire_after_a_loss_is_not_a_reconnect(rigs) -> None:
    rig = rigs(Port(read_error=OSError(errno.EIO, "gone")), Port())
    wait_until(lambda: ("warn", "[serial] disconnected from fake", "serial") in rig.emitter.notes())
    rig.ask("release")
    rig.ask("acquire")
    assert rig.ask("state")["counters"]["reconnects"] == 0


def test_release_while_connecting(rigs) -> None:
    rig = rigs(ConnectionRefusedError())
    assert rig.ask("release") == {"ok": True}
    assert rig.ask("state")["state"] == "released"


def test_state_reply(rigs) -> None:
    rig = rigs(Port([b"x" * 5000 + b"\r\n"]))
    rig.emitter.signal_list.append(SignalInfo("dut/dcdc", "rail", "V", "zephyr+kv", "rail=1V"))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    state = rig.ask("state")
    assert state["state"] == "open"
    assert state["where"] == "fake"
    assert state["clock"] == "not seen"
    assert state["counters"] == {
        "rx_bytes": 5002,
        "tx_bytes": 0,
        "lines": 2,
        "prompts": 0,
        "echoes": 0,
        "reconnects": 0,
        "faults": 0,
        "errors": 0,
        "long_lines": 1,
        "signals": 1,
        "late_names": 0,
        "value_lines": 0,
    }
    assert state["signals"] == [
        {
            "event": "dut/dcdc",
            "field": "rail",
            "unit": "V",
            "rule": "zephyr+kv",
            "example": "rail=1V",
        }
    ]


def test_sample_lines_and_hex_chunks(rigs) -> None:
    rig = rigs(Port([b"a\r\n", b"b\r\n", b"c\r\n"]))
    wait_until(lambda: len(rig.emitter.lines()) == 3)
    assert rig.ask("sample", lines=2) == {"lines": ["b", "c"]}
    assert rig.ask("sample", lines=50) == {"lines": ["a", "b", "c"]}
    assert rig.ask("sample", lines=2, hex=True) == {"chunks": ["620d0a", "630d0a"]}


def test_sample_asked_for_more_than_is_held_returns_all(rigs) -> None:
    rig = rigs(Port([f"{i}\r\n".encode() for i in range(31)]))
    wait_until(lambda: len(rig.emitter.lines()) == 31)
    assert len(rig.ask("sample", lines=50)["lines"]) == 31
    assert len(rig.ask("sample", lines=50, hex=True)["chunks"]) == 31


def test_request_on_a_stopped_worker_raises_at_once(rigs) -> None:
    rig = rigs(Port())
    rig.worker.stop()
    with pytest.raises(RuntimeError, match="port is stopped"):
        rig.worker.request("state")


def test_stop_fails_a_pending_acquire(rigs, monkeypatch) -> None:
    held = Held()
    rig = rigs(Port(), on_open=held)
    counters = rig.emitter.counters

    def stop_then_count(port: str) -> dict[str, int]:
        rig.worker.stop()
        return counters(port)

    monkeypatch.setattr(rig.emitter, "counters", stop_then_count)
    _, acquire, _ = held.serve(rig, "release", "acquire", "state")
    with pytest.raises(RuntimeError, match="^port is stopped$"):
        acquire.result(timeout=2)


def test_an_error_flushing_at_stop_still_fails_a_pending_acquire(rigs, monkeypatch) -> None:
    held = Held()
    rig = rigs(Port(), on_open=held)
    counters = rig.emitter.counters

    def stop_then_count(port: str) -> dict[str, int]:
        rig.worker.stop()
        return counters(port)

    def flush(port: str, now_ns: int | None = None) -> None:
        if now_ns is None:
            raise ValueError("odd flush")

    monkeypatch.setattr(rig.emitter, "counters", stop_then_count)
    monkeypatch.setattr(rig.emitter, "flush", flush)
    _, acquire, _ = held.serve(rig, "release", "acquire", "state")
    with pytest.raises(RuntimeError, match="^port is stopped$"):
        acquire.result(timeout=2)


def test_a_send_served_as_the_worker_stops_writes_nothing(rigs) -> None:
    class StopsWhenEncoded(str):
        """Stops the worker after the send is served and before its write."""

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            rig.worker.stop()
            return str(self).encode(encoding, errors)

    port = Port()
    rig = rigs(port)
    assert rig.fails("send", text=StopsWhenEncoded("x")) == "port is stopped"
    assert port.writes == []


def test_every_request_kind_has_a_handler() -> None:
    worker = PortWorker(port_config(), RecordingEmitter(), SETTINGS)
    assert set(worker._handlers) == set(get_args(RequestKind))


# Commands


def zephyr_shell(command: bytes, *reply: bytes) -> Port:
    """Echo after the prompt, then the reply, then a log line erasing the next prompt."""
    after = command.rstrip(b"\n") + b"\r\n" + b"".join(reply) + PROMPT + ERASE
    return Port([PROMPT], replies={command: [after + zephyr(1.0)]})


def test_command_ends_at_the_prompt(rigs) -> None:
    rig = rigs(zephyr_shell(b"dcdc limit get\n", b"limit: 4.5 A\r\n"))
    reply = rig.ask("command", text="dcdc limit get")
    assert reply == {"reply": ["limit: 4.5 A"], "ended_by": "prompt", "duration_ms": 0}
    assert rig.emitter.notes()[-1] == ("info", "> dcdc limit get", "tx")
    assert "limit: 4.5 A" in rig.emitter.messages()


def test_command_ends_at_a_prompt_run_into_a_log_line(rigs) -> None:
    after = b"ver\r\nzephyr v3.7.0\r\n" + PROMPT + zephyr(19.024, "pump: rpm=1042 temp=23.5C")
    rig = rigs(Port([PROMPT], replies={b"ver\n": [after]}))
    reply = rig.ask("command", text="ver")
    assert reply == {"reply": ["zephyr v3.7.0"], "ended_by": "prompt", "duration_ms": 0}
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert rig.emitter.lines()[1].module == "pump"
    assert rig.ask("state")["counters"]["echoes"] == 1


def test_command_ignores_the_prompt_erased_before_its_echo(rigs) -> None:
    after = ERASE + zephyr(1.0) + PROMPT + b"dcdc limit get\r\nlimit: 4.5 A\r\n" + PROMPT + ERASE
    port = Port([PROMPT], replies={b"dcdc limit get\n": [after + zephyr(1.02)]})
    rig = rigs(port)
    rig.settle()
    reply = rig.ask("command", text="dcdc limit get")
    assert reply["reply"] == ["limit: 4.5 A"]
    assert reply["ended_by"] == "prompt"


@pytest.mark.parametrize(
    ("echo", "reply"),
    [
        (b"dcdc limit get\r\n", []),
        (b"uart:~$ dcdc limit get\r\n", []),
        (b"# dcdc limit get\r\n", []),
        (b"unknown: dcdc limit get\r\n", ["unknown: dcdc limit get"]),
    ],
)
def test_command_removes_the_echo(rigs, echo: bytes, reply: list[str]) -> None:
    port = Port(replies={b"dcdc limit get\n": [echo + b"# " + ERASE]})
    rig = rigs(port)
    assert rig.ask("command", text="dcdc limit get")["reply"] == reply
    assert rig.emitter.messages() == reply
    assert rig.ask("state")["counters"]["echoes"] == 1 - len(reply)


def test_command_reply_excludes_log_lines_and_the_dropped_notice(rigs) -> None:
    reply = [zephyr(1.0), b"--- 3 messages dropped ---\r\n", b"limit: 4.5 A\r\n"]
    rig = rigs(zephyr_shell(b"dcdc limit get\n", *reply))
    assert rig.ask("command", text="dcdc limit get")["reply"] == ["limit: 4.5 A"]
    assert "--- 3 messages dropped ---" in rig.emitter.messages()


def test_command_ends_300_ms_after_its_reply_goes_quiet(rigs) -> None:
    port = Port(replies={b"ls\n": [b"ls\r\n"]})
    rig = rigs(port)
    future = rig.worker.request("command", text="ls")
    wait_until(lambda: rig.ask("state")["counters"]["echoes"] == 1)
    rig.clock.ns += 1 * S
    rig.settle()
    assert not future.done()
    port.chunks.append(b"a.txt\r\n")
    wait_until(lambda: "a.txt" in rig.emitter.messages())
    rig.clock.ns += 299 * MS
    rig.settle()
    assert not future.done()
    rig.clock.ns += 1 * MS
    reply = {"reply": ["a.txt"], "ended_by": "quiet", "duration_ms": 1300}
    assert future.result(timeout=2) == reply


def test_command_times_out(rigs) -> None:
    rig = rigs(Port())
    future = rig.worker.request("command", text="x", timeout_s=0.5)
    rig.settle()
    rig.clock.ns += 499 * MS
    rig.settle()
    assert not future.done()
    rig.clock.ns += 1 * MS
    assert future.result(timeout=2) == {"reply": [], "ended_by": "timeout", "duration_ms": 500}


def test_release_aborts_a_command(rigs) -> None:
    rig = rigs(Port(replies={b"ls\n": [b"ls\r\nline\r\n"]}))
    future = rig.worker.request("command", text="ls")
    wait_until(lambda: "line" in rig.emitter.messages())
    rig.ask("release")
    assert future.result(timeout=2)["ended_by"] == "aborted"
    assert future.result()["reply"] == ["line"]


def test_losing_the_port_aborts_a_command(rigs) -> None:
    port = Port(replies={b"ls\n": [b"ls\r\nline\r\n"]})
    rig = rigs(port, ConnectionRefusedError())
    future = rig.worker.request("command", text="ls")
    wait_until(lambda: "line" in rig.emitter.messages())
    port.read_error = OSError(errno.EIO, "gone")
    assert future.result(timeout=2) == {"reply": ["line"], "ended_by": "aborted", "duration_ms": 0}


def test_one_command_at_a_time_while_send_still_works(rigs) -> None:
    rig = rigs(Port())
    first = rig.worker.request("command", text="slow")
    assert rig.fails("command", text="other") == "command in progress"
    assert rig.ask("send", text="y") == {"bytes_written": 2}
    assert not first.done()


# Connection and health


def test_failed_opens_back_off_doubling_to_10_s(rigs) -> None:
    def stop_after_eight(rig: Rig) -> None:
        if len(rig.opened) == 8:
            rig.worker.stop()

    rig = rigs(ConnectionRefusedError(), virtual=True, on_open=stop_after_eight)
    rig.worker.join(2)
    assert [t / S for t in rig.opened] == [0, 0.5, 1.5, 3.5, 7.5, 15.5, 25.5, 35.5]
    assert rig.stop_flag is not None
    assert set(rig.stop_flag.waits) == {0.1}


def test_read_error_closes_notes_and_reconnects_with_backoff(rigs) -> None:
    lost = Port(read_error=OSError(errno.EIO, "gone"))

    def stop_after_four(rig: Rig) -> None:
        if len(rig.opened) == 4:
            rig.worker.stop()

    refused = ConnectionRefusedError()
    rig = rigs(refused, lost, refused, virtual=True, on_open=stop_after_four)
    rig.worker.join(2)
    assert lost.closed
    assert [t / S for t in rig.opened] == [0, 0.5, 1.5, 3.5]
    assert rig.emitter.notes() == [
        ("info", "[serial] connected to fake", "serial"),
        ("warn", "[serial] disconnected from fake", "serial"),
    ]


@pytest.mark.parametrize(
    ("byte", "opened"), [(b"", [0, 0.5, 1.5, 3.5, 7.5]), (b"x", [0, 0.5, 1.0, 1.5, 2.0])]
)
def test_backoff_resets_on_the_first_byte_not_on_the_open(
    rigs, byte: bytes, opened: list[float]
) -> None:
    port = Port(read_error=OSError(errno.EIO, "gone"))

    def open_then_close(rig: Rig) -> None:
        port.chunks.append(byte)
        if len(rig.opened) == 5:
            rig.worker.stop()

    rig = rigs(port, virtual=True, on_open=open_then_close)
    rig.worker.join(2)
    assert [t / S for t in rig.opened] == opened


def test_reconnect_is_counted(rigs) -> None:
    lost = Port(read_error=OSError(errno.EIO, "gone"))
    rig = rigs(lost, Port())
    wait_until(lambda: ("warn", "[serial] disconnected from fake", "serial") in rig.emitter.notes())
    assert rig.health() == "Disconnected from fake. Waiting for it to come back."
    rig.clock.ns += 500 * MS
    wait_until(lambda: rig.ask("state")["state"] == "open")
    counters = rig.ask("state")["counters"]
    assert (counters["reconnects"], counters["faults"]) == (1, 1)


def test_open_failures_log_once_then_a_count_per_minute(rigs, caplog) -> None:
    def stop_after(rig: Rig) -> None:
        if len(rig.opened) == 12:
            rig.worker.stop()

    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        rig = rigs(ConnectionRefusedError("refused"), virtual=True, on_open=stop_after)
        rig.worker.join(2)
    assert [r.getMessage() for r in caplog.records] == [
        "dut: cannot open bench:2000: refused",
        "dut: 10 more failed attempts to open bench:2000, the last: refused",
    ]


def test_a_good_open_logs_the_next_failed_open_in_full(rigs, caplog) -> None:
    def stop_after_three(rig: Rig) -> None:
        if len(rig.opened) == 3:
            rig.worker.stop()

    refused, lost = ConnectionRefusedError("refused"), Port(read_error=OSError(errno.EIO, "gone"))
    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        rig = rigs(refused, lost, refused, virtual=True, on_open=stop_after_three)
        rig.worker.join(2)
    assert [r.getMessage() for r in caplog.records] == [
        "dut: cannot open bench:2000: refused",
        "dut: cannot open fake: refused",
    ]


def test_config_fault_fails_without_retry(rigs) -> None:
    rig = rigs(ValueError("baud 7 is not supported"))
    rig.settle()
    rig.clock.ns += 60 * S
    rig.settle()
    assert len(rig.opened) == 1
    assert rig.ask("state")["state"] == "failed"
    assert rig.health() == "Unsupported setting: baud 7 is not supported. Change it in the config."
    assert rig.fails("send", text="x") == "port failed to open; see Get State"
    assert rig.fails("release") == "port failed to open; see Get State"
    assert rig.fails("acquire") == "port is not released"


FTDI = "usb:0403:6001:A50285BI"


def serial_cfg(port: str = "/dev/ttyFAKE") -> PortConfig:
    return port_config(connection="serial", port=port, host=None, tcp_port=None)


@pytest.mark.parametrize(
    ("port", "error"),
    [
        ("/dev/ttyFAKE", FileNotFoundError(errno.ENOENT, "No such file or directory")),
        (FTDI, PortNotFound(FTDI, [])),
    ],
)
def test_an_absent_device_is_opened_again_every_half_second(
    rigs, port: str, error: BaseException
) -> None:
    def stop_at_12_5_s(rig: Rig) -> None:
        if rig.clock.ns == 12_500 * MS:
            rig.worker.stop()

    rig = rigs(error, cfg=serial_cfg(port), virtual=True, on_open=stop_at_12_5_s)
    rig.worker.join(2)
    assert rig.opened == [i * 500 * MS for i in range(26)]


@pytest.mark.parametrize(("listed", "state"), [("COM7", "connecting"), ("COM3", "open")])
def test_a_windows_path_is_judged_gone_only_if_listed_at_the_open(
    rigs, monkeypatch, listing: list[serialx.SerialPortInfo], listed: str, state: str
) -> None:
    # Virtual ports, such as com0com, may not be listed.
    monkeypatch.setattr(discovery, "_WINDOWS", True)
    listing.append(listed_port(listed))
    rig = rigs(Port(), cfg=serial_cfg("COM7"))
    wait_until(lambda: rig.ask("state")["state"] == "open")
    listing.clear()
    rig.clock.ns += 5 * S
    rig.settle()
    assert rig.ask("state")["state"] == state


def test_silent_port_checks_presence_every_5_s(rigs, monkeypatch) -> None:
    calls: list[int] = []
    present = {"/dev/ttyFAKE": True}

    def check(port: str) -> bool:
        calls.append(rig.clock.ns)
        return present[port]

    monkeypatch.setattr(worker_module, "present", check)
    rig = rigs(Port(), cfg=serial_cfg(), start=False)
    rig.worker.start()
    wait_until(lambda: rig.ask("state")["state"] == "open")
    rig.clock.ns = 4999 * MS
    rig.settle()
    assert calls == [0]
    rig.clock.ns = 5 * S
    rig.settle()
    rig.settle()
    assert calls == [0, 5 * S]
    present["/dev/ttyFAKE"] = False
    rig.clock.ns = 10 * S
    wait_until(lambda: rig.ask("state")["state"] == "connecting")
    assert ("warn", "[serial] disconnected from fake", "serial") in rig.emitter.notes()


def test_tcp_port_never_checks_presence(rigs, monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "present", lambda port: pytest.fail("checked"))
    rig = rigs(Port())
    rig.clock.ns += 6 * S
    rig.settle()
    assert rig.ask("state")["state"] == "open"


@pytest.mark.parametrize(
    ("error", "platform", "health"),
    [
        (
            PermissionError(errno.EACCES, "denied"),
            "linux",
            "Permission denied on bench:2000. Add the agent's user to the dialout group (uucp on "
            "Arch): sudo usermod -aG dialout $USER, then log in again.",
        ),
        (
            PermissionError(errno.EACCES, "denied"),
            "darwin",
            "Permission denied on bench:2000. Check the device's permissions.",
        ),
        (
            OSError(errno.EBUSY, "locked"),
            "linux",
            "bench:2000 is in use by another program, or access was denied. "
            "Close the other program. If another Zelos agent holds it, run Release Port there.",
        ),
        (
            PortNotFound("usb:0403:6001:A50285BI", []),
            "linux",
            "No device matches bench:2000. Connected: none.",
        ),
        (
            ConnectionRefusedError(),
            "linux",
            "Cannot connect to bench:2000. Check that the server is running.",
        ),
        (RuntimeError("odd"), "linux", "Cannot open bench:2000: odd."),
    ],
)
def test_health_of_a_failed_open(
    rigs, monkeypatch, error: BaseException, platform: str, health: str
) -> None:
    monkeypatch.setattr(worker_module, "sys", SimpleNamespace(platform=platform))
    rig = rigs(error)
    assert rig.health() == health


@pytest.mark.parametrize(
    ("fault", "health"),
    [
        ("unresolved", "Cannot resolve bench. Check the host name."),
        (
            "no_rfc2217",
            "bench:2000 accepted the connection but does not answer RFC 2217. "
            "Is it a raw TCP port? Use tcp instead.",
        ),
    ],
)
def test_health_of_a_network_fault_that_is_retried(
    rigs, monkeypatch, fault: str, health: str
) -> None:
    monkeypatch.setattr(worker_module, "classify", lambda e: fault)
    rig = rigs(OSError("any"))
    assert rig.health() == health


def test_health_lists_connected_devices_for_a_missing_id(rigs, monkeypatch) -> None:
    listed = [PortInfo("/dev/ttyS0", "/dev/ttyS0", None, None, None, None, None)]
    monkeypatch.setattr(worker_module, "list_ports", lambda: listed)
    rig = rigs(PortNotFound(FTDI, []), cfg=serial_cfg(FTDI))
    assert rig.health() == f"No device matches {FTDI}. Connected: /dev/ttyS0."


def test_health_says_connecting_once_a_missing_device_is_listed_again(rigs, monkeypatch) -> None:
    # The clock stands still, so the failed open stays the last one while the device is listed.
    listed = [PortInfo("COM4", "COM4", 0x0403, 0x6001, "A50285BI", None, None)]
    monkeypatch.setattr(worker_module, "list_ports", lambda: listed)
    rig = rigs(PortNotFound(FTDI, []), cfg=serial_cfg(FTDI))
    assert rig.health() == f"Connecting to {FTDI}."


def test_health_survives_a_listing_that_fails(rigs, monkeypatch) -> None:
    def failing() -> list[Any]:
        raise OSError("enumeration failed")

    monkeypatch.setattr(worker_module, "list_ports", failing)
    rig = rigs(PortNotFound(FTDI, []), cfg=serial_cfg(FTDI))
    assert rig.health() == f"No device matches {FTDI}. Connected: none."


STALLED = (
    "Bytes arrive but no line ends. Check the baud rate, and run Sample with hex to see the raw "
    "bytes."
)


class Noise(FakeTransport):
    """10 bytes without a line end every 50 ms of `clock`, until `until_ns`."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = Clock()
        self.until_ns = 0

    def readinto(self, buf: bytearray, timeout: float) -> int:
        if self.clock.ns >= self.until_ns:
            return super().readinto(buf, timeout)
        self.clock.ns += 50 * MS
        buf[:10] = b"\xff" * 10
        return 10


def test_health_when_bytes_arrive_without_a_line_end_for_10_s(rigs) -> None:
    noise = Noise()
    rig = rigs(noise, start=False)
    noise.clock = rig.clock
    rig.worker.start()
    rig.settle()
    # The first bytes arrive at 50 ms.
    noise.until_ns = 10 * S
    wait_until(lambda: rig.clock.ns == 10 * S)
    assert rig.health().startswith("Connected.")
    noise.until_ns = 10 * S + 50 * MS
    wait_until(lambda: rig.health() == STALLED)
    assert rig.emitter.lines() == []


SILENT = (
    "Connected. No bytes yet. Check the baud rate and the wiring, and close any program that "
    "opened the port first."
)


def test_health_when_no_byte_arrives_for_10_s_after_each_open(rigs) -> None:
    first, second = Port(), Port()
    rig = rigs(first, second)
    wait_until(lambda: rig.ask("state")["state"] == "open")
    rig.clock.ns = 10 * S - 1
    assert rig.health() == "Connected. 0 lines, 0 signals, device clock not seen."
    rig.clock.ns = 10 * S
    assert rig.health() == SILENT
    first.chunks.append(b"hello\r\n")
    wait_until(lambda: len(rig.emitter.lines()) == 1)
    rig.clock.ns += 60 * S
    assert rig.health() == "Connected. 1 lines, 0 signals, device clock not seen."
    first.read_error = OSError("gone")
    wait_until(lambda: rig.ask("state")["state"] == "connecting")
    rig.clock.ns += 1 * S
    wait_until(lambda: rig.ask("state")["state"] == "open")
    rig.clock.ns += 10 * S
    assert rig.health() == SILENT


def test_prompts_and_text_released_by_a_pause_prove_the_baud_rate(rigs) -> None:
    port = Port([b"Welcome to Buildroot\r\n"])
    rig = rigs(port)
    wait_until(lambda: len(rig.emitter.lines()) == 1)
    rig.clock.ns = 10 * S
    # A quiet port is not a stalled one.
    assert rig.health().startswith("Connected.")
    for dots in range(12):
        port.chunks.append(b"Loading" if dots == 0 else b".")
        wait_until(lambda: not port.chunks)
        rig.settle()
        assert rig.health().startswith("Connected."), dots
        rig.clock.ns += 1 * S
        rig.settle()


@pytest.mark.parametrize(
    "line",
    [b">>> " + b"x" * 4096, b"x" * 4095 + b">tail", b"x" * 4096 + b"root$ "],
    ids=["starts-like-a-prompt", "first-piece-ends-like-a-prompt", "last-piece-is-a-prompt"],
)
def test_a_long_line_is_logged_whole_even_where_a_piece_looks_like_a_prompt(
    rigs, line: bytes
) -> None:
    rig = rigs(Port([line + b"\r\n"]))
    wait_until(lambda: len(rig.emitter.lines()) == 2)
    assert "".join(rig.emitter.messages()) == line.decode()
    assert rig.ask("state")["counters"]["prompts"] == 0


def test_the_end_of_a_long_line_is_not_an_echo(rigs) -> None:
    port = Port([BANNER])
    rig = rigs(port)
    rig.ask("send", text="ls")
    port.chunks.append(b"x" * 4096 + b"ls\r\n")
    wait_until(lambda: len(rig.emitter.lines()) == 3)
    assert rig.emitter.messages()[2] == "ls"
    assert rig.ask("state")["counters"]["echoes"] == 0


@pytest.mark.parametrize(
    ("values", "value_lines", "health"),
    [
        (True, 0, "No values found in 300 lines. Run Sample to see the lines."),
        (False, 0, "Connected. 300 lines, 0 signals, device clock not seen."),
        (True, 1, "Connected. 300 lines, 0 signals, device clock not seen."),
    ],
)
def test_health_of_an_open_port(rigs, values: bool, value_lines: int, health: str) -> None:
    rig = rigs(Port([b"hello\r\n" * 300]), cfg=port_config(values=values))
    rig.emitter.value_lines = value_lines
    wait_until(lambda: len(rig.emitter.lines()) == 300)
    assert rig.health() == health


def test_health_counts_lines_below_300_as_connected(rigs) -> None:
    rig = rigs(Port([zephyr(1.0)]))
    wait_until(lambda: len(rig.emitter.lines()) == 1)
    assert rig.health() == "Connected. 1 lines, 0 signals, device clock in use."


def test_a_failing_presence_check_counts_as_present(rigs, monkeypatch, caplog) -> None:
    checks: list[int] = []

    def present(port: str) -> bool:
        checks.append(rig.clock.ns)
        raise OSError("enumeration failed")

    monkeypatch.setattr(worker_module, "present", present)
    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        rig = rigs(Port(), cfg=serial_cfg(), start=False)
        rig.worker.start()
        wait_until(lambda: rig.ask("state")["state"] == "open")
        for _ in range(2):
            rig.clock.ns += 5 * S
            rig.settle()
            rig.settle()
        assert rig.ask("state")["state"] == "open"
    assert checks == [0, 5 * S, 10 * S]
    assert [r.getMessage() for r in caplog.records] == [
        "dut: cannot check whether /dev/ttyFAKE is connected (enumeration failed); assuming it is.",
    ]


# Unexpected errors


def test_an_unexpected_error_is_logged_and_the_port_carries_on(rigs, monkeypatch, caplog) -> None:
    port = Port([b"bad\r\nok\r\nbad\r\n"])
    rig = rigs(port, start=False)
    record = rig.emitter.line

    def line(name: str, parsed: Parsed, time_ns: int) -> None:
        if parsed.message == "bad":
            raise ValueError("odd line")
        record(name, parsed, time_ns)

    monkeypatch.setattr(rig.emitter, "line", line)
    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        rig.worker.start()
        wait_until(lambda: rig.ask("sample", lines=3)["lines"] == ["bad", "ok", "bad"])
        rig.clock.ns += 60 * S
        port.chunks.append(b"bad\r\nlast\r\n")
        wait_until(lambda: "last" in rig.emitter.messages())
    assert rig.emitter.messages() == ["ok", "last"]
    assert [r.getMessage() for r in caplog.records] == [
        "dut: unexpected error; the port carries on",
        "dut: 2 more unexpected errors, the last: ValueError('odd line')",
    ]
    assert all(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)
    assert rig.ask("state")["counters"]["errors"] == 3


def test_an_unexpected_error_outside_a_line_leaves_the_port_running(rigs, monkeypatch) -> None:
    rig = rigs(Port([b"after\r\n"]), start=False)

    def note(*_: Any, **__: Any) -> None:
        raise ValueError("odd note")

    monkeypatch.setattr(rig.emitter, "note", note)
    rig.worker.start()
    wait_until(lambda: "after" in rig.emitter.messages())
    assert rig.ask("state")["counters"]["errors"] == 1


# Throughput and stopping


class Flood(FakeTransport):
    """Never runs dry: every read fills the buffer with log lines, and counts the reads."""

    CHUNK = zephyr(0.02) * 90

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def readinto(self, buf: bytearray, timeout: float) -> int:
        self.reads += 1
        n = min(len(buf), len(self.CHUNK))
        buf[:n] = self.CHUNK[:n]
        return n


def test_requests_are_served_within_one_loop_under_a_flood(rigs, monkeypatch) -> None:
    # Reads, not milliseconds: a shared CI runner's scheduler decides the wall time.
    flood = Flood()
    rig = rigs(flood)
    wait_until(lambda: len(rig.emitter.lines()) > 1000)
    counters = rig.emitter.counters
    served_at: list[int] = []

    def counting(port: str) -> dict[str, int]:
        # A state request reads the counters on the worker's thread, while it is served.
        served_at.append(flood.reads)
        return counters(port)

    monkeypatch.setattr(rig.emitter, "counters", counting)
    for _ in range(5):
        queued_at = flood.reads
        served_at.clear()
        rig.ask("state")
        # Queued during one read, served before the read after next.
        assert served_at[0] - queued_at <= 2


class BlockedWrite(FakeTransport):
    """A write that flow control holds until the port closes."""

    def __init__(self) -> None:
        super().__init__()
        self.writing = threading.Event()
        self.released = threading.Event()

    def write(self, data: bytes) -> int:
        self.writing.set()
        self.released.wait(5)
        raise OSError(errno.EBADF, "closed")

    def close(self) -> None:
        super().close()
        self.released.set()


def assert_stops_in_a_second(worker: PortWorker) -> None:
    started = time.monotonic()
    worker.stop()
    worker.join(1.0)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1.0


def test_held_signal_rows_are_flushed_every_loop_and_when_the_port_closes(
    rigs, monkeypatch
) -> None:
    wall = Wall(monkeypatch)
    rig = rigs(Port())
    wall.clock = rig.clock
    rig.clock.ns = 7 * S
    rig.settle()
    assert rig.emitter.flushes[-1] == 50 * S + 7 * S
    rig.ask("release")
    assert None in rig.emitter.flushes


def test_stop_during_backoff(rigs) -> None:
    rig = rigs(ConnectionRefusedError())
    wait_until(lambda: rig.opened)
    assert_stops_in_a_second(rig.worker)


def test_stop_during_a_blocked_write(rigs) -> None:
    port = BlockedWrite()
    rig = rigs(port)
    send = rig.worker.request("send", text="x")
    queued = rig.worker.request("state")
    assert port.writing.wait(2)
    assert_stops_in_a_second(rig.worker)
    for future in (send, queued):
        with pytest.raises(RuntimeError, match="port is stopped"):
            future.result(timeout=0)


def test_stop_during_a_command(rigs) -> None:
    port = Port()
    rig = rigs(port)
    future = rig.worker.request("command", text="x", timeout_s=60)
    rig.settle()
    assert_stops_in_a_second(rig.worker)
    assert future.result(timeout=0)["ended_by"] == "aborted"
    assert port.closed


# End to end


def test_demo_device_end_to_end() -> None:
    emitter = RecordingEmitter()
    worker = PortWorker(
        port_config(connection="demo", name="demo"), emitter, SETTINGS, open_transport
    )
    worker.start()
    try:
        wait_until(lambda: any(p.module == "dcdc" for p in emitter.lines()))
        reply = worker.request("command", text="dcdc limit get").result(timeout=3)
        assert reply["reply"] == ["limit: 4.5 A"]
        assert reply["ended_by"] == "prompt"
        assert any(v.name == "rail" for p in emitter.lines() for v in p.values)
    finally:
        worker.stop()
        worker.join(1.0)
    assert not worker.is_alive()
