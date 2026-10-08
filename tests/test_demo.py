"""The demo device prints what a Zephyr board with the shell prints."""

import re
import time

import pytest

from zelos_extension_serial.demo import DemoDevice
from zelos_extension_serial.lines import LineSplitter

PROMPT = b"\x1b[1;32muart:~$ \x1b[m"
ERASE = b"\x1b[8D\x1b[J"
BANNER = b"*** Booting Zephyr OS build dccb09599635 ***\r\n"
BOOT = b"\x1b[m" + PROMPT + ERASE + BANNER
STATUS = re.compile(
    rb"\[(\d\d):(\d\d):(\d\d)\.(\d\d\d),000\] \x1b\[0m<inf> dcdc: "
    rb"rail=(\S+)V in=(\S+)A limit=(\S+)A temp=(\S+)C\x1b\[0m\r\n"
)


def red(text: str) -> bytes:
    return b"\x1b[1;31m" + text.encode() + b"\x1b[m\r\n"


class Bench:
    """A demo device on a clock the test moves."""

    def __init__(self) -> None:
        self.ns = 0
        self.device = DemoDevice(now=lambda: self.ns)

    def run(self, seconds: float, step_ms: int = 5) -> bytes:
        """Move the clock `seconds` ahead, reading at every step."""
        out = bytearray()
        buf = bytearray(4096)
        for _ in range(round(seconds * 1000 / step_ms)):
            self.ns += step_ms * 1_000_000
            while n := self.device.readinto(buf, 0):
                out += buf[:n]
        return bytes(out)

    def type(self, text: bytes) -> bytes:
        """Write `text`, then let the reply arrive."""
        self.device.write(text)
        return self.run(0.1)


@pytest.fixture
def bench() -> Bench:
    return Bench()


def stamp_ms(m: re.Match[bytes]) -> int:
    """The device ms a status line was printed at."""
    return ((int(m[1]) * 60 + int(m[2])) * 60 + int(m[3])) * 1000 + int(m[4])


def statuses(stream: bytes) -> list[tuple[int, dict[str, bytes]]]:
    """Each status line as (device ms, values)."""
    return [
        (stamp_ms(m), {"rail": m[5], "in": m[6], "limit": m[7], "temp": m[8]})
        for m in STATUS.finditer(stream)
    ]


def test_where() -> None:
    assert DemoDevice().where == "demo"


def test_boot_bytes_are_those_of_the_real_board(bench: Bench) -> None:
    stream = bench.run(0.05)
    assert stream.startswith(
        BOOT
        + PROMPT
        + ERASE
        + b"[00:00:00.000,000] \x1b[0m<inf> dcdc: rail=13.64V in=4.50A limit=4.5A temp=35.0C"
        b"\x1b[0m\r\n"
        + PROMPT
        + ERASE
        + b"[00:00:00.020,000] \x1b[0m<inf> dcdc: rail=13.65V in=4.50A limit=4.5A temp=35.0C"
        b"\x1b[0m\r\n"
    )


def test_one_status_line_every_20_ms(bench: Bench) -> None:
    stream = bench.run(2.0)
    times = [ms for ms, _ in statuses(stream)]
    assert times[:100] == list(range(0, 2000, 20))


def test_value_model(bench: Bench) -> None:
    rows = [values for _, values in statuses(bench.run(61.0, step_ms=20))]
    assert {v["limit"] for v in rows} == {b"4.5"}
    assert {v["in"] for v in rows} == {b"4.50"}
    assert {v["rail"] for v in rows} == {b"13.%02d" % c for c in range(64, 75)}
    temps = [float(v["temp"]) for v in rows]
    assert (min(temps), max(temps), temps[0], temps[1500]) == (35.0, 45.0, 35.0, 45.0)


def test_warning_every_5_s(bench: Bench) -> None:
    stream = bench.run(10.1)
    warning = b"[00:00:%02d.000,000] \x1b[1;33m<wrn> bms: cell imbalance %dmV\x1b[0m\r\n"
    assert stream.count(b"<wrn>") == 2
    assert PROMPT + ERASE + warning % (5, 30) + PROMPT in stream
    assert PROMPT + ERASE + warning % (10, 37) + PROMPT in stream
    assert stream.index(warning % (5, 30)) < stream.index(b"[00:00:05.000,000] \x1b[0m")


@pytest.mark.parametrize(
    ("typed", "reply"),
    [
        (b"dcdc limit get", b"limit: 4.5 A\r\n"),
        (b"dcdc limit set 2.0", b"limit set to 2.0 A\r\n"),
        (b"dcdc limit set 20", b"limit set to 20.0 A\r\n"),
        (b"dcdc limit set 0.5", b"limit set to 0.5 A\r\n"),
        (b"dcdc limit set 20.1", red("limit must be 0.0 to 20.0 A, one decimal at most")),
        (b"dcdc limit set 2.55", red("limit must be 0.0 to 20.0 A, one decimal at most")),
        (b"dcdc limit set -1", red("limit must be 0.0 to 20.0 A, one decimal at most")),
        (b"dcdc limit set abc", red("limit must be 0.0 to 20.0 A, one decimal at most")),
        (b"reboot now", red("reboot: command not found")),
        (b"", b""),
    ],
)
def test_command_reply(bench: Bench, typed: bytes, reply: bytes) -> None:
    bench.run(0.1)
    stream = bench.type(typed + b"\r")
    assert PROMPT + typed + b"\r\n" + reply + PROMPT in stream


def test_a_rejected_limit_changes_nothing(bench: Bench) -> None:
    bench.type(b"dcdc limit set 20.1\r")
    assert b"limit: 4.5 A\r\n" in bench.type(b"dcdc limit get\r")


def test_set_limit_is_read_back(bench: Bench) -> None:
    bench.type(b"dcdc limit set 7.5\r")
    assert b"limit: 7.5 A\r\n" in bench.type(b"dcdc limit get\r")


@pytest.mark.parametrize("ending", [b"\r", b"\n", b"\r\n"])
def test_line_endings_run_the_command_once(bench: Bench, ending: bytes) -> None:
    stream = bench.type(b"dcdc limit get" + ending)
    assert stream.count(b"limit: 4.5 A") == 1
    assert PROMPT + b"\r\n" not in stream


def test_current_follows_the_new_limit_within_200_ms(bench: Bench) -> None:
    bench.run(0.1)
    bench.device.write(b"dcdc limit set 20\r")
    rows = [(ms, v) for ms, v in statuses(bench.run(0.5)) if v["limit"] == b"20.0"]
    assert float(rows[0][1]["in"]) <= 7.0
    reached = next(ms for ms, v in rows if v["in"] == b"20.00")
    assert reached - rows[0][0] <= 200
    assert b"13.25" <= rows[-1][1]["rail"] <= b"13.35"


def test_typed_text_is_echoed_and_kept_on_screen_under_log_lines(bench: Bench) -> None:
    bench.run(0.025)
    bench.device.write(b"dc")
    stream = bench.run(0.035)
    assert PROMPT + b"dc\x1b[10D\x1b[J[00:00:00.040,000]" in stream
    assert stream.endswith(PROMPT + b"dc")


def test_dtr_pulse_restarts_the_board(bench: Bench) -> None:
    bench.type(b"dcdc limit set 20\r")
    bench.run(1.0)
    bench.device.set_lines(dtr=False, rts=None)
    bench.device.set_lines(dtr=True, rts=None)
    stream = bench.run(0.2)
    assert stream.startswith(BOOT)
    times = [ms for ms, _ in statuses(stream)]
    assert times[:2] == [0, 20]
    assert max(times) <= 200
    assert statuses(stream)[0][1]["limit"] == b"4.5"


def test_other_line_changes_do_not_restart(bench: Bench) -> None:
    bench.run(0.1)
    bench.device.set_lines(dtr=True, rts=False)
    bench.device.set_lines(dtr=None, rts=True)
    assert BANNER not in bench.run(0.1)


def test_output_is_paced_to_a_115200_baud_line(bench: Bench) -> None:
    bench.device.write(b"x\r" * 400)
    buf = bytearray(4096)
    total = 0
    for ms in range(1, 2001):
        bench.ns = ms * 1_000_000
        while n := bench.device.readinto(buf, 0):
            total += n
        assert total <= 11_520 * ms / 1000 + 1
        if ms == 500:
            assert total >= 11_520 * 0.5 - 100


def test_an_idle_line_does_not_bank_bytes(bench: Bench) -> None:
    bench.run(3.0)
    bench.device.write(b"x\r" * 400)
    before = bench.ns
    total = len(bench.run(0.1, step_ms=100))
    assert total <= 11_520 * (bench.ns - before) / 1e9 + 1


@pytest.mark.parametrize("step_ms", [16, 33, 250])
def test_a_late_reader_never_gets_a_line_before_the_wire_carried_it(
    bench: Bench, step_ms: int
) -> None:
    arrived: list[int] = []
    stream = bytearray()
    buf = bytearray(4096)
    for _ in range(2000 // step_ms):
        bench.ns += step_ms * 1_000_000
        while n := bench.device.readinto(buf, 0):
            stream += buf[:n]
            arrived += [bench.ns] * n
    lines = list(STATUS.finditer(stream))
    assert len(lines) >= 90
    for line in lines:
        printed_ns = stamp_ms(line) * 1_000_000
        wire_ns = (line.end() - line.start()) * 1_000_000_000 // 11_520
        assert arrived[line.end() - 1] >= printed_ns + wire_ns, line[0]


def test_an_echo_does_not_leave_before_it_was_typed(bench: Bench) -> None:
    bench.ns = 35_000_000  # Two status lines printed, none read yet.
    bench.device.write(b"x")
    buf = bytearray(4096)
    n = bench.device.readinto(buf, 0)
    assert buf[:n].endswith(PROMPT)
    assert bench.run(0.001, step_ms=1) == b"x"


def test_readinto_returns_within_the_timeout_on_the_real_clock() -> None:
    device = DemoDevice()
    buf = bytearray(65536)
    start = time.monotonic()
    assert device.readinto(buf, 1.0) > 0
    assert device.readinto(buf, 0.05) < len(buf)
    assert time.monotonic() - start < 0.5


def test_readinto_gives_up_after_the_timeout_when_the_device_is_silent() -> None:
    device = DemoDevice(now=lambda: 0)
    start = time.monotonic()
    assert device.readinto(bytearray(16), 0.05) == 0
    assert time.monotonic() - start < 0.5


def test_two_seconds_of_demo_output_split_into_lines(bench: Bench) -> None:
    try:
        LineSplitter().idle()
    except NotImplementedError:
        pytest.skip("LineSplitter.idle is not implemented yet")
    splitter = LineSplitter()
    lines = splitter.feed(bench.run(2.0), 0)
    texts = [line.text for line in lines if "dcdc" in line.text]
    assert len(texts) >= 99
    assert texts[0].startswith("[00:00:00.000,000]") and "rail=13.64V" in texts[0]
