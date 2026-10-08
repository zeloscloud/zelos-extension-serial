import dataclasses
import errno
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
import serialx
from serialx import Parity, PinState, UnsupportedSetting

from tests.conftest import port_config, read, wait_until
from zelos_extension_serial import transport
from zelos_extension_serial.discovery import PortNotFound
from zelos_extension_serial.rfc2217 import NoRfc2217
from zelos_extension_serial.transport import (
    Fault,
    SerialxTransport,
    Transport,
    UnresolvedHost,
    WriteTimeout,
    classify,
    open_transport,
)

SERIAL = port_config(connection="serial", port="/dev/ttyUSB0", host=None, tcp_port=None)


class RecordingSerial:
    """Stands in for serialx.Serial: records the constructor call and every later call."""

    made: list["RecordingSerial"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict[str, Any]]] = []
        RecordingSerial.made.append(self)

    def open(self) -> None:
        self.calls.append(("open", {}))

    def set_modem_pins(self, **pins: Any) -> None:
        self.calls.append(("set_modem_pins", pins))

    def close(self) -> None:
        self.calls.append(("close", {}))

    @property
    def is_open(self) -> bool:
        return ("close", {}) not in self.calls


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[RecordingSerial]:
    """Every serialx.Serial that open_transport constructs."""
    RecordingSerial.made = []
    monkeypatch.setattr(serialx, "Serial", RecordingSerial)

    def set_exclusive(port: RecordingSerial, on: bool) -> None:
        port.calls.append(("exclusive", {"on": on}))

    monkeypatch.setattr(transport, "_set_exclusive", set_exclusive)
    return RecordingSerial.made


def test_serial_opens_with_every_setting(made: list[RecordingSerial]) -> None:
    port = open_transport(
        dataclasses.replace(
            SERIAL, baud=57600, data_bits=7, parity="even", stop_bits=2.0, rtscts=True, dtr=False
        )
    )
    assert port.where == "/dev/ttyUSB0"
    [serial] = made
    assert serial.args == ("/dev/ttyUSB0",)
    assert serial.kwargs == {
        "baudrate": 57600,
        "byte_size": 7,
        "parity": Parity.EVEN,
        "stopbits": 2.0,
        "rtscts": True,
        "xonxoff": False,
        "exclusive": True,
        "write_timeout": 1.0,
        "dtr_on_open": PinState.LOW,
        "rts_on_open": PinState.HIGH,
        "dtr_on_close": PinState.UNDEFINED,
        "rts_on_close": PinState.UNDEFINED,
    }
    assert serial.calls == [("open", {}), ("exclusive", {"on": True})]


def test_serial_gives_up_exclusive_access_before_it_closes(made: list[RecordingSerial]) -> None:
    port = open_transport(SERIAL)
    port.close()
    port.close()
    assert made[0].calls[1:] == [
        ("exclusive", {"on": True}),
        ("exclusive", {"on": False}),
        ("close", {}),
        ("close", {}),
    ]


@pytest.mark.parametrize(
    ("parity", "expected"),
    [
        ("none", Parity.NONE),
        ("even", Parity.EVEN),
        ("odd", Parity.ODD),
        ("mark", Parity.MARK),
        ("space", Parity.SPACE),
    ],
)
def test_parity_maps_to_serialx(made: list[RecordingSerial], parity: str, expected: Parity) -> None:
    open_transport(dataclasses.replace(SERIAL, parity=parity))
    assert made[0].kwargs["parity"] is expected


def test_flow_control_and_lines_map_to_serialx(made: list[RecordingSerial]) -> None:
    open_transport(dataclasses.replace(SERIAL, xonxoff=True, stop_bits=1.5, dtr=True, rts=False))
    kwargs = made[0].kwargs
    assert (kwargs["xonxoff"], kwargs["rtscts"], kwargs["stopbits"]) == (True, False, 1.5)
    assert (kwargs["dtr_on_open"], kwargs["rts_on_open"]) == (PinState.HIGH, PinState.LOW)


def test_serial_opens_the_path_of_a_device_id(
    made: list[RecordingSerial], monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = serialx.SerialPortInfo("COM7", "COM7", 0x0403, 0x6001, "A50285BI", *[None] * 5)
    monkeypatch.setattr(serialx, "list_serial_ports", lambda: [listed])
    port = open_transport(dataclasses.replace(SERIAL, port="usb:0403:6001:A50285BI"))
    assert made[0].args == ("COM7",)
    assert port.where == "COM7"


def test_set_lines_leaves_a_none_line_alone(made: list[RecordingSerial]) -> None:
    port = open_transport(SERIAL)
    port.set_lines(False, None)
    port.set_lines(None, True)
    assert made[0].calls[2:] == [
        ("set_modem_pins", {"dtr": False, "rts": None}),
        ("set_modem_pins", {"dtr": None, "rts": True}),
    ]
    # serialx reads None as UNDEFINED, which leaves the pin untouched.
    assert PinState.convert(None) is PinState.UNDEFINED


class SlowSerial(RecordingSerial):
    """Takes every write whole, until `accepts` bytes went out; then times out."""

    accepts = 10**9
    written = 0

    def write(self, data: bytes, *, timeout: float) -> int:
        self.calls.append(("write", {"size": len(data), "timeout": timeout}))
        if self.written + len(data) > self.accepts:
            raise TimeoutError("Write timeout")
        self.written += len(data)
        return len(data)


def test_a_serial_write_has_time_for_its_bytes_at_the_baud(
    made: list[RecordingSerial], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serialx, "Serial", SlowSerial)
    port = open_transport(dataclasses.replace(SERIAL, baud=9600))

    assert port.write(b"x" * 2000) == 2000

    writes = [args for name, args in made[0].calls if name == "write"]
    assert sum(w["size"] for w in writes) == 2000
    # 8N1 is 10 bits a byte: 2000 bytes take 2.08 s at 9600 baud, plus the 1 s margin.
    assert writes[0]["timeout"] == pytest.approx(1.0 + 2000 * 10 / 9600, abs=0.05)


@pytest.mark.parametrize(
    ("framing", "bits"),
    [
        ({}, 10),
        ({"data_bits": 7, "parity": "even", "stop_bits": 2.0}, 11),
        ({"data_bits": 5, "stop_bits": 1.5}, 7.5),
    ],
)
def test_the_write_time_counts_every_bit_of_the_framing(
    made: list[RecordingSerial],
    monkeypatch: pytest.MonkeyPatch,
    framing: dict[str, Any],
    bits: float,
) -> None:
    monkeypatch.setattr(serialx, "Serial", SlowSerial)
    port = open_transport(dataclasses.replace(SERIAL, baud=100, **framing))

    port.write(b"x")

    [write] = [args for name, args in made[0].calls if name == "write"]
    assert write["timeout"] == pytest.approx(1.0 + bits / 100, abs=0.004)


def test_a_timed_out_write_says_how_many_bytes_went_out(
    made: list[RecordingSerial], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serialx, "Serial", SlowSerial)
    monkeypatch.setattr(SlowSerial, "accepts", 600)
    port = open_transport(SERIAL)

    with pytest.raises(WriteTimeout) as caught:
        port.write(b"x" * 2000)

    assert caught.value.written == 512
    assert isinstance(caught.value, TimeoutError)
    assert str(caught.value) == "write timed out after 512 bytes"


def test_a_write_with_no_time_left_stops_before_the_next_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stalling(SlowSerial):
        def write(self, data: bytes, *, timeout: float) -> int:
            time.sleep(0.2)
            return super().write(data, timeout=timeout)

    monkeypatch.setattr(transport, "WRITE_TIMEOUT_S", 0.1)
    port = SerialxTransport(cast(Any, Stalling()), "x")

    with pytest.raises(WriteTimeout) as caught:
        port.write(b"x" * 1000)

    assert caught.value.written == 256


def test_demo_is_the_demo_device(monkeypatch: pytest.MonkeyPatch) -> None:
    class Demo:
        where = "demo"

    monkeypatch.setattr(transport, "DemoDevice", Demo)
    assert isinstance(open_transport(port_config(connection="demo")), Demo)


def test_tcp_url_brackets_an_ipv6_host(
    made: list[RecordingSerial], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "SocketSerial", RecordingSerial)
    monkeypatch.setattr(transport, "_keep_alive", lambda _sock: None)
    monkeypatch.setattr(RecordingSerial, "_socket", object(), raising=False)
    port = open_transport(port_config(host="::1", tcp_port=4001))
    assert made[0].args == ("socket://[::1]:4001",)
    assert made[0].kwargs == {"connect_timeout": 2.0, "write_timeout": 1.0}
    assert port.where == "[::1]:4001"


@pytest.fixture
def server() -> Iterator[socket.socket]:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        # accept() then fails instead of hanging when no client connects.
        listener.settimeout(5)
        yield listener


def _tcp(listener: socket.socket) -> Transport:
    host, port = listener.getsockname()
    return open_transport(port_config(host=host, tcp_port=port))


def test_tcp_reads_writes_and_times_out(server: socket.socket) -> None:
    port = _tcp(server)
    peer, _ = server.accept()
    with peer:
        assert port.where == f"127.0.0.1:{server.getsockname()[1]}"
        started = time.monotonic()
        assert port.readinto(bytearray(64), 0.2) == 0
        assert time.monotonic() - started >= 0.15
        peer.sendall(b"boot ok\n")
        assert read(port, 8) == b"boot ok\n"
        assert port.write(b"help\n") == 5
        assert peer.recv(64) == b"help\n"
    port.close()


def test_tcp_read_raises_when_the_peer_closes(server: socket.socket) -> None:
    port = _tcp(server)
    peer, _ = server.accept()
    peer.close()
    with pytest.raises(OSError):
        read(port, 1)
    port.close()


def test_tcp_nothing_listening_is_refused() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        free = listener.getsockname()[1]
    with pytest.raises(OSError) as caught:
        open_transport(port_config(host="127.0.0.1", tcp_port=free))
    assert classify(caught.value) == "refused"


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Host name to its addresses, an exception to raise, or an Event the lookup waits on."""
    answers: dict[str, Any] = {}
    real = socket.getaddrinfo

    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        answer = answers.get(host)
        if answer is None:
            return real(host, port, *args, **kwargs)
        if isinstance(answer, threading.Event):
            answer.wait(5)
            raise socket.gaierror(socket.EAI_AGAIN, "timed out")
        if isinstance(answer, BaseException):
            raise answer
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port)) for family, ip in answer]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    threads = threading.active_count()
    yield answers
    for answer in answers.values():
        if isinstance(answer, threading.Event):
            answer.set()
    # A stranded lookup thread would skew the thread counts later tests take.
    wait_until(lambda: threading.active_count() == threads)


def test_a_host_lookup_that_hangs_is_unresolved_within_the_connect_timeout(
    lookups: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "CONNECT_TIMEOUT_S", 0.3)
    lookups["bench.local"] = threading.Event()
    started = time.monotonic()
    with pytest.raises(UnresolvedHost) as caught:
        open_transport(port_config(host="bench.local", tcp_port=4001))
    assert 0.25 <= time.monotonic() - started < 1.0
    assert classify(caught.value) == "unresolved"


def test_an_unknown_host_is_unresolved(lookups: dict[str, Any]) -> None:
    lookups["nowhere.invalid"] = socket.gaierror(socket.EAI_NONAME, "not known")
    with pytest.raises(UnresolvedHost, match="^cannot resolve nowhere.invalid$") as caught:
        open_transport(port_config(host="nowhere.invalid", tcp_port=4001))
    assert classify(caught.value) == "unresolved"


def test_a_host_name_connects_to_the_address_that_answers(
    lookups: dict[str, Any], server: socket.socket
) -> None:
    # Like localhost on most machines: IPv6 first, while the server listens on IPv4 only.
    lookups["bench.local"] = [(socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")]
    tcp_port = server.getsockname()[1]
    port = open_transport(port_config(host="bench.local", tcp_port=tcp_port))
    peer, _ = server.accept()
    with peer:
        assert port.where == f"bench.local:{tcp_port}"
        peer.sendall(b"ok\n")
        assert read(port, 3) == b"ok\n"
    port.close()


def _socket_of(port: Transport) -> socket.socket:
    return cast(Any, port)._port._socket


def test_tcp_keeps_the_link_alive(server: socket.socket) -> None:
    """A peer that vanishes without a FIN or RST (power loss, a pulled cable) fails a read."""
    port = _tcp(server)
    server.accept()[0].close()
    sock = _socket_of(port)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        if sys.platform == "win32":
            return
        idle = socket.TCP_KEEPALIVE if sys.platform == "darwin" else socket.TCP_KEEPIDLE
        options = (idle, socket.TCP_KEEPINTVL, socket.TCP_KEEPCNT)
        # About 30 s from the last byte to the failed read.
        assert [sock.getsockopt(socket.IPPROTO_TCP, o) for o in options] == [10, 5, 4]
        if sys.platform == "linux":
            # The same 30 s while sent bytes wait for an ack, which stops Linux's probes.
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 30_000
    finally:
        port.close()


@pytest.fixture
def pty() -> Iterator[tuple[int, str]]:
    """A pseudo-terminal: its controller's fd and the path a port opens."""
    if sys.platform == "win32":
        pytest.skip("pseudo-terminals are POSIX only")
    controller, device = os.openpty()
    yield controller, os.ttyname(device)
    os.close(controller)
    os.close(device)


def test_serial_reads_and_times_out_on_a_pty(pty: tuple[int, str]) -> None:
    controller, path = pty
    port = open_transport(dataclasses.replace(SERIAL, port=path))
    try:
        assert port.where == path
        started = time.monotonic()
        assert port.readinto(bytearray(64), 0.2) == 0
        assert time.monotonic() - started >= 0.15
        os.write(controller, b"uart:~$ ")
        assert read(port, 8) == b"uart:~$ "
    finally:
        port.close()


def test_serial_holds_the_port_exclusively(pty: tuple[int, str]) -> None:
    _, path = pty
    port = open_transport(dataclasses.replace(SERIAL, port=path))
    try:
        with pytest.raises(OSError) as caught:
            open_transport(dataclasses.replace(SERIAL, port=path))
        assert classify(caught.value) == "busy"
    finally:
        port.close()


def test_serial_keeps_out_a_reader_that_takes_no_lock(pty: tuple[int, str]) -> None:
    if sys.platform != "linux":
        pytest.skip("macOS pseudo-terminals ignore TIOCEXCL")
    _, path = pty
    # As cat or screen open it: no lock.
    flags = os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK
    port = open_transport(dataclasses.replace(SERIAL, port=path))
    try:
        with pytest.raises(OSError) as caught:
            os.open(path, flags)
        assert caught.value.errno == errno.EBUSY
    finally:
        port.close()
    # The fixture still holds the device open, so only clearing TIOCEXCL lets the next reader in.
    os.close(os.open(path, flags))


def _windows(exc: OSError, winerror: int) -> OSError:
    # typeshed declares OSError.winerror on Windows only; every platform accepts the attribute.
    setattr(exc, "winerror", winerror)  # noqa: B010
    return exc


@pytest.mark.parametrize(
    ("exc", "fault"),
    [
        (PermissionError(errno.EACCES, "Permission denied"), "permission"),
        (
            OSError(errno.EBUSY, "Serial port '/dev/ttyUSB0' is already locked by another process"),
            "busy",
        ),
        (_windows(PermissionError(errno.EACCES, "Access is denied."), 5), "in_use_or_denied"),
        (FileNotFoundError(errno.ENOENT, "No such file or directory"), "not_found"),
        (
            _windows(FileNotFoundError(errno.ENOENT, "The system cannot find the file"), 2),
            "not_found",
        ),
        (PortNotFound("usb:0403:6001:A50285BI", ["COM3"]), "not_found"),
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "refused"),
        (_windows(ConnectionRefusedError(errno.ECONNREFUSED, "Refused"), 10061), "refused"),
        (TimeoutError("timed out"), "refused"),
        # EAI_AGAIN on macOS, the same number as ENOENT.
        (socket.gaierror(2, "Temporary failure in name resolution"), "unresolved"),
        (UnresolvedHost("bench.local"), "unresolved"),
        (NoRfc2217("the server does not answer RFC 2217"), "no_rfc2217"),
        (UnsupportedSetting("1.5 stop bits"), "config"),
        (ValueError("Invalid socket URI, expected both host and port"), "config"),
        # SetCommState refusing a baud the driver does not support.
        (_windows(OSError(errno.EINVAL, "A device attached is not functioning."), 31), "other"),
        (OSError(errno.EIO, "Input/output error"), "other"),
        (OSError(errno.EINVAL, "Invalid argument"), "other"),
        (RuntimeError("boom"), "other"),
    ],
)
def test_classify(exc: BaseException, fault: Fault) -> None:
    assert classify(exc) == fault
