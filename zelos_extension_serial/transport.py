"""The byte pipe a worker owns, and its faults."""

import contextlib
import errno
import socket
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any, Literal, Protocol

import serialx
from serialx import Parity, PinState, UnsupportedSetting
from serialx.platforms.serial_socket import SocketSerial

from .config import PortConfig
from .demo import DemoDevice
from .discovery import PortNotFound, resolve
from .rfc2217 import FixedRFC2217Serial, NoRfc2217

if sys.platform != "win32":
    import fcntl
    import termios


class Transport(Protocol):
    """An open port, touched only by its worker's thread.

    The one exception: stop() closes a port blocked in a write from another thread.
    """

    where: str

    def readinto(self, buf: bytearray, timeout: float) -> int:
        """Read into `buf`, waiting at most `timeout` seconds; 0 on timeout."""
        ...

    def write(self, data: bytes) -> int:
        """Write all of `data` and return its length; raise WriteTimeout when time runs out."""
        ...

    def set_lines(self, dtr: bool | None, rts: bool | None) -> None:
        """Drive DTR and RTS; None leaves a line as it is."""
        ...

    def close(self) -> None:
        """Close the port."""
        ...


Fault = Literal[
    "permission",
    "busy",
    "in_use_or_denied",
    "not_found",
    "refused",
    "unresolved",
    "no_rfc2217",
    "config",
    "other",
]

# A write may take this long beyond the time its bytes need on the wire at the port's baud.
WRITE_TIMEOUT_S = 1.0
# Short, because a blocked connect holds the port's thread: Get State and stop wait behind it.
# It also bounds the host-name lookup and every RFC 2217 reply wait.
CONNECT_TIMEOUT_S = 2.0
# Written a piece at a time, so a timed-out write knows how much went out.
_WRITE_CHUNK = 256
# A dead peer fails a read about 10 + 5 * 4 = 30 s after the last byte.
_KEEPALIVE_IDLE_S, _KEEPALIVE_INTERVAL_S, _KEEPALIVE_PROBES = 10, 5, 4


class UnresolvedHost(Exception):
    """A host name that cannot be looked up in time."""

    def __init__(self, host: str) -> None:
        super().__init__(f"cannot resolve {host}")


class WriteTimeout(TimeoutError):
    """A write ran out of time after `written` of its bytes went out."""

    def __init__(self, written: int) -> None:
        super().__init__(f"write timed out after {written} bytes")
        self.written = written


class SerialxTransport:
    """A serialx port behind the Transport protocol."""

    def __init__(
        self,
        port: serialx.BaseSerial,
        where: str,
        *,
        seconds_per_byte: float = 0.0,
        exclusive: bool = False,
    ) -> None:
        self._port = port
        self.where = where
        self._seconds_per_byte = seconds_per_byte
        self._exclusive = exclusive

    def readinto(self, buf: bytearray, timeout: float) -> int:
        return self._port.readinto(buf, timeout=timeout)

    def write(self, data: bytes) -> int:
        # serialx times a whole write, so a long one at a low baud needs its wire time on top.
        deadline = time.monotonic() + WRITE_TIMEOUT_S + len(data) * self._seconds_per_byte
        written = 0
        while written < len(data):
            remaining = deadline - time.monotonic()
            # Checked first: serialx reads a timeout of 0 as "do not wait".
            if remaining <= 0:
                raise WriteTimeout(written)
            try:
                written += self._port.write(
                    data[written : written + _WRITE_CHUNK], timeout=remaining
                )
            except TimeoutError:
                raise WriteTimeout(written) from None
        return written

    def set_lines(self, dtr: bool | None, rts: bool | None) -> None:
        self._port.set_modem_pins(dtr=dtr, rts=rts)

    def close(self) -> None:
        if self._exclusive and self._port.is_open:
            _set_exclusive(self._port, False)
        self._port.close()


def address(cfg: PortConfig) -> str:
    """`host:port` for a network port."""
    assert cfg.host is not None
    return f"{_url_host(cfg.host)}:{cfg.tcp_port}"


def _url_host(host: str) -> str:
    # A URL needs an IPv6 address in brackets.
    return f"[{host}]" if ":" in host else host


def framing(cfg: PortConfig) -> dict[str, Any]:
    """serialx's baud, framing and flow-control arguments."""
    return {
        "baudrate": cfg.baud,
        "byte_size": cfg.data_bits,
        "parity": Parity[cfg.parity.upper()],
        "stopbits": cfg.stop_bits,
        "rtscts": cfg.rtscts,
        "xonxoff": cfg.xonxoff,
    }


def open_transport(cfg: PortConfig) -> Transport:
    """Open the port `cfg` describes."""
    if cfg.connection == "demo":
        return DemoDevice()
    if cfg.connection != "serial":
        return _open_network(cfg)
    assert cfg.port is not None
    where = resolve(cfg.port)
    # serialx types Serial as its abstract base; at run time it is this platform's class.
    port = serialx.Serial(  # pyright: ignore[reportAbstractUsage]
        where,
        **framing(cfg),
        exclusive=True,
        write_timeout=WRITE_TIMEOUT_S,
        dtr_on_open=PinState.convert(cfg.dtr),
        rts_on_open=PinState.convert(cfg.rts),
        # serialx lowers both lines at close by default, an edge that can reset a board.
        # UNDEFINED leaves them as they are; POSIX refuses unequal close values at open().
        dtr_on_close=PinState.UNDEFINED,
        rts_on_close=PinState.UNDEFINED,
    )
    # serialx objects open only when asked; a failed open() closes itself.
    port.open()
    _set_exclusive(port, True)
    # A start bit, the data bits, the parity bit if any, and the stop bits.
    bits = 1 + cfg.data_bits + (cfg.parity != "none") + cfg.stop_bits
    return SerialxTransport(port, where, seconds_per_byte=bits / cfg.baud, exclusive=True)


def _open_network(cfg: PortConfig) -> Transport:
    """Connect to the first address of the host that answers, as socket.create_connection does."""
    assert cfg.host is not None
    last: OSError | None = None
    for ip in _addresses(cfg.host, cfg.tcp_port):
        url = f"{_url_host(ip)}:{cfg.tcp_port}"
        if cfg.connection == "rfc2217":
            # Opening sends no DTR or RTS: they stay as the server has them.
            port: SocketSerial = FixedRFC2217Serial(
                f"rfc2217://{url}",
                **framing(cfg),
                connect_timeout=CONNECT_TIMEOUT_S,
                write_timeout=WRITE_TIMEOUT_S,
            )
        else:
            port = SocketSerial(
                f"socket://{url}", connect_timeout=CONNECT_TIMEOUT_S, write_timeout=WRITE_TIMEOUT_S
            )
        try:
            port.open()
        except OSError as e:
            last = e
            continue
        assert port._socket is not None
        _keep_alive(port._socket)
        # The host as typed, not the address it resolved to.
        return SerialxTransport(port, address(cfg))
    assert last is not None, "getaddrinfo returns an address or raises"
    raise last


def _addresses(host: str, port: int | None) -> list[str]:
    """The addresses of `host`, looked up within CONNECT_TIMEOUT_S."""
    # getaddrinfo cannot be interrupted, and a .local name can block it for 5 s, so it runs on its
    # own thread; one that outlives the wait ends on its own.
    found: Future[list[Any]] = Future()

    def look_up() -> None:
        try:
            found.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as e:
            found.set_exception(e)

    threading.Thread(target=look_up, name=f"look up {host}", daemon=True).start()
    try:
        infos = found.result(timeout=CONNECT_TIMEOUT_S)
    except (TimeoutError, OSError) as e:
        raise UnresolvedHost(host) from e
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _keep_alive(sock: socket.socket) -> None:
    """Probe an idle link, so a peer that vanished without closing fails a read."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if sys.platform == "win32":
        # Windows sends 10 probes, so a dead peer fails a read after about 60 s.
        keepalive = (1, _KEEPALIVE_IDLE_S * 1000, _KEEPALIVE_INTERVAL_S * 1000)
        sock.ioctl(socket.SIO_KEEPALIVE_VALS, keepalive)
        return
    idle = socket.TCP_KEEPALIVE if sys.platform == "darwin" else socket.TCP_KEEPIDLE
    sock.setsockopt(socket.IPPROTO_TCP, idle, _KEEPALIVE_IDLE_S)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPALIVE_INTERVAL_S)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPALIVE_PROBES)
    if sys.platform == "linux":
        # Linux sends no probes while sent bytes wait for an ack, and retransmits them for about
        # 17 min instead; this gives up on them after the same 30 s.
        budget_s = _KEEPALIVE_IDLE_S + _KEEPALIVE_INTERVAL_S * _KEEPALIVE_PROBES
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, budget_s * 1000)


def _set_exclusive(port: serialx.BaseSerial, on: bool) -> None:
    """Refuse, or allow again, other opens of the device, by programs that take no lock too."""
    # serialx's exclusive=True is an flock, which only programs that also lock respect.
    # Windows opens a port exclusively already.
    if sys.platform == "win32":
        return
    # Best effort: the flock still keeps out programs that lock, and an unplugged device fails
    # the ioctl but must still close.
    with contextlib.suppress(OSError):
        fcntl.ioctl(port.fileno(), termios.TIOCEXCL if on else termios.TIOCNXCL)


_WINERRORS: dict[int, Fault] = {
    # ERROR_ACCESS_DENIED: another program holds the port, or the user may not open it.
    # CreateFile reports both the same way.
    5: "in_use_or_denied",
    2: "not_found",  # ERROR_FILE_NOT_FOUND
}

_ERRNOS: dict[int, Fault] = {
    errno.EACCES: "permission",
    # serialx reports a port another process has locked as EBUSY, "already locked".
    errno.EBUSY: "busy",
    errno.ENOENT: "not_found",
}


def classify(exc: BaseException) -> Fault:
    """Map a failed open to a fault."""
    # Before errno: on macOS a gaierror's EAI_AGAIN is 2, which is ENOENT.
    if isinstance(exc, UnresolvedHost | socket.gaierror):
        return "unresolved"
    if isinstance(exc, NoRfc2217):
        return "no_rfc2217"
    if isinstance(exc, PortNotFound):
        return "not_found"
    if isinstance(exc, UnsupportedSetting | ValueError):
        return "config"
    # Only a failed open reaches here, so a timeout is a connect timeout. Before the Windows
    # codes: a refused connect carries winerror 10061 there.
    if isinstance(exc, ConnectionRefusedError | TimeoutError):
        return "refused"
    if not isinstance(exc, OSError):
        return "other"
    winerror = getattr(exc, "winerror", None)
    # Before errno: Windows derives errno EACCES from both of its access-denied causes.
    if winerror is not None:
        return _WINERRORS.get(winerror, "other")
    return _ERRNOS.get(exc.errno or 0, "other")
