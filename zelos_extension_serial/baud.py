"""The baud a serial device prints at, found by listening at each common rate."""

import contextlib
import threading
import time
from dataclasses import dataclass

from serialx import UnsupportedSetting

from . import transport
from .config import PortConfig
from .lines import LineSplitter

#: Most common first. 115200 is the default of this extension, Zephyr, ESP-IDF and the Linux
#: console; 9600 that of Arduino's examples. macOS sets the last two through a driver call that
#: can refuse them.
RATES = (115200, 9600, 57600, 38400, 19200, 230400, 460800, 921600)
#: Every device is done by then: devices are heard at once, and their rates one after another.
LISTEN_S = 9.0
# Long enough to hold a line from a board that prints once a second.
_WINDOW_S = 1.0
# A rate is judged once this much arrives: 90 ms at 115200.
_ENOUGH_BYTES = 1024
# A USB bridge can deliver bytes it received at the old rate for a few ms after a change.
_SETTLE_S = 0.05
# At a wrong rate, most bytes come out as NUL, control codes or invalid UTF-8.
_MIN_TEXT = 0.9
# A listener closes its port after the deadline; a closing port is waited for this long.
_CLOSE_S = 1.0


@dataclass(frozen=True, slots=True)
class Heard:
    """What listening to one device found: its baud, else why it has none."""

    baud: int | None
    why: str | None = None


def detect(cfgs: list[PortConfig]) -> list[Heard]:
    """Listen to every device at once; each device's answer, in order."""
    deadline = time.monotonic() + LISTEN_S
    heard: dict[int, Heard] = {}

    def listen_to(index: int, cfg: PortConfig) -> None:
        heard[index] = listen(cfg, deadline)

    # A thread stuck in a driver past the wait ends on its own.
    threads = [
        threading.Thread(target=listen_to, args=item, name=f"listen {item[1].port}", daemon=True)
        for item in enumerate(cfgs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline + _CLOSE_S - time.monotonic()))
    late = Heard(None, f"it did not answer within {LISTEN_S:g} s")
    return [heard.get(index, late) for index in range(len(cfgs))]


def listen(cfg: PortConfig, deadline: float) -> Heard:
    """Open the port once, and try each rate until one reads as text or the deadline passes.

    Writes nothing. The port opens with DTR and RTS as the extension opens it, so a board that
    resets when it starts the port resets here too.
    """
    try:
        port = transport.open_serial(cfg)
    except Exception as e:
        return Heard(None, _cannot_open(e))
    arrived = False
    try:
        for baud in RATES:
            if time.monotonic() >= deadline:
                break
            try:
                port.set_baud(baud)
            except UnsupportedSetting:
                # The driver refuses this rate; the device cannot be using it here.
                continue
            _read(port, _SETTLE_S, deadline)
            data = _read(port, _WINDOW_S, deadline)
            arrived = arrived or bool(data)
            if readable(data):
                return Heard(baud)
    except Exception as e:
        return Heard(None, f"it cannot be read ({e})")
    finally:
        # A port that is already gone can fail to close; it is closed either way.
        with contextlib.suppress(Exception):
            port.close()
    return Heard(None, "no baud tried gave readable text" if arrived else "no data arrived")


def readable(data: bytes) -> bool:
    """Whether bytes are text the extension can log: at least one whole line, and almost every
    character printable."""
    text = data.decode("utf-8", "replace")
    # ESC starts a colour code, whose other bytes are printable.
    printable = sum(c in "\t\r\n\x1b" or (c.isprintable() and c != "�") for c in text)
    lines = LineSplitter().feed(data, 0)
    return printable >= _MIN_TEXT * len(text) and any(line.text.strip() for line in lines)


def _read(port: transport.SerialxTransport, window_s: float, deadline: float) -> bytes:
    """What arrives within `window_s`, ending early once there is enough or at the deadline."""
    until = min(deadline, time.monotonic() + window_s)
    data, buf = bytearray(), bytearray(_ENOUGH_BYTES)
    while len(data) < _ENOUGH_BYTES and (left := until - time.monotonic()) > 0:
        data += buf[: port.readinto(buf, left)]
    return bytes(data)


def _cannot_open(error: Exception) -> str:
    """Why a port did not open, in the words of the extension's health."""
    fault = transport.classify(error)
    if fault == "permission":
        return "permission to open it was denied"
    if fault in ("busy", "in_use_or_denied"):
        return "it is in use by another program, or access was denied"
    return f"it cannot be opened ({error})"
