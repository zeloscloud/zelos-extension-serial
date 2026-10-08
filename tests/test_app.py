"""The app end to end, and the names it gives unnamed ports."""

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import serialx
from zelos_sdk.actions import ActionExecutionError

from tests.conftest import FakeTransport, Recording, listed_port, run, wait_until
from zelos_extension_serial import actions, app, config
from zelos_extension_serial.config import PortConfig
from zelos_extension_serial.transport import Transport

FTDI = listed_port("/dev/ttyUSB0", 0x0403, 0x6001, "A50285BI", "FTDI", "FT232R USB UART")
FTDI_ID = "usb:0403:6001:A50285BI"


@pytest.fixture
def config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[dict[str, Any]], Path]:
    def write(content: dict[str, Any]) -> Path:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(content))
        monkeypatch.setenv("ZELOS_CONFIG_PATH", str(path))
        return path

    return write


def test_lines_from_a_tcp_port_reach_its_log(trace_file: Recording, config_file) -> None:
    config_file(
        {"ports": [{"connection": "tcp", "host": "127.0.0.1", "tcp_port": 4001, "name": "dut"}]}
    )
    transport = FakeTransport([b"hello\nworld\n"])
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()

    app.run(trace_file.source, open=lambda _cfg: transport, stop=stop)

    rows = [row for row in trace_file.events()["dut/log"] if row["name"] != "serial"]
    assert [row["message"] for row in rows] == ["hello", "world"]
    assert {row["level"] for row in rows} == {"info"}
    assert transport.closed


def test_run_fills_the_workers_under_default_names(
    trace_file: Recording, config_file, listing: list[serialx.SerialPortInfo]
) -> None:
    listing.append(FTDI)
    config_file(
        {
            "ports": [
                {"connection": "serial", "port": FTDI_ID},
                {"connection": "tcp", "host": "127.0.0.1", "tcp_port": 4001},
                {"connection": "demo"},
            ]
        }
    )
    stop = threading.Event()
    stop.set()

    app.run(trace_file.source, open=lambda _cfg: FakeTransport(), stop=stop)

    assert sorted(actions.workers) == ["127_0_0_1_4001", "demo", "usb_0403_6001_A50285BI"]


def test_an_empty_config_exits_with_code_1(trace_file: Recording, config_file) -> None:
    config_file({})

    with pytest.raises(SystemExit) as exit_info:
        app.run(trace_file.source, stop=threading.Event())

    assert exit_info.value.code == 1


def test_stuck_ports_end_within_one_second_of_stop(
    trace_file: Recording, config_file, listing: list[serialx.SerialPortInfo]
) -> None:
    config_file({"ports": [{"connection": "demo"}] * 4})
    stop, release = threading.Event(), threading.Event()
    opening: list[str] = []
    stopped_at = 0.0

    def stuck(cfg: PortConfig) -> Transport:
        nonlocal stopped_at
        opening.append(cfg.name)
        if len(opening) == 4:
            stopped_at = time.monotonic()
            stop.set()
        release.wait()
        raise OSError("released")

    try:
        app.run(trace_file.source, open=stuck, stop=stop)
    finally:
        release.set()
    elapsed = time.monotonic() - stopped_at
    for worker in actions.workers.values():
        worker.join(5)

    # Under 2 s even on a stalled runner; joined one at a time, the four took 4 s.
    assert 1.0 <= elapsed < 2.0


def default_names(config_file, ports: list[dict[str, Any]]) -> list[str]:
    settings = config.load(config_file({"ports": ports}))
    return [cfg.name for cfg in app._with_default_names(settings.ports)]


@pytest.mark.parametrize(
    ("port", "name"),
    [
        pytest.param({"connection": "serial", "port": FTDI_ID}, "usb_0403_6001_A50285BI", id="id"),
        pytest.param({"connection": "serial", "port": "/dev/ttyUSB0"}, "dev_ttyUSB0", id="path"),
        pytest.param({"connection": "serial", "port": "COM7"}, "COM7", id="windows path"),
        pytest.param(
            {"connection": "tcp", "host": "bench.local", "tcp_port": 2217},
            "bench_local_2217",
            id="tcp",
        ),
        pytest.param(
            {"connection": "rfc2217", "host": "10.0.0.5", "tcp_port": 2217},
            "10_0_0_5_2217",
            id="rfc2217",
        ),
        pytest.param({"connection": "demo"}, "demo", id="demo"),
    ],
)
def test_an_unnamed_port_is_named_after_its_config(
    config_file, listing: list[serialx.SerialPortInfo], port: dict[str, Any], name: str
) -> None:
    listing.append(FTDI)

    assert default_names(config_file, [port]) == [name]


def test_default_names_do_not_depend_on_which_devices_are_plugged_in(
    config_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical adapters, one unplugged at start, must not swap names between runs."""
    second = listed_port("/dev/ttyUSB1", 0x0403, 0x6001, "B77777XY", "FTDI", FTDI.product)
    ports = [
        {"connection": "serial", "port": FTDI_ID},
        {"connection": "serial", "port": "usb:0403:6001:B77777XY"},
    ]
    seen = []
    for plugged in ([FTDI, second], [second], []):
        monkeypatch.setattr(serialx, "list_serial_ports", lambda plugged=plugged: list(plugged))
        seen.append(default_names(config_file, ports))

    assert seen == [["usb_0403_6001_A50285BI", "usb_0403_6001_B77777XY"]] * 3


def test_duplicates_get_a_suffix(config_file, listing: list[serialx.SerialPortInfo]) -> None:
    ports = [
        {"connection": "serial", "port": "/dev/tty.A"},
        {"connection": "serial", "port": "/dev/tty_A"},
        {"connection": "demo"},
        {"connection": "demo"},
    ]

    assert default_names(config_file, ports) == ["dev_tty_A", "dev_tty_A_2", "demo", "demo_2"]


def test_configured_names_win(config_file, listing: list[serialx.SerialPortInfo]) -> None:
    ports = [{"connection": "demo"}, {"connection": "demo", "name": "demo"}]

    assert default_names(config_file, ports) == ["demo_2", "demo"]


def test_a_default_name_differs_from_configured_names_in_more_than_case(
    config_file, listing: list[serialx.SerialPortInfo]
) -> None:
    ports = [{"connection": "demo"}, {"connection": "demo", "name": "DEMO"}]

    assert default_names(config_file, ports) == ["demo_2", "DEMO"]


def test_a_failed_action_reaches_the_caller_but_not_the_log(
    trace_file: Recording, config_file
) -> None:
    config_file(
        {"ports": [{"connection": "tcp", "host": "127.0.0.1", "tcp_port": 4001, "name": "dut"}]}
    )
    stop = threading.Event()
    runner = threading.Thread(
        target=app.run,
        args=(trace_file.source,),
        kwargs={"open": lambda _cfg: FakeTransport(), "stop": stop},
    )
    runner.start()
    try:
        wait_until(lambda: "dut" in actions.workers)
        with pytest.raises(ActionExecutionError):
            run("acquire", port="dut")
        logging.getLogger(app.__name__).warning("a note from the extension")
    finally:
        stop.set()
        runner.join(5)

    messages = [row["message"] for row in trace_file.events()["log"]]
    assert "a note from the extension" in messages
    assert not [m for m in messages if "acquire" in m], messages
