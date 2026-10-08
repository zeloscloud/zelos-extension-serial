"""What the Action panel, notebooks and Zelos AI can do with the ports."""

from typing import Any

from zelos_sdk.actions import SelectField, action, register_field_type

from . import discovery
from .naming import ascii_name, unique_name
from .worker import PortWorker, RequestKind

#: Port name to its worker; app.run() fills it before the main loop starts.
workers: dict[str, PortWorker] = {}

_TIMEOUT_S = 5.0
_COMMAND_TIMEOUT_S = 62.0
_ACQUIRE_TIMEOUT_S = 15.0
# A wait ends this much before the action's own timeout, so the caller gets the reason.
_MARGIN_S = 0.5
_MIN_COMMAND_S, _MAX_COMMAND_S = 0.1, 60.0
_MAX_LINES = 1000
# The first run after an install imports for 10 to 12 s on macOS.
_STANDALONE_TIMEOUT_S = 30


def _first_port() -> str | None:
    """The first configured port: the form starts on it, and a caller that omits one gets it."""
    return next(iter(workers), None)


@register_field_type("serial_port")
class _PortField(SelectField):
    """A port name field whose form default is the first configured port."""

    def to_json_schema(self, current_values: dict[str, Any] | None = None) -> dict[str, Any]:
        schema = super().to_json_schema(current_values)
        # The SDK's `default` is fixed at import; the ports are known only once the app runs.
        schema["default"] = _first_port()
        return schema


_port = action.input(
    "port",
    type="serial_port",
    title="Port name",
    choices=lambda *_: sorted(workers),
    required=False,
)


@action(
    "List Ports",
    "Serial devices on this machine, as choices for a port field.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
    read_only=True,
)
def list_ports() -> dict[str, Any]:
    choices = []
    for p in discovery.list_ports():
        usb = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None and p.pid is not None else None
        # Linux lists a /dev/serial/by-id name; the label shows the node it links to, the detail it.
        by_id = p.path if p.resolved != p.path else None
        choices.append(
            {
                "value": p.choice,
                "label": f"{p.product} ({p.resolved})" if p.product else p.resolved,
                "detail": " ".join(filter(None, (by_id, p.manufacturer, usb, p.serial))) or None,
            }
        )
    if not choices:
        return {
            "status": "success",
            "choices": [],
            "message": "No serial devices found. Type a path such as /dev/ttyUSB0 or COM7.",
        }
    return {"status": "success", "choices": choices}


@action(
    "Auto-configure",
    "Keep the configured ports and add one for each USB serial device not already listed, "
    "or the demo device when there are no ports and no devices.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
    read_only=True,
)
@action.object("config", title="Configuration", properties={}, required=False)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    # The form sends its unsaved config from 26.0.9; 26.0.8 sends none and replaces its ports.
    ports: list[Any] = list((config or {}).get("ports") or [])
    # Only a serial entry names a device: a network entry can keep the port of the serial entry
    # the form changed it from.
    fields = [
        p["port"]
        for p in ports
        if isinstance(p, dict)
        and p.get("connection") == "serial"
        and isinstance(p.get("port"), str)
    ]
    used = {p["name"] for p in ports if isinstance(p, dict) and isinstance(p.get("name"), str)}
    added = 0
    for device in discovery.list_ports():
        if device.vid is None or any(device.named_by(field) for field in fields):
            continue
        port = {"connection": "serial", "port": device.choice}
        # Folded to ASCII: the config's name pattern accepts ASCII only.
        if device.product and (name := ascii_name(device.product)):
            port["name"] = unique_name(name, used)
        ports.append(port)
        added += 1
    if added:
        noun = "device" if added == 1 else "devices"
        message = f"Added {added} serial {noun}."
    elif ports:
        message = "No new USB serial devices found."
    else:
        ports = [{"connection": "demo"}]
        message = "No USB serial devices found; added the demo device."
    return {"status": "success", "config": {"ports": ports}, "message": message}


# A field's `default` only fills the form; a caller that omits the field gets the Python default,
# so each parameter repeats it.
@action("Send", "Write text, or hex bytes, to a port.", timeout=_TIMEOUT_S)
@_port
@action.text("text", title="Text")
@action.boolean(
    "hex",
    title="Hex",
    description="Send the text as hex bytes, such as 0d0a.",
    required=False,
    default=False,
)
def send(port: str | None = None, *, text: str, hex: bool = False) -> dict[str, Any]:
    return _ask(port, "send", _TIMEOUT_S, text=text, hex=hex)


@action(
    "Run Command",
    "Send a shell command and return the lines the device prints in reply.",
    timeout=_COMMAND_TIMEOUT_S,
)
@_port
@action.text("text", title="Command")
@action.number(
    "timeout_s",
    title="Timeout (s)",
    minimum=_MIN_COMMAND_S,
    maximum=_MAX_COMMAND_S,
    required=False,
    default=2.0,
)
def command(port: str | None = None, *, text: str, timeout_s: float = 2.0) -> dict[str, Any]:
    _check_range("timeout_s", timeout_s, _MIN_COMMAND_S, _MAX_COMMAND_S)
    return _ask(port, "command", _COMMAND_TIMEOUT_S, text=text, timeout_s=timeout_s)


@action("Reset Device", "Pulse the port's reset line.", timeout=_TIMEOUT_S)
@_port
def reset(port: str | None = None) -> dict[str, Any]:
    return _ask(port, "reset", _TIMEOUT_S)


@action("Release Port", "Close the port so another program can use it.", timeout=_TIMEOUT_S)
@_port
def release(port: str | None = None) -> dict[str, Any]:
    return _ask(port, "release", _TIMEOUT_S)


@action("Acquire Port", "Take a released port back.", timeout=_ACQUIRE_TIMEOUT_S)
@_port
def acquire(port: str | None = None) -> dict[str, Any]:
    # The worker gives up a margin before its caller does, so the caller gets the reason.
    return _ask(port, "acquire", _ACQUIRE_TIMEOUT_S, wait_s=_ACQUIRE_TIMEOUT_S - 2 * _MARGIN_S)


@action(
    "Get State",
    "State, health, clock, counters and signals of a port.",
    timeout=_TIMEOUT_S,
    read_only=True,
)
@_port
def get_state(port: str | None = None) -> dict[str, Any]:
    return _ask(port, "state", _TIMEOUT_S)


@action(
    "Sample",
    "The last lines a port received, or its last raw reads as hex.",
    timeout=_TIMEOUT_S,
    read_only=True,
)
@_port
@action.integer("lines", title="Lines", minimum=1, maximum=_MAX_LINES, required=False, default=50)
@action.boolean("hex", title="Raw reads as hex", required=False, default=False)
def sample(port: str | None = None, lines: int = 50, hex: bool = False) -> dict[str, Any]:
    _check_range("lines", lines, 1, _MAX_LINES)
    return _ask(port, "sample", _TIMEOUT_S, lines=lines, hex=hex)


def _ask(
    port: str | None,
    kind: RequestKind,
    action_timeout_s: float,
    **args: Any,
) -> dict[str, Any]:
    """The worker's reply; its error is raised as it is."""
    if port is None:
        port = _first_port()
        if port is None:
            raise ValueError("no port is configured")
    future = workers[port].request(kind, **args)
    wait_s = action_timeout_s - _MARGIN_S
    try:
        return future.result(timeout=wait_s)
    except TimeoutError:
        # Not cancelled: the worker sets the result later, and a cancelled Future refuses it.
        raise RuntimeError(f"{port} did not answer within {wait_s:g} s") from None


def _check_range(name: str, value: float, low: float, high: float) -> None:
    # The field bounds hold only for calls through the agent.
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}")
