import logging
from pathlib import Path

import pytest
import serialx

from zelos_extension_serial import discovery
from zelos_extension_serial.discovery import (
    PortInfo,
    PortNotFound,
    list_ports,
    present,
    resolve,
    same_port,
)

FTDI = "usb:0403:6001:A50285BI"


def _listed(
    device: str,
    vid: int | None = None,
    pid: int | None = None,
    serial: str | None = None,
    resolved: str | None = None,
) -> serialx.SerialPortInfo:
    return serialx.SerialPortInfo(
        device=device,
        resolved_device=resolved or device,
        vid=vid,
        pid=pid,
        serial_number=serial,
        manufacturer="FTDI",
        product="FT232R USB UART",
        bcd_device=None,
        interface_description=None,
        interface_num=None,
    )


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[serialx.SerialPortInfo]:
    """What serialx lists; tests append devices to it."""
    ports: list[serialx.SerialPortInfo] = []
    monkeypatch.setattr(serialx, "list_serial_ports", lambda: list(ports))
    monkeypatch.setattr(discovery, "_warned_duplicates", set())
    return ports


def _info(vid: int | None, pid: int | None, serial: str | None) -> PortInfo:
    return PortInfo("COM7", "COM7", vid, pid, serial, None, None)


def test_device_id_is_lower_case_hex_and_keeps_the_serial() -> None:
    assert _info(0x10C4, 0xEA60, "A50285BI").device_id == "usb:10c4:ea60:A50285BI"
    assert _info(0x403, 0x6001, "A50285BI").device_id == FTDI


@pytest.mark.parametrize(
    ("vid", "pid", "serial"),
    [
        (0x0403, 0x6001, None),
        (0x0403, 0x6001, "7"),
        (0x0403, 0x6001, "A50"),
        (None, 0x6001, "A50285BI"),
        (0x0403, None, "A50285BI"),
    ],
)
def test_no_device_id_without_a_trusted_serial(
    vid: int | None, pid: int | None, serial: str | None
) -> None:
    assert _info(vid, pid, serial).device_id is None


def test_a_four_character_serial_is_trusted() -> None:
    assert _info(0x0403, 0x6001, "A502").device_id == "usb:0403:6001:A502"


def test_list_ports_maps_serialx_fields_sorted_by_path(
    listing: list[serialx.SerialPortInfo],
) -> None:
    by_id = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
    listing += [
        _listed("/dev/ttyUSB1"),
        _listed(by_id, 0x0403, 0x6001, "A50285BI", resolved="/dev/ttyUSB0"),
    ]
    assert list_ports() == [
        PortInfo(by_id, "/dev/ttyUSB0", 0x0403, 0x6001, "A50285BI", "FTDI", "FT232R USB UART"),
        PortInfo("/dev/ttyUSB1", "/dev/ttyUSB1", None, None, None, "FTDI", "FT232R USB UART"),
    ]


def test_resolve_finds_the_path_of_a_device_id(listing: list[serialx.SerialPortInfo]) -> None:
    listing += [_listed("COM3"), _listed("COM7", 0x0403, 0x6001, "A50285BI")]
    assert resolve(FTDI) == "COM7"


def test_resolve_returns_a_path_as_typed_listed_or_not(
    listing: list[serialx.SerialPortInfo],
) -> None:
    assert resolve("/dev/ttyACM0") == "/dev/ttyACM0"
    listing.append(_listed("COM3"))
    assert resolve("COM9") == "COM9"


def test_resolve_raises_for_an_absent_device(listing: list[serialx.SerialPortInfo]) -> None:
    listing += [_listed("COM3"), _listed("COM4", 0x10C4, 0xEA60, "0001")]
    with pytest.raises(PortNotFound) as caught:
        resolve(FTDI)
    assert str(caught.value) == f"No device matches {FTDI}. Connected: COM3, usb:10c4:ea60:0001."


def test_port_not_found_with_nothing_connected() -> None:
    assert str(PortNotFound(FTDI, [])) == f"No device matches {FTDI}. Connected: none."


def test_duplicate_device_ids_resolve_to_the_first_path_warning_once(
    listing: list[serialx.SerialPortInfo], caplog: pytest.LogCaptureFixture
) -> None:
    listing += [
        _listed("/dev/ttyUSB3", 0x0403, 0x6001, "A50285BI"),
        _listed("/dev/ttyUSB1", 0x0403, 0x6001, "A50285BI"),
    ]
    with caplog.at_level(logging.WARNING):
        assert resolve(FTDI) == "/dev/ttyUSB1"
        assert resolve(FTDI) == "/dev/ttyUSB1"
    assert [r.getMessage() for r in caplog.records] == [
        f"{FTDI} matches /dev/ttyUSB1, /dev/ttyUSB3; using /dev/ttyUSB1"
    ]


def test_present_by_device_id(listing: list[serialx.SerialPortInfo]) -> None:
    assert not present(FTDI)
    listing.append(_listed("COM7", 0x0403, 0x6001, "A50285BI"))
    assert present(FTDI)


def test_present_by_path_is_the_path_existing_outside_windows(
    listing: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", False)
    device = tmp_path / "ttyUSB0"
    device.touch()
    listing.append(_listed(str(tmp_path / "by-id-gone")))
    assert present(str(device))
    assert not present(str(tmp_path / "by-id-gone"))


def test_present_by_path_on_windows_needs_the_path_listed(
    listing: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", True)
    listing.append(_listed("COM3"))
    assert present("COM3")
    assert not present("COM7")


def test_a_path_on_windows_is_absent_when_nothing_is_listed(
    listing: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", True)
    assert not present("COM7")


@pytest.mark.parametrize("typed", ["COM10", "com10", "\\\\.\\COM10", "\\\\.\\com10"])
def test_present_on_windows_matches_a_path_as_serialx_opens_it(
    listing: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", True)
    listing.append(_listed("COM10"))
    assert present(typed)
    assert not present("COM1")


def test_a_device_has_its_listed_and_its_resolved_path(
    listing: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", False)
    by_id = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
    listing.append(_listed(by_id, 0x0403, 0x6001, "A50285BI", resolved="/dev/ttyUSB0"))
    [device] = list_ports()
    assert device.has_path(by_id)
    assert device.has_path("/dev/ttyUSB0")
    assert not device.has_path("/dev/ttyUSB1")


def test_a_port_field_names_a_device_by_its_id_or_either_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", False)
    by_id = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
    device = PortInfo(by_id, "/dev/ttyUSB0", 0x0403, 0x6001, "A50285BI", None, None)
    assert device.named_by(FTDI)
    assert device.named_by(by_id)
    assert device.named_by("/dev/ttyUSB0")
    assert not device.named_by("usb:0403:6001:B50285BI")
    assert not device.named_by("/dev/ttyUSB1")


def test_same_port_on_windows_ignores_case_and_the_device_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", True)
    assert same_port("\\\\.\\com10", "COM10")
    assert not same_port("COM1", "COM10")
    # Only a leading prefix is the device namespace.
    assert not same_port("COM10\\\\.\\", "COM10")


def test_same_port_elsewhere_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "_WINDOWS", False)
    assert same_port("/dev/ttyUSB0", "/dev/ttyUSB0")
    assert not same_port("/dev/ttyusb0", "/dev/ttyUSB0")
