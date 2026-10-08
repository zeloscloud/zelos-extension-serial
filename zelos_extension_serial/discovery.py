"""Serial devices on this machine."""

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import serialx

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
# Device IDs already warned about: resolve() runs on every reconnect.
_warned_duplicates: set[str] = set()
# On Windows the product is the driver's name for the port, which ends in the port:
# `USB Serial Device (COM4)`.
_COM_SUFFIX = re.compile(r" \(COM\d+\)$")


@dataclass(frozen=True, slots=True)
class PortInfo:
    """One listed serial device."""

    path: str
    # The device node `path` links to; Linux lists /dev/serial/by-id names as the path.
    resolved: str
    vid: int | None
    pid: int | None
    serial: str | None
    manufacturer: str | None
    product: str | None

    @property
    def device_id(self) -> str | None:
        """`usb:0403:6001:A50285BI`, hex lower case, only with a trusted serial."""
        # Windows makes up a 1 or 2 character serial for a device that has none.
        if self.vid is None or self.pid is None or self.serial is None or len(self.serial) < 4:
            return None
        return f"usb:{self.vid:04x}:{self.pid:04x}:{self.serial}"

    @property
    def choice(self) -> str:
        """What the Port field stores for this device: its device ID, else its path."""
        return self.device_id or self.path

    def has_path(self, path: str) -> bool:
        """Whether a typed `path` names this device."""
        return same_port(path, self.path) or same_port(path, self.resolved)

    def named_by(self, port_field: str) -> bool:
        """Whether a Port field names this device, by its device ID or one of its paths."""
        return port_field == self.device_id or self.has_path(port_field)


class PortNotFound(Exception):
    """A device ID matches no connected device."""

    def __init__(self, port: str, connected: list[str]) -> None:
        super().__init__(f"No device matches {port}. Connected: {', '.join(connected) or 'none'}.")


def list_ports() -> list[PortInfo]:
    """Every serial device on this machine, sorted by path."""
    ports = [
        PortInfo(
            p.device,
            p.resolved_device,
            p.vid,
            p.pid,
            p.serial_number,
            p.manufacturer,
            p.product and _COM_SUFFIX.sub("", p.product),
        )
        for p in serialx.list_serial_ports()
    ]
    return sorted(ports, key=lambda p: p.path)


def resolve(port_field: str) -> str:
    """A device ID to its current path; a path to itself."""
    if not _is_device_id(port_field):
        return port_field
    ports = list_ports()
    matches = [p.path for p in ports if p.device_id == port_field]
    if not matches:
        raise PortNotFound(port_field, [p.choice for p in ports])
    if len(matches) > 1 and port_field not in _warned_duplicates:
        _warned_duplicates.add(port_field)
        logger.warning("%s matches %s; using %s", port_field, ", ".join(matches), matches[0])
    return matches[0]


def present(port_field: str) -> bool:
    """Whether the device is connected: an ID is listed; a path exists, or on Windows is listed."""
    if _is_device_id(port_field):
        return any(p.device_id == port_field for p in list_ports())
    if not _WINDOWS:
        # Not the listing: Linux lists /dev/serial/by-id names, macOS lists only cu.* devices,
        # and neither lists pseudo-terminals. Unplugging removes the device node.
        return Path(port_field).exists()
    return any(p.has_path(port_field) for p in list_ports())


def same_port(a: str, b: str) -> bool:
    """Whether two paths name one port."""
    if not _WINDOWS:
        return a == b
    # Windows opens a COM port by any case, and serialx adds the \\.\ prefix itself.
    return _windows_key(a) == _windows_key(b)


def _windows_key(path: str) -> str:
    return path.removeprefix("\\\\.\\").casefold()


def _is_device_id(port_field: str) -> bool:
    return port_field.startswith("usb:")
