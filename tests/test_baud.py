"""Finding the baud a device prints at: the rates tried, the text test, and the time it takes."""

import errno
import os
import threading
import time
from pathlib import Path

import pytest
from serialx import Parity, PinState

from tests.conftest import NOISE, Board
from zelos_extension_serial import actions, baud
from zelos_extension_serial.baud import RATES, Heard, detect, listen, readable
from zelos_extension_serial.config import serial_port

PATH = "/dev/ttyUSB0"
VECTORS = sorted((Path(__file__).parent / "vectors").glob("*.txt"))
# A rate's settle and window.
SLOT_S = baud._SETTLE_S + baud._WINDOW_S


class Clock:
    """time.monotonic and time.sleep for one thread: sleeping moves it on at once."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


def heard(board: Board, boards: dict[str, Board], deadline: float = baud.LISTEN_S) -> Heard:
    boards[PATH] = board
    return listen(serial_port(PATH), deadline)


# Rates


@pytest.mark.parametrize("rate", RATES)
def test_text_is_found_at_its_rate_and_the_rest_are_not_tried(
    boards: dict[str, Board], clock: Clock, rate: int
) -> None:
    board = Board(baud=rate)

    assert heard(board, boards) == Heard(rate)
    assert board.rates == list(RATES[: RATES.index(rate) + 1])
    assert len(board.closed) == 1
    assert board.writes == []


def test_a_bridge_that_gives_nothing_at_a_wrong_rate_is_still_found(
    boards: dict[str, Board], clock: Clock
) -> None:
    """An ST-LINK V3 bridge at a wrong baud delivers no bytes at all."""
    assert heard(Board(baud=9600, noise=False), boards) == Heard(9600)


def test_a_silent_device_is_heard_at_every_rate_and_keeps_none(
    boards: dict[str, Board], clock: Clock
) -> None:
    board = Board()

    assert heard(board, boards) == Heard(None, "no data arrived")
    assert board.rates == list(RATES)
    assert clock.now == pytest.approx(len(RATES) * SLOT_S)
    assert len(board.closed) == 1


def test_noise_at_every_rate_is_not_text(boards: dict[str, Board], clock: Clock) -> None:
    board = Board(baud=1200)

    assert heard(board, boards) == Heard(None, "no baud tried gave readable text")
    assert board.rates == list(RATES)


def test_a_rate_the_driver_refuses_is_skipped(boards: dict[str, Board], clock: Clock) -> None:
    board = Board(baud=921600, refuses=frozenset({460800}))

    assert heard(board, boards) == Heard(921600)
    assert 460800 not in board.rates


def test_the_port_opens_as_the_extension_opens_it(boards: dict[str, Board], clock: Clock) -> None:
    board = Board(baud=115200)

    heard(board, boards)

    assert board.settings == {
        "baudrate": 115200,
        "byte_size": 8,
        "parity": Parity.NONE,
        "stopbits": 1.0,
        "rtscts": False,
        "xonxoff": False,
        "exclusive": True,
        "write_timeout": 1.0,
        "dtr_on_open": PinState.HIGH,
        "rts_on_open": PinState.HIGH,
        "dtr_on_close": PinState.UNDEFINED,
        "rts_on_close": PinState.UNDEFINED,
    }


@pytest.mark.parametrize(
    ("error", "why"),
    [
        (
            OSError(errno.EBUSY, "already locked"),
            "it is in use by another program, or access was denied",
        ),
        (OSError(errno.EACCES, "Permission denied"), "permission to open it was denied"),
        (OSError(errno.EIO, "I/O error"), "it cannot be opened ([Errno 5] I/O error)"),
    ],
)
def test_a_port_that_cannot_be_opened_says_why(
    boards: dict[str, Board], clock: Clock, error: OSError, why: str
) -> None:
    board = Board(baud=9600, open_error=error)

    assert heard(board, boards) == Heard(None, why)
    assert board.rates == []


def test_a_port_that_fails_while_heard_is_closed(boards: dict[str, Board], clock: Clock) -> None:
    board = Board(baud=9600, read_error=OSError(errno.ENXIO, "Device not configured"))

    assert heard(board, boards) == Heard(
        None, "it cannot be read ([Errno 6] Device not configured)"
    )
    assert len(board.closed) == 1


# Time


def test_every_rate_gets_its_whole_window_within_the_wait() -> None:
    assert len(RATES) * SLOT_S <= baud.LISTEN_S


def test_the_longest_auto_configure_ends_before_the_action_times_out() -> None:
    # The first run after an install imports for up to 12 s on macOS.
    import_s = 12.0
    assert import_s + baud.LISTEN_S + baud._CLOSE_S < actions._STANDALONE_TIMEOUT_S


def test_listening_stops_at_the_deadline(boards: dict[str, Board], clock: Clock) -> None:
    board = Board()

    assert heard(board, boards, deadline=2.5) == Heard(None, "no data arrived")
    # Two whole rates, then the third's settle and what is left of its window.
    assert board.rates == list(RATES[:3])
    assert clock.now == pytest.approx(2.5)


def test_devices_are_heard_at_once(boards: dict[str, Board], quick: None) -> None:
    paths = [f"/dev/ttyUSB{n}" for n in range(3)]
    boards.update({paths[0]: Board(), paths[1]: Board(baud=9600), paths[2]: Board()})
    threads = threading.active_count()

    found = detect([serial_port(path) for path in paths])

    assert found == [Heard(None, "no data arrived"), Heard(9600), Heard(None, "no data arrived")]
    # Each silent device was open before the other closed.
    silent = [boards[paths[0]], boards[paths[2]]]
    assert max(board.opened[0] for board in silent) < min(board.closed[0] for board in silent)
    assert threading.active_count() == threads


def test_a_device_stuck_in_its_driver_is_given_up_at_the_deadline(
    quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(baud, "LISTEN_S", 0.2)
    monkeypatch.setattr(baud, "_CLOSE_S", 0.1)
    stuck = threading.Event()

    def open_stuck(_cfg: object) -> object:
        stuck.wait(5)
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(baud.transport, "open_serial", open_stuck)
    started = time.monotonic()
    try:
        assert detect([serial_port(PATH)]) == [Heard(None, "it did not answer within 0.2 s")]
        assert time.monotonic() - started < 1.0
    finally:
        stuck.set()
        # Ended here, not in a later test that counts threads.
        for thread in threading.enumerate():
            if thread.name == f"listen {PATH}":
                thread.join(1.0)


# Text


@pytest.mark.parametrize("path", VECTORS, ids=lambda path: path.stem)
def test_every_format_the_extension_reads_is_text(path: Path) -> None:
    lines = [raw.rpartition(" ⇒ ")[0] for raw in path.read_text(encoding="utf-8").splitlines()]
    data = "".join(f"{line}\r\n" for line in lines if line).encode()

    assert readable(data[: baud._ENOUGH_BYTES])


@pytest.mark.parametrize(
    "data",
    [
        b"\x1b[1;31m[00:00:01.000,000] <err> dcdc: overcurrent\x1b[0m\r\n",
        "temp=21.5°C current=12µA\n".encode(),
        b"uart:~$ \r\nok\r\n",
    ],
    ids=["colour", "units", "prompt"],
)
def test_text_is_readable(data: bytes) -> None:
    assert readable(data)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        NOISE,
        b"\x00" * 64 + b"\n",
        bytes(range(256)) * 4,
        b"rail=13.73V without a line end",
        b"\r\n\r\n",
        b"rail=13.73V\n" + b"\xfb\x80\xf8" * 8,
    ],
    ids=["nothing", "noise", "nul", "every-byte", "no-line-end", "only-line-ends", "mostly-noise"],
)
def test_noise_and_partial_lines_are_not_readable(data: bytes) -> None:
    assert not readable(data)


# A real port


def test_a_pty_is_heard_at_every_rate_and_closed(pty: tuple[int, str], quick: None) -> None:
    """A pty has no baud, so this shows that serialx opens, changes rate, reads and closes a real
    port, not that a rate is found: text reads the same at every rate."""
    controller, path = pty

    assert listen(serial_port(path), time.monotonic() + 5) == Heard(None, "no data arrived")

    printing = threading.Event()

    def print_lines() -> None:
        # Each rate change drops what arrived before it, so the device keeps printing.
        while not printing.wait(0.01):
            os.write(controller, b"[00:00:01.000,000] <inf> dcdc: rail=13.73V\r\n")

    writer = threading.Thread(target=print_lines)
    writer.start()
    try:
        assert listen(serial_port(path), time.monotonic() + 5) == Heard(115200)
    finally:
        printing.set()
        writer.join()
    # Closed: it opens again.
    baud.transport.open_serial(serial_port(path)).close()


def test_a_pty_another_program_holds_is_left_alone(pty: tuple[int, str], quick: None) -> None:
    _, path = pty
    held = baud.transport.open_serial(serial_port(path))
    try:
        assert listen(serial_port(path), time.monotonic() + 5) == Heard(
            None, "it is in use by another program, or access was denied"
        )
    finally:
        held.close()
