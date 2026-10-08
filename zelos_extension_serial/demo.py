"""A simulated Zephyr board behind the Transport protocol."""

import re
import time
from collections import deque
from collections.abc import Callable

_BYTES_PER_S = 11_520  # 115200 baud, ten bits per byte
_BYTE_NS = 1_000_000_000 // _BYTES_PER_S + 1
_CYCLE_NS = 20_000_000
_WARN_NS = 5_000_000_000

_PROMPT_TEXT = "uart:~$ "
_PROMPT = f"\x1b[1;32m{_PROMPT_TEXT}\x1b[m"
_PROMPT_WIDTH = len(_PROMPT_TEXT)

_LIMIT_DEFAULT_DA = 45
_LIMIT_MAX_DA = 200
_SLEW_CA_PER_CYCLE = 250
_RAIL_NOMINAL_CV = 1380
_RAIL_DROOP_CA_PER_CV = 40
_RAIL_RIPPLE_CV = 5
_TEMP_MIN_DC = 350
_TEMP_PERIOD_CYCLES = 60_000_000_000 // _CYCLE_NS
_TEMP_CYCLES_PER_DC = _TEMP_PERIOD_CYCLES // 2 // 100

_AMPS = re.compile(r"([0-9]+)(?:\.([0-9]))?")


def _deci(value: int) -> str:
    """Tenths as decimal text: 45 is `4.5`."""
    return f"{value // 10}.{value % 10}"


def _centi(value: int) -> str:
    """Hundredths as decimal text: 1364 is `13.64`."""
    return f"{value // 100}.{value % 100:02d}"


def _deciamps(text: str) -> int | None:
    """Tenths of an amp in `0` to `20.0` with at most one decimal, else None."""
    match = _AMPS.fullmatch(text)
    if not match:
        return None
    value = int(match[1]) * 10 + int(match[2] or 0)
    return value if value <= _LIMIT_MAX_DA else None


def _stamp(ns: int) -> str:
    """Zephyr's log timestamp, `hh:mm:ss.mmm,uuu`."""
    ms = ns // 1_000_000
    return (
        f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d},000"
    )


def _error(text: str) -> str:
    """A shell error line: red, as `shell_error` prints it."""
    return f"\x1b[1;31m{text}\x1b[m\r\n"


class DemoDevice:
    """Prints exactly what a Zephyr board with the shell prints."""

    where = "demo"

    # perf_counter_ns: monotonic_ns ticks every 15.6 ms on Windows, too coarse to pace bytes.
    def __init__(self, now: Callable[[], int] = time.perf_counter_ns) -> None:
        self._now = now
        self._dtr = True
        self._boot()

    def _boot(self) -> None:
        self._start = self._free_ns = self._now()
        # (ns its first byte starts to leave, text): what the line still has to send, in order.
        self._wire: deque[tuple[int, bytes]] = deque()
        self._queue(f"\x1b[m{_PROMPT}", self._start)
        self._typed = ""
        self._after_cr = False
        self._status_at = 0
        self._warn_at = _WARN_NS
        self._limit_da = _LIMIT_DEFAULT_DA
        self._in_ca = _LIMIT_DEFAULT_DA * 10
        self._print("*** Booting Zephyr OS build dccb09599635 ***\r\n", self._start)

    def readinto(self, buf: bytearray, timeout: float) -> int:
        """Copy the bytes the line has carried so far; wait at most `timeout` for the first."""
        deadline = time.perf_counter() + timeout
        while (n := self._take(buf)) == 0 and (left := deadline - time.perf_counter()) > 0:
            # At least 1 ms: the next byte may be due already on a clock that has not ticked.
            time.sleep(min(left, max(0.001, (self._wake() - self._now()) / 1e9)))
        return n

    def write(self, data: bytes) -> int:
        """Type `data` at the shell."""
        at = self._now()
        self._advance(at)
        for ch in data.decode("latin-1"):
            self._type(ch, at)
        return len(data)

    def set_lines(self, dtr: bool | None, rts: bool | None) -> None:
        """Reset the board when DTR rises."""
        if dtr is None:
            return
        if dtr and not self._dtr:
            self._boot()
        self._dtr = dtr

    def close(self) -> None:
        """Nothing to release."""

    def _take(self, buf: bytearray) -> int:
        now = self._now()
        self._advance(now)
        n = 0
        while self._wire:
            start, text = self._wire[0]
            k = min(len(buf) - n, len(text), (now - start) // _BYTE_NS)
            if k <= 0:
                break
            buf[n : n + k] = text[:k]
            n += k
            if k < len(text):
                self._wire[0] = (start + k * _BYTE_NS, text[k:])
                break
            self._wire.popleft()
        return n

    def _wake(self) -> int:
        """When the next byte can leave the line."""
        if self._wire:
            return self._wire[0][0] + _BYTE_NS
        return self._start + min(self._status_at, self._warn_at)

    def _queue(self, text: str, at: int) -> None:
        """Send `text` once the line is free, but not before `at`: an idle line saves up nothing."""
        data = text.encode("latin-1")
        start = max(at, self._free_ns)
        self._wire.append((start, data))
        self._free_ns = start + len(data) * _BYTE_NS

    def _print(self, text: str, at: int) -> None:
        """Print a message the way the shell does: over the prompt and what is typed on it."""
        erase = f"\x1b[{_PROMPT_WIDTH + len(self._typed)}D\x1b[J"
        self._queue(f"{erase}{text}{_PROMPT}{self._typed}", at)

    def _advance(self, now: int) -> None:
        """Print every scheduled line that is due by `now`."""
        while (due := min(self._status_at, self._warn_at)) <= now - self._start:
            if self._warn_at <= self._status_at:
                count = self._warn_at // _WARN_NS - 1
                imbalance = 30 + count * 7 % 21
                text = f"[{_stamp(due)}] \x1b[1;33m<wrn> bms: cell imbalance {imbalance}mV\x1b[0m"
                self._warn_at += _WARN_NS
            else:
                text = f"[{_stamp(due)}] \x1b[0m<inf> dcdc: {self._status()}\x1b[0m"
                self._status_at += _CYCLE_NS
            self._print(f"{text}\r\n", self._start + due)

    def _status(self) -> str:
        cycle = self._status_at // _CYCLE_NS
        phase = cycle % _TEMP_PERIOD_CYCLES
        triangle = min(phase, _TEMP_PERIOD_CYCLES - phase)
        ripple = cycle % (2 * _RAIL_RIPPLE_CV + 1) - _RAIL_RIPPLE_CV
        step = self._limit_da * 10 - self._in_ca
        self._in_ca += max(-_SLEW_CA_PER_CYCLE, min(_SLEW_CA_PER_CYCLE, step))
        rail = _RAIL_NOMINAL_CV - self._in_ca // _RAIL_DROOP_CA_PER_CV + ripple
        temp = _TEMP_MIN_DC + triangle // _TEMP_CYCLES_PER_DC
        return (
            f"rail={_centi(rail)}V in={_centi(self._in_ca)}A "
            f"limit={_deci(self._limit_da)}A temp={_deci(temp)}C"
        )

    def _type(self, ch: str, at: int) -> None:
        after_cr, self._after_cr = self._after_cr, ch == "\r"
        if ch in "\r\n":
            if ch == "\n" and after_cr:
                return
            line, self._typed = self._typed, ""
            self._queue(f"\r\n{self._run(line)}{_PROMPT}", at)
        elif ch.isprintable():
            self._typed += ch
            self._queue(ch, at)

    def _run(self, line: str) -> str:
        words = line.split()
        if not words:
            return ""
        if words == ["dcdc", "limit", "get"]:
            return f"limit: {_deci(self._limit_da)} A\r\n"
        if len(words) == 4 and words[:3] == ["dcdc", "limit", "set"]:
            da = _deciamps(words[3])
            if da is None:
                return _error("limit must be 0.0 to 20.0 A, one decimal at most")
            self._limit_da = da
            return f"limit set to {_deci(da)} A\r\n"
        return _error(f"{words[0]}: command not found")
