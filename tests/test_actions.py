"""The actions, run the way the agent runs them: through the SDK's own validation and execution."""

import concurrent.futures
import inspect
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import serialx
from zelos_sdk.actions import ActionExecutionError, ValidationError

from tests.conftest import FakeTransport, Recording, listed_port, run, wait_until
from zelos_extension_serial import actions, config, discovery, transport, worker
from zelos_extension_serial.emitter import Emitter
from zelos_extension_serial.transport import open_transport
from zelos_extension_serial.worker import PortWorker

REPO_ROOT = Path(__file__).resolve().parents[1]
BANNER = "*** Booting Zephyr OS build dccb09599635 ***"
STATE_KEYS = {"state", "health", "where", "clock", "counters", "signals"}
COUNTERS = {
    "rx_bytes",
    "tx_bytes",
    "lines",
    "value_lines",
    "signals",
    "late_names",
    "long_lines",
    "prompts",
    "echoes",
    "reconnects",
    "faults",
    "errors",
}


def fails(name: str, **params: Any) -> str:
    """The message the caller sees when the action fails."""
    with pytest.raises((ActionExecutionError, ValidationError)) as raised:
        run(name, **params)
    return str(raised.value)


def sampled(port: str = "demo") -> list[str]:
    return run("sample", port=port, lines=1000)["lines"]


# Flags and inventory


def test_names_timeouts_and_standalone_flags() -> None:
    declared = {
        name: (obj._action.timeout, obj._action.standalone)
        for name, obj in vars(actions).items()
        if hasattr(obj, "_action")
    }
    assert declared == {
        "list_ports": (30, True),
        "auto_config": (30, True),
        "send": (5, False),
        "command": (62, False),
        "reset": (5, False),
        "release": (5, False),
        "acquire": (15, False),
        "get_state": (5, False),
        "sample": (5, False),
    }


def test_only_actions_that_change_nothing_are_read_only() -> None:
    """Zelos AI runs a read-only action without asking, so each one is a deliberate choice."""
    declared = {
        name
        for name, obj in vars(actions).items()
        if hasattr(obj, "_action") and obj._action.read_only
    }
    assert declared == {"list_ports", "auto_config", "get_state", "sample"}


def test_python_defaults_equal_the_form_defaults() -> None:
    """A caller that omits a field gets the Python default, not the form's."""
    for name, obj in vars(actions).items():
        if not hasattr(obj, "_action"):
            continue
        parameters = inspect.signature(obj).parameters
        for field in obj._action.fields:
            if not field.required:
                assert parameters[field.name].default == field.default, f"{name}.{field.name}"


# Standalone actions


@pytest.fixture
def devices(
    listing: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[serialx.SerialPortInfo]]:
    """What serialx lists; opening any port, or starting any worker, fails the test."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a standalone action opened a port")

    monkeypatch.setattr(serialx, "Serial", refuse)
    monkeypatch.setattr(serialx, "serial_for_url", refuse)
    monkeypatch.setattr(transport, "open_transport", refuse)
    monkeypatch.setattr(worker.PortWorker, "start", refuse)
    threads = threading.active_count()
    yield listing
    assert threading.active_count() == threads


FT232R = listed_port("/dev/ttyUSB0", 0x0403, 0x6001, "A50285BI", "FTDI", "FT232R USB UART")


def test_list_ports_offers_every_device(devices: list[serialx.SerialPortInfo]) -> None:
    devices.extend(
        [
            FT232R,
            listed_port("COM7", 0x10C4, 0xEA60, "1", product="CP2102 USB to UART"),
            listed_port("/dev/ttyS0"),
        ]
    )

    assert run("list_ports") == {
        "status": "success",
        "choices": [
            {
                "value": "/dev/ttyS0",
                "label": "/dev/ttyS0",
                "detail": None,
            },
            {
                "value": "usb:0403:6001:A50285BI",
                "label": "FT232R USB UART (/dev/ttyUSB0)",
                "detail": "FTDI 0403:6001 A50285BI",
            },
            {
                "value": "COM7",
                "label": "CP2102 USB to UART (COM7)",
                "detail": "10c4:ea60 1",
            },
        ],
    }


def test_list_ports_shows_the_device_node_a_linux_by_id_path_links_to(
    devices: list[serialx.SerialPortInfo],
) -> None:
    by_id = "/dev/serial/by-id/usb-STMicroelectronics_STLINK-V3_001E00423532-if02"
    devices.extend(
        [
            listed_port(
                by_id,
                0x0483,
                0x3754,
                "001E00423532",
                "STMicroelectronics",
                "STLINK-V3",
                resolved="/dev/ttyACM0",
            ),
            listed_port("/dev/serial/by-id/usb-1a86_USB_Serial-if00", resolved="/dev/ttyUSB0"),
        ]
    )

    assert run("list_ports")["choices"] == [
        {
            "value": "/dev/serial/by-id/usb-1a86_USB_Serial-if00",
            "label": "/dev/ttyUSB0",
            "detail": "/dev/serial/by-id/usb-1a86_USB_Serial-if00",
        },
        {
            "value": "usb:0483:3754:001E00423532",
            "label": "STLINK-V3 (/dev/ttyACM0)",
            "detail": f"{by_id} STMicroelectronics 0483:3754 001E00423532",
        },
    ]


def test_a_windows_cdc_device_is_labelled_and_named_without_its_com_port_twice(
    devices: list[serialx.SerialPortInfo],
) -> None:
    devices.append(
        listed_port("COM4", 0x0483, 0x374E, "001E0042", "Microsoft", "USB Serial Device (COM4)")
    )

    assert run("list_ports")["choices"] == [
        {
            "value": "usb:0483:374e:001E0042",
            "label": "USB Serial Device (COM4)",
            "detail": "Microsoft 0483:374e 001E0042",
        }
    ]
    assert run("auto_config")["config"]["ports"] == [
        {"connection": "serial", "port": "usb:0483:374e:001E0042", "name": "USB Serial Device"}
    ]


def test_list_ports_with_nothing_listed_says_what_to_type(
    devices: list[serialx.SerialPortInfo],
) -> None:
    assert run("list_ports") == {
        "status": "success",
        "choices": [],
        "message": "No serial devices found. Type a path such as /dev/ttyUSB0 or COM7.",
    }


def test_every_port_field_is_titled_port_name() -> None:
    titles = {
        name: field.title
        for name, obj in vars(actions).items()
        if hasattr(obj, "_action")
        for field in obj._action.fields
        if field.name == "port"
    }
    assert set(titles.values()) == {"Port name"}
    assert len(titles) == 7


def port_fields() -> dict[str, dict[str, Any]]:
    """Each live action's port field, as the form builds it now."""
    return {
        name: obj._action.to_schema()["properties"]["port"]
        for name, obj in vars(actions).items()
        if hasattr(obj, "_action") and not obj._action.standalone
    }


def test_the_port_field_starts_on_the_first_configured_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "workers", {"zeta": None, "alpha": None})

    fields = port_fields()

    assert len(fields) == 7
    for field in fields.values():
        assert (field["enum"], field["default"]) == (["alpha", "zeta"], "zeta")


def test_with_no_ports_the_port_field_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "workers", {})

    for field in port_fields().values():
        assert (field["enum"], field["default"]) == ([], None)


ACM = listed_port("/dev/ttyACM0", 0x2341, 0x0043, "75735303")


def test_auto_config_without_a_config_adds_one_port_per_usb_device(
    devices: list[serialx.SerialPortInfo], tmp_path: Path
) -> None:
    devices.extend(
        [
            FT232R,
            listed_port("/dev/ttyUSB1", 0x0403, 0x6001, "B7", "FTDI", "FT232R USB UART"),
            ACM,
            listed_port("/dev/ttyS0"),
        ]
    )

    answer = run("auto_config")

    assert answer == {
        "status": "success",
        "config": {
            "ports": [
                {"connection": "serial", "port": "usb:2341:0043:75735303"},
                {
                    "connection": "serial",
                    "port": "usb:0403:6001:A50285BI",
                    "name": "FT232R USB UART",
                },
                {"connection": "serial", "port": "/dev/ttyUSB1", "name": "FT232R USB UART_2"},
            ]
        },
        "message": "Added 3 serial devices.",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(answer["config"]))
    assert len(config.load(path).ports) == 3


def test_auto_config_keeps_every_port_and_adds_only_new_devices(
    devices: list[serialx.SerialPortInfo], tmp_path: Path
) -> None:
    devices.extend([FT232R, ACM, listed_port("/dev/ttyUSB1", 0x0403, 0x6001, "B50285BI")])
    existing = [
        {"connection": "serial", "port": "usb:0403:6001:A50285BI", "baud": 9600, "name": "dut"},
        {"connection": "serial", "port": "/dev/ttyACM0", "advanced": {"reset_line": "dtr"}},
        {"connection": "tcp", "host": "bench", "tcp_port": 4001, "name": "FT232R USB UART"},
        {"connection": "serial"},
    ]

    answer = run("auto_config", config={"ports": existing, "advanced": {"prefix": "Bench"}})

    assert answer == {
        "status": "success",
        "config": {
            "ports": [
                *existing,
                {"connection": "serial", "port": "usb:0403:6001:B50285BI"},
            ]
        },
        "message": "Added 1 serial device.",
    }


def test_auto_config_matches_a_configured_path_as_discovery_does(
    devices: list[serialx.SerialPortInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    by_id = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
    devices.extend(
        [
            listed_port(by_id, 0x0403, 0x6001, "A50285BI", resolved="/dev/ttyUSB0"),
            listed_port("COM7", 0x10C4, 0xEA60, "0001"),
        ]
    )
    monkeypatch.setattr(discovery, "_WINDOWS", True)
    existing = [
        {"connection": "serial", "port": "/dev/ttyUSB0"},
        {"connection": "serial", "port": "com7"},
    ]

    answer = run("auto_config", config={"ports": existing})

    assert answer["config"] == {"ports": existing}
    assert answer["message"] == "No new USB serial devices found."


def test_auto_config_ignores_a_port_field_left_on_a_network_entry(
    devices: list[serialx.SerialPortInfo],
) -> None:
    devices.append(FT232R)
    existing = [{"connection": "tcp", "host": "bench", "tcp_port": 4001, "port": "/dev/ttyUSB0"}]

    answer = run("auto_config", config={"ports": existing})

    assert answer["config"]["ports"][1:] == [
        {"connection": "serial", "port": "usb:0403:6001:A50285BI", "name": "FT232R USB UART"}
    ]
    assert answer["message"] == "Added 1 serial device."


def test_auto_config_names_new_ports_apart_from_configured_names(
    devices: list[serialx.SerialPortInfo],
) -> None:
    devices.append(FT232R)
    existing = [{"connection": "demo", "name": "FT232R USB UART"}]

    ports = run("auto_config", config={"ports": existing})["config"]["ports"]

    assert ports[1] == {
        "connection": "serial",
        "port": "usb:0403:6001:A50285BI",
        "name": "FT232R USB UART_2",
    }


def test_auto_config_writes_names_the_config_accepts(
    devices: list[serialx.SerialPortInfo], tmp_path: Path
) -> None:
    devices.extend(
        [
            listed_port("/dev/ttyUSB0", 0x0403, 0x6001, "A50285BI", product="Écran série"),
            listed_port("/dev/ttyUSB1", 0x0403, 0x6001, "B50285BI", product="µC board"),
            listed_port("/dev/ttyUSB2", 0x0403, 0x6001, "C50285BI", product="串口"),
        ]
    )

    ports = run("auto_config")["config"]["ports"]

    assert [port.get("name") for port in ports] == ["Ecran serie", "C board", None]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ports": ports}))
    assert len(config.load(path).ports) == 3


@pytest.mark.parametrize("given", [{}, {"config": {}}, {"config": {"ports": []}}])
def test_auto_config_falls_back_to_the_demo_device(
    devices: list[serialx.SerialPortInfo], given: dict[str, Any]
) -> None:
    devices.append(listed_port("/dev/ttyS0"))

    assert run("auto_config", **given) == {
        "status": "success",
        "config": {"ports": [{"connection": "demo"}]},
        "message": "No USB serial devices found; added the demo device.",
    }


def test_auto_config_with_ports_and_no_device_changes_nothing(
    devices: list[serialx.SerialPortInfo],
) -> None:
    existing = [{"connection": "tcp", "host": "bench", "tcp_port": 4001}]

    assert run("auto_config", config={"ports": existing}) == {
        "status": "success",
        "config": {"ports": existing},
        "message": "No new USB serial devices found.",
    }


@pytest.mark.parametrize(
    ("name", "params"), [("list_ports", {}), ("auto_config", {"config": {"ports": []}})]
)
def test_standalone_actions_run_at_rest(name: str, params: dict[str, Any], tmp_path: Path) -> None:
    """The agent's at-rest path: a fresh interpreter imports main.py and runs the action."""
    params_file, result = tmp_path / "params.json", tmp_path / "result.json"
    params_file.write_text(json.dumps(params))
    subprocess.run(
        [
            *(sys.executable, "-m", "zelos_sdk.extensions.actions", "execute"),
            *("--entry", "main.py", "--action", name),
            *("--params-file", str(params_file), "--result-file", str(result)),
        ],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
    )

    envelope = json.loads(result.read_text())
    assert envelope["status"] == "done"
    assert envelope["result"]["status"] == "success"


# Live actions


@pytest.fixture
def fake() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def ports(trace_file: Recording, tmp_path: Path, fake: FakeTransport) -> Iterator[None]:
    """A demo device named `demo` and a fake TCP port named `fake`, each on its own worker."""
    path = tmp_path / "config.json"
    demo = {"connection": "demo", "name": "demo"}
    tcp = {"connection": "tcp", "host": "127.0.0.1", "tcp_port": 4001, "name": "fake"}
    path.write_text(json.dumps({"ports": [demo, tcp]}))
    settings = config.load(path)
    emitter = Emitter(trace_file.source)
    # A test that ran the app leaves its stopped workers here.
    actions.workers.clear()
    for cfg in settings.ports:
        emitter.add_port(cfg.name)
        opener = open_transport if cfg.name == "demo" else (lambda _cfg: fake)
        actions.workers[cfg.name] = PortWorker(cfg, emitter, settings, open=opener)
        actions.workers[cfg.name].start()
    for name in actions.workers:
        wait_until(lambda name=name: run("get_state", port=name)["state"] == "open")
    yield
    for running in actions.workers.values():
        running.stop()
    for running in actions.workers.values():
        running.join(1.0)
    actions.workers.clear()


def test_command_returns_the_reply_up_to_the_prompt(ports: None) -> None:
    answer = run("command", port="demo", text="dcdc limit get")

    assert answer["reply"] == ["limit: 4.5 A"]
    assert answer["ended_by"] == "prompt"
    assert isinstance(answer["duration_ms"], int)


def test_command_ends_at_its_timeout_even_the_shortest(ports: None) -> None:
    answer = run("command", port="fake", text="hello", timeout_s=0.1)

    assert answer["ended_by"] == "timeout"
    assert answer["duration_ms"] < 1000


def test_send_text_and_hex(ports: None) -> None:
    def replies() -> int:
        return sampled().count("limit: 4.5 A")

    assert run("send", port="demo", text="dcdc limit get") == {"bytes_written": 15}
    wait_until(lambda: replies() == 1)
    assert run("send", port="demo", text=b"dcdc limit get\r".hex(), hex=True) == {
        "bytes_written": 15
    }
    wait_until(lambda: replies() == 2)


def test_reset_restarts_the_device(ports: None) -> None:
    wait_until(lambda: any(BANNER in line for line in sampled()))

    assert run("reset", port="demo") == {"ok": True}
    wait_until(lambda: sum(BANNER in line for line in sampled()) == 2)


def test_release_and_acquire(ports: None) -> None:
    assert run("release", port="demo") == {"ok": True}
    assert run("get_state", port="demo")["state"] == "released"

    assert run("acquire", port="demo") == {"ok": True}
    assert run("get_state", port="demo")["state"] == "open"


def test_get_state(ports: None) -> None:
    wait_until(lambda: run("get_state", port="demo")["signals"])

    state = run("get_state", port="demo")

    assert set(state) == STATE_KEYS
    assert (state["state"], state["where"]) == ("open", "demo")
    assert state["health"].startswith("Connected.")
    assert state["clock"] in ("in use", "not seen")
    assert set(state["counters"]) == COUNTERS
    assert {"event", "field", "unit", "rule", "example"} <= set(state["signals"][0])


def test_sample_lines_and_hex(ports: None) -> None:
    wait_until(lambda: len(sampled()) >= 3)

    assert len(run("sample", port="demo", lines=3)["lines"]) == 3
    chunks = run("sample", port="demo", lines=2, hex=True)["chunks"]
    assert len(chunks) == 2
    assert all(bytes.fromhex(chunk) for chunk in chunks)


def test_a_caller_that_omits_the_port_gets_the_first_configured_port(ports: None) -> None:
    assert run("get_state")["where"] == "demo"
    assert run("send", text="dcdc limit get") == {"bytes_written": 15}


def test_with_no_ports_a_caller_that_omits_the_port_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "workers", {})

    assert fails("get_state") == "no port is configured"


def test_the_worker_settles_an_acquire_before_the_caller_stops_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def request(self, kind: str, **args: Any) -> concurrent.futures.Future[dict[str, Any]]:
            asked.append((kind, args))
            future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
            future.set_result({"ok": True})
            return future

    monkeypatch.setitem(actions.workers, "dut", Recorder())

    assert run("acquire", port="dut") == {"ok": True}
    # The action times out at 15 s and its caller waits 14.5 s.
    assert asked == [("acquire", {"wait_s": 14.0})]


def test_worker_errors_reach_the_caller(ports: None) -> None:
    assert fails("reset", port="fake") == "a TCP port has no reset line"
    assert fails("acquire", port="fake") == "port is not released"


def test_a_stopped_port_fails_at_once(ports: None) -> None:
    actions.workers["fake"].stop()
    actions.workers["fake"].join(1.0)

    assert fails("send", port="fake", text="x") == "port is stopped"


def test_an_unknown_port_is_refused(ports: None) -> None:
    assert "Invalid choice: nope" in fails("get_state", port="nope")


def test_a_silent_worker_times_out_before_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    class Silent:
        def request(self, kind: str, **args: Any) -> concurrent.futures.Future[dict[str, Any]]:
            return concurrent.futures.Future()

    monkeypatch.setitem(actions.workers, "dut", Silent())

    with pytest.raises(RuntimeError, match=r"^dut did not answer within 0\.05 s$"):
        actions._ask("dut", "state", 0.55)


@pytest.mark.parametrize(
    ("name", "params", "error"),
    [
        ("sample", {"lines": 0}, "lines: Must be >= 1"),
        ("sample", {"lines": 1001}, "lines: Must be <= 1000"),
        ("command", {"text": "x", "timeout_s": 0.05}, "timeout_s: Must be >= 0.1"),
        ("command", {"text": "x", "timeout_s": 61}, "timeout_s: Must be <= 60"),
    ],
)
def test_the_form_bounds_arguments(
    ports: None, name: str, params: dict[str, Any], error: str
) -> None:
    assert fails(name, port="fake", **params).startswith(error)


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda: actions.sample("fake", lines=0), "lines must be from 1 to 1000"),
        (lambda: actions.sample("fake", lines=1001), "lines must be from 1 to 1000"),
        (
            lambda: actions.command("fake", text="x", timeout_s=0.05),
            "timeout_s must be from 0.1 to 60",
        ),
        (
            lambda: actions.command("fake", text="x", timeout_s=61),
            "timeout_s must be from 0.1 to 60",
        ),
    ],
)
def test_a_direct_call_is_bounded_too(ports: None, call: Callable[[], object], error: str) -> None:
    with pytest.raises(ValueError, match=f"^{error}$"):
        call()


@pytest.mark.parametrize("lines", [1, 1000])
def test_lines_bounds_are_inclusive(ports: None, lines: int) -> None:
    assert len(run("sample", port="demo", lines=lines)["lines"]) <= lines
