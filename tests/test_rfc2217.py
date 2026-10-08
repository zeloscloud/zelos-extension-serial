import contextlib
import dataclasses
import inspect
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal, cast

import pytest
from serialx.platforms.serial_rfc2217 import Rfc2217, RFC2217Serial

from tests.conftest import port_config, read
from zelos_extension_serial import transport
from zelos_extension_serial.config import PortConfig
from zelos_extension_serial.rfc2217 import FixedRFC2217Serial, NoRfc2217
from zelos_extension_serial.transport import Transport, classify, open_transport

# Telnet bytes and RFC 2217 command ids, as they appear on the wire.
IAC, SE, SB, WILL, DO, DONT = 255, 240, 250, 251, 253, 254
COM_PORT = 44
SET_BAUDRATE, SET_DATASIZE, SET_PARITY, SET_STOPSIZE, SET_CONTROL = 1, 2, 3, 4, 5
NOTIFY_MODEMSTATE, SET_LINESTATE_MASK, SET_MODEMSTATE_MASK = 7, 10, 11

RFC2217 = port_config(connection="rfc2217", host="127.0.0.1", tcp_port=2217)


class FakeSer2net:
    """ser2net 4.3.4 or 4.6.0 serving one client a device that prints a line every 10 ms."""

    # Like those versions, it answers SET-*-MASK with NOTIFY-MODEMSTATE and never acks it.
    # It acks every other command but `unacked`. `telnet=False` leaves negotiation unanswered, as a
    # raw TCP console does; `com_port` "refuse" or "ignore" plays telnet without RFC 2217.
    def __init__(
        self,
        *,
        telnet: bool = True,
        unacked: int | None = None,
        com_port: Literal["accept", "refuse", "ignore"] = "accept",
    ) -> None:
        self.commands: list[tuple[int, bytes]] = []
        # Every WILL, WONT, DO or DONT received, as (verb, option).
        self.options: list[tuple[int, int]] = []
        self._answered: set[tuple[int, int]] = set()
        self._telnet = telnet
        self._com_port = com_port
        self._unacked = unacked
        self._listener = socket.create_server(("127.0.0.1", 0))
        # accept() then fails instead of hanging when no client connects.
        self._listener.settimeout(5)
        self.port: int = self._listener.getsockname()[1]
        self.url = f"rfc2217://127.0.0.1:{self.port}"
        # The reply path and the printing device share the socket; a reply must not split a line.
        self._send_lock = threading.Lock()
        self._done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self._done.set()
        self._listener.close()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            threading.Thread(target=self._print_lines, args=(conn,), daemon=True).start()
            pending = b""
            # OSError: the client closed the connection.
            with contextlib.suppress(OSError):
                while chunk := conn.recv(4096):
                    pending = self._answer(conn, pending + chunk)
            self._done.set()

    def _send(self, conn: socket.socket, data: bytes) -> None:
        with self._send_lock:
            conn.sendall(data)

    def _print_lines(self, conn: socket.socket) -> None:
        seq = 0
        while not self._done.wait(0.01):
            try:
                self._send(conn, b"L%08d\n" % seq)
            except OSError:
                return
            seq += 1

    def _answer(self, conn: socket.socket, data: bytes) -> bytes:
        """Answer every whole command in `data`; return the incomplete rest."""
        while (start := data.find(IAC)) != -1:
            data = data[start:]
            if len(data) < 3:
                return data
            if data[1] == SB:
                end = data.find(bytes([IAC, SE]))
                if end == -1:
                    return data
                # Inside a subnegotiation, IAC IAC is one 0xFF data byte.
                self._command(conn, data[3], data[4:end].replace(b"\xff\xff", b"\xff"))
                data = data[end + 2 :]
                continue
            verb, option = data[1], data[2]
            self.options.append((verb, option))
            data = data[3:]
            # Like ser2net, it answers an option once: serialx acknowledges each answer with the
            # request again, and answering that too trades messages for ever.
            if (verb, option) in self._answered:
                continue
            self._answered.add((verb, option))
            if option == COM_PORT and self._com_port != "accept":
                if self._com_port == "refuse":
                    self._send(conn, bytes([IAC, DONT, COM_PORT]))
            elif verb in (WILL, DO) and self._telnet:
                self._send(conn, bytes([IAC, DO if verb == WILL else WILL, option]))
        return b""

    def _command(self, conn: socket.socket, cmd: int, payload: bytes) -> None:
        self.commands.append((cmd, payload))
        if cmd in (SET_LINESTATE_MASK, SET_MODEMSTATE_MASK):
            reply = bytes([NOTIFY_MODEMSTATE + 100, 0x30])
        elif cmd == self._unacked:
            return
        else:
            reply = bytes([cmd + 100]) + payload
        self._send(conn, bytes([IAC, SB, COM_PORT]) + reply + bytes([IAC, SE]))


@pytest.fixture
def ser2net() -> Iterator[Callable[..., FakeSer2net]]:
    """Starts fake servers and stops them after the test."""
    servers: list[FakeSer2net] = []

    def start(**options: Any) -> FakeSer2net:
        servers.append(FakeSer2net(**options))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def test_open_sends_the_settings_though_the_masks_are_never_acked(
    ser2net: Callable[..., FakeSer2net],
) -> None:
    server = ser2net()
    port = open_transport(
        dataclasses.replace(
            RFC2217,
            tcp_port=server.port,
            baud=57600,
            data_bits=7,
            parity="even",
            stop_bits=2.0,
            xonxoff=True,
        )
    )
    try:
        assert port.where == f"127.0.0.1:{server.port}"
        # No SET-CONTROL for DTR or RTS: the server's settings stay.
        assert server.commands == [
            (SET_MODEMSTATE_MASK, b"\xff"),
            (SET_LINESTATE_MASK, b"\x00"),
            (SET_BAUDRATE, (57600).to_bytes(4, "big")),
            (SET_DATASIZE, b"\x07"),
            (SET_PARITY, b"\x03"),
            (SET_STOPSIZE, b"\x02"),
            (SET_CONTROL, b"\x02"),
        ]
        assert read(port, 9).startswith(b"L000000")
    finally:
        port.close()


def test_open_url_brackets_an_ipv6_host(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class Recording:
        _socket = object()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            made.append((args, kwargs))

        def open(self) -> None:
            pass

    monkeypatch.setattr(transport, "FixedRFC2217Serial", Recording)
    monkeypatch.setattr(transport, "_keep_alive", lambda _sock: None)
    port = open_transport(dataclasses.replace(RFC2217, host="::1", tcp_port=4002))
    assert port.where == "[::1]:4002"
    [(args, kwargs)] = made
    assert args == ("rfc2217://[::1]:4002",)
    assert (kwargs["connect_timeout"], kwargs["write_timeout"]) == (2.0, 1.0)


@pytest.mark.parametrize(
    ("silence", "raised_type"),
    [({"telnet": False}, NoRfc2217), ({"unacked": SET_BAUDRATE}, TimeoutError)],
)
def test_an_unanswered_wait_ends_at_the_deadline_while_the_device_prints(
    ser2net: Callable[..., FakeSer2net], silence: dict[str, Any], raised_type: type
) -> None:
    server = ser2net(**silence)
    port = FixedRFC2217Serial(server.url, connect_timeout=0.5)
    raised: list[BaseException] = []

    def open_port() -> None:
        try:
            port.open()
        except BaseException as exc:
            raised.append(exc)

    started = time.monotonic()
    # A thread, so a wait that never ends fails the test instead of hanging it.
    waiter = threading.Thread(target=open_port, daemon=True)
    waiter.start()
    waiter.join(3)
    assert not waiter.is_alive(), "the wait outlived its deadline"
    assert time.monotonic() - started < 1.5
    assert [type(exc) for exc in raised] == [raised_type]
    port.close()


@pytest.mark.parametrize(
    "server",
    [{"telnet": False}, {"com_port": "refuse"}, {"com_port": "ignore"}],
    ids=["raw TCP console", "telnet refusing RFC 2217", "telnet ignoring RFC 2217"],
)
def test_a_server_without_rfc2217_is_told_apart_from_one_that_is_down(
    ser2net: Callable[..., FakeSer2net], monkeypatch: pytest.MonkeyPatch, server: dict[str, Any]
) -> None:
    monkeypatch.setattr(transport, "CONNECT_TIMEOUT_S", 0.3)
    started = time.monotonic()
    with pytest.raises(NoRfc2217) as caught:
        open_transport(dataclasses.replace(RFC2217, tcp_port=ser2net(**server).port))
    assert time.monotonic() - started < 1.5
    assert classify(caught.value) == "no_rfc2217"


def test_option_negotiation_goes_quiet_once_agreed(ser2net: Callable[..., FakeSer2net]) -> None:
    server = ser2net()
    port = open_transport(dataclasses.replace(RFC2217, tcp_port=server.port))
    try:
        # Reading is what answers the server's option messages.
        _sequence(port, 0.2)
        agreed = len(server.options)
        _sequence(port, 0.2)
        assert len(server.options) == agreed
        assert agreed <= 5
    finally:
        port.close()


def test_rfc2217_keeps_the_link_alive(ser2net: Callable[..., FakeSer2net]) -> None:
    port = open_transport(dataclasses.replace(RFC2217, tcp_port=ser2net().port))
    try:
        sock = cast(Any, port)._port._socket
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    finally:
        port.close()


@pytest.mark.parametrize(
    ("owner", "name", "signature"),
    [
        (
            RFC2217Serial,
            "_send_command",
            "(self, cmd: 'TelnetCommand | Rfc2217Command', responses: 'list[TelnetCommand] "
            "| None' = None, timeout: 'float | None' = None) -> 'TelnetCommand | None'",
        ),
        (RFC2217Serial, "_send_and_wait", "(self, cmd: 'Rfc2217Command') -> 'Rfc2217Command'"),
        (RFC2217Serial, "_negotiate", "(self) -> 'None'"),
        (RFC2217Serial, "_recv_and_process", "(self) -> 'None'"),
        (
            RFC2217Serial,
            "_socket_timeout",
            "(self, timeout: 'float | None') -> 'Generator[float | None, None, None]'",
        ),
        (
            Rfc2217,
            "pop_matching_telnet",
            "(self, expected: 'list[TelnetCommand]') -> 'TelnetCommand | None'",
        ),
        (
            Rfc2217,
            "pop_pending_rfc2217",
            "(self, cmd_id: 'Rfc2217CmdId') -> 'Rfc2217Command | None'",
        ),
    ],
)
def test_the_serialx_internals_the_fixes_rely_on_are_unchanged(
    owner: type, name: str, signature: str
) -> None:
    assert str(inspect.signature(getattr(owner, name))) == signature


# Against a real ser2net that serves a pseudo-terminal pair at SER2NET_HOST:SER2NET_PORT as
# RFC 2217, while a writer prints `L<8 digits>` lines into SER2NET_PEER about every 5 ms.


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"set {name} to run against ser2net")
    return value


@pytest.fixture
def real() -> PortConfig:
    """The port config for the ser2net under test."""
    return dataclasses.replace(
        RFC2217, host=_env("SER2NET_HOST"), tcp_port=int(_env("SER2NET_PORT"))
    )


def _sequence(port: Transport, seconds: float) -> list[int]:
    """The numbers of the whole lines read in `seconds`."""
    data, buf = bytearray(), bytearray(4096)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        data += buf[: port.readinto(buf, 0.1)]
    # The first and last pieces can be partial lines.
    lines = bytes(data).split(b"\n")[1:-1]
    assert all(re.fullmatch(rb"L\d{8}", line) for line in lines), lines[:3]
    return [int(line[1:]) for line in lines]


def test_ser2net_streams_5_s_of_lines_without_a_gap(real: PortConfig) -> None:
    port = open_transport(real)
    try:
        # What the pseudo-terminal held while no client was connected.
        _sequence(port, 0.5)
        seq = _sequence(port, 5.0)
    finally:
        port.close()
    assert len(seq) > 500
    assert seq == list(range(seq[0], seq[0] + len(seq)))


def test_ser2net_applies_the_baud_on_every_reopen(real: PortConfig) -> None:
    if sys.platform == "win32":
        pytest.skip("ser2net serves a POSIX pseudo-terminal")
    import termios

    device = _env("SER2NET_DEVICE")

    for baud in (9600, 57600, 115200):
        port = open_transport(dataclasses.replace(real, baud=baud))
        try:
            # Root opens a device that ser2net holds exclusively only with CAP_SYS_ADMIN.
            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
            try:
                assert termios.tcgetattr(fd)[5] == getattr(termios, f"B{baud}")
            finally:
                os.close(fd)
            assert _sequence(port, 0.5)
        finally:
            port.close()


def test_ser2net_delivers_a_write(real: PortConfig) -> None:
    if sys.platform == "win32":
        pytest.skip("ser2net serves a POSIX pseudo-terminal")
    peer = os.open(_env("SER2NET_PEER"), os.O_RDONLY | os.O_NONBLOCK)
    # 0xFF is telnet's IAC; the client must escape it.
    token = b"\xffzelos-%d\xff\n" % os.getpid()
    port = open_transport(real)
    try:
        port.write(token)
        got = b""
        deadline = time.monotonic() + 2
        while token not in got and time.monotonic() < deadline:
            if select.select([peer], [], [], 0.1)[0]:
                got += os.read(peer, 4096)
        assert token in got
    finally:
        port.close()
        os.close(peer)


# Last: nothing restarts the server.
def test_ser2net_killed_fails_a_read_within_1_s(real: PortConfig) -> None:
    kill = _env("SER2NET_KILL")
    port = open_transport(real)
    try:
        _sequence(port, 0.3)
        subprocess.run(kill, shell=True, check=True)
        killed = time.monotonic()
        buf = bytearray(4096)
        with pytest.raises(OSError):
            while time.monotonic() - killed < 5:
                port.readinto(buf, 0.1)
        assert time.monotonic() - killed < 1.0
    finally:
        port.close()
