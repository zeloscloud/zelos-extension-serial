"""Shared by every test: an isolated trace recording, a fake transport, a faked device listing,
fake serial devices, a port config, and the actions run as the agent runs them."""

import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import serialx
import zelos_sdk
from serialx import UnsupportedSetting

from zelos_extension_serial import ACTION_PREFIX, actions, baud, transport
from zelos_extension_serial.config import PortConfig
from zelos_extension_serial.transport import Transport

# Not reader.time_range(): it raises on a trace that holds no rows.
ALL_TIME = ("1970-01-01T00:00:00Z", "2200-01-01T00:00:00Z")


@dataclass
class Recording:
    """A trace source in its own namespace, written to `path`."""

    source: zelos_sdk.TraceSource
    path: Path
    writer: zelos_sdk.TraceWriter

    def events(self) -> dict[str, list[dict[str, Any]]]:
        """Close the recording and read it back with read_events()."""
        self.source.flush()
        # A .trz reads back empty until its writer has closed it.
        self.writer.close()
        return read_events(self.path)


# No field can be named this: sanitising turns `@` into `_`, so a printed `time_s` stays a field.
TIME = "@time_s"


def read_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Event name (`dut/log`) to its rows, each `{TIME: float, field: value, ...}`."""
    events: dict[str, list[dict[str, Any]]] = {}
    with zelos_sdk.TraceReader(str(path)) as reader:
        segments = [segment.id for segment in reader.list_data_segments()]
        for source in reader.list_fields():
            for event in source.events:
                fields = [field.path for field in event.fields]
                data = reader.query(segments, fields, *ALL_TIME).to_arrow()
                # Fields are named `<source>/<event>.<field>`, the time `time_s`; an event with no
                # rows returns b"".
                prefix = f"{source.name}/{event.name}."
                rows = pa.ipc.open_stream(data).read_all().to_pylist() if data else []
                events[event.name] = [
                    {
                        key.removeprefix(prefix) if key.startswith(prefix) else TIME: value
                        for key, value in row.items()
                    }
                    for row in rows
                ]
    return events


@pytest.fixture
def trace_file(tmp_path: Path) -> Iterator[Recording]:
    """A recording that never touches the global source or an agent."""
    namespace = zelos_sdk.TraceNamespace(f"test-{uuid.uuid4().hex}")
    source = zelos_sdk.TraceSource(ACTION_PREFIX, namespace=namespace)
    path = tmp_path / "trace.trz"
    writer = zelos_sdk.TraceWriter(str(path), namespace=namespace)
    writer.open()
    yield Recording(source, path, writer)
    writer.close()


class FakeTransport:
    """Plays byte chunks, then times out on every read; records writes and line changes."""

    where = "fake"

    def __init__(self, chunks: Iterable[bytes] = ()) -> None:
        self.chunks = list(chunks)
        self.writes: list[bytes] = []
        self.lines: list[tuple[bool | None, bool | None]] = []
        self.closed = False

    def readinto(self, buf: bytearray, timeout: float) -> int:
        if not self.chunks:
            # A real port blocks for the whole timeout; returning at once would spin the worker.
            time.sleep(timeout)
            return 0
        chunk = self.chunks.pop(0)
        n = min(len(chunk), len(buf))
        buf[:n] = chunk[:n]
        if n < len(chunk):
            self.chunks.insert(0, chunk[n:])
        return n

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def set_lines(self, dtr: bool | None, rts: bool | None) -> None:
        self.lines.append((dtr, rts))

    def close(self) -> None:
        self.closed = True


def listed_port(
    device: str,
    vid: int | None = None,
    pid: int | None = None,
    serial: str | None = None,
    manufacturer: str | None = None,
    product: str | None = None,
    resolved: str | None = None,
) -> serialx.SerialPortInfo:
    """A device as serialx lists it; `resolved` defaults to `device`."""
    return serialx.SerialPortInfo(
        device=device,
        resolved_device=resolved or device,
        vid=vid,
        pid=pid,
        serial_number=serial,
        manufacturer=manufacturer,
        product=product,
        bcd_device=None,
        interface_description=None,
        interface_num=None,
    )


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[serialx.SerialPortInfo]:
    """The devices serialx lists; tests append to it."""
    found: list[serialx.SerialPortInfo] = []
    monkeypatch.setattr(serialx, "list_serial_ports", lambda: list(found))
    return found


# A board's output read at another baud: high bytes and NUL, and now and then a line end.
NOISE = b"\xfb\x00\xf8\x80\xfe\n" * 8


@dataclass
class Board:
    """A device behind a fake serial port, and what was done to it."""

    # The baud it prints at; None prints nothing.
    baud: int | None = None
    text: bytes = b"[00:00:01.000,000] <inf> dcdc: rail=13.73V in=4.50A\r\n"
    # At another baud: NOISE, or nothing, as an ST-LINK V3 bridge gives.
    noise: bool = True
    # Rates the driver refuses.
    refuses: frozenset[int] = frozenset()
    open_error: Exception | None = None
    read_error: Exception | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    rates: list[int] = field(default_factory=list)
    writes: list[bytes] = field(default_factory=list)
    opened: list[float] = field(default_factory=list)
    closed: list[float] = field(default_factory=list)


class FakeSerial:
    """Stands in for serialx.Serial: its board prints at the board's baud, at once."""

    def __init__(self, board: Board, baudrate: int, **kwargs: Any) -> None:
        self.board = board
        self.baudrate = baudrate
        self.is_open = False
        board.settings = {"baudrate": baudrate, **kwargs}

    def open(self) -> None:
        if self.board.open_error is not None:
            raise self.board.open_error
        self.board.opened.append(time.monotonic())
        self.is_open = True

    def reconfigure_port(self, *, baudrate: int) -> None:
        if baudrate in self.board.refuses:
            raise UnsupportedSetting(f"baud {baudrate} is not supported")
        self.baudrate = baudrate
        self.board.rates.append(baudrate)

    def reset_read_buffer(self) -> None:
        pass

    def readinto(self, buf: bytearray, *, timeout: float) -> int:
        board = self.board
        if board.read_error is not None:
            raise board.read_error
        at_baud = self.baudrate == board.baud
        if board.baud is None or not (at_baud or board.noise):
            time.sleep(timeout)
            return 0
        data = board.text if at_baud else NOISE
        n = min(len(data), len(buf))
        buf[:n] = data[:n]
        return n

    def write(self, data: bytes, *, timeout: float | None = None) -> int:
        self.board.writes.append(bytes(data))
        return len(data)

    def set_modem_pins(self, **pins: Any) -> None:
        pass

    def close(self) -> None:
        self.board.closed.append(time.monotonic())
        self.is_open = False


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch) -> dict[str, Board]:
    """The fake devices serialx.Serial opens, by path; tests add them."""
    found: dict[str, Board] = {}

    def make(path: str, **kwargs: Any) -> FakeSerial:
        return FakeSerial(found[path], **kwargs)

    monkeypatch.setattr(serialx, "Serial", make)
    monkeypatch.setattr(transport, "_set_exclusive", lambda _port, _on: None)
    return found


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-configure's listening windows, short enough for a test, in real time."""
    monkeypatch.setattr(baud, "_WINDOW_S", 0.05)
    monkeypatch.setattr(baud, "_SETTLE_S", 0.005)


@pytest.fixture
def pty() -> Iterator[tuple[int, str]]:
    """A pseudo-terminal: its controller's fd and the path a port opens."""
    if sys.platform == "win32":
        pytest.skip("pseudo-terminals are POSIX only")
    controller, device = os.openpty()
    yield controller, os.ttyname(device)
    os.close(controller)
    os.close(device)


def run(name: str, **params: Any) -> Any:
    """What the action returns when the agent runs it with these parameters."""
    return getattr(actions, name)._action.execute(**params).value


def wait_until(condition: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


def read(port: Transport, size: int) -> bytes:
    """Read until `size` bytes arrived, or 2 s passed."""
    data, buf = b"", bytearray(64)
    deadline = time.monotonic() + 2
    while len(data) < size and time.monotonic() < deadline:
        data += buf[: port.readinto(buf, 0.1)]
    return data


def port_config(**changes: Any) -> PortConfig:
    """A TCP port `dut` at bench:2000 with every setting at its default; `changes` override."""
    fields: dict[str, Any] = {
        "name": "dut",
        "connection": "tcp",
        "port": None,
        "host": "bench",
        "tcp_port": 2000,
        "baud": 115200,
        "data_bits": 8,
        "parity": "none",
        "stop_bits": 1.0,
        "rtscts": False,
        "xonxoff": False,
        "dtr": True,
        "rts": True,
        "reset_line": "none",
        "line_ending": b"\n",
        "prompt": None,
        "values": True,
    }
    return PortConfig(**{**fields, **changes})
