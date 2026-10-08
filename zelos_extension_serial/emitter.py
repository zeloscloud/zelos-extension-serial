"""Parsed lines to trace events."""

import logging
from dataclasses import dataclass, field

import zelos_sdk
from zelos_sdk import schemas

from .formats import Parsed
from .naming import Namer

logger = logging.getLogger(__name__)

# A device printing ever-changing names (`pkt_1234=`) would otherwise grow memory and the log
# without bound.
_LATE_CAP = 32

# Every module is a registered event, and a device printing ever-changing tags (ESP-IDF) would
# register one per tag.
_MODULE_CAP = 64

# The SDK fixes an event's fields when it is registered, and Teleplot or `label: value` print one
# name per line, some slower than others: a module's rows wait until its names are known.
_HOLD_NS = 2_000_000_000
_HOLD_NAMES = 64
# One name printed at 1 kHz would otherwise hold 2,000 rows.
_HOLD_ROWS = 1000


@dataclass(frozen=True, slots=True)
class SignalInfo:
    """One registered signal, as get_state reports it."""

    event: str
    field: str
    unit: str | None
    rule: str
    example: str


@dataclass(slots=True)
class _Field:
    key: str
    unit: str | None
    last: Parsed


@dataclass(slots=True)
class _Event:
    name: str
    handle: zelos_sdk.TraceSourceEvent
    # Keyed by the name as printed; fixed at registration.
    fields: dict[str, _Field]
    late: set[str] = field(default_factory=set)
    late_capped: bool = False
    unit_warned: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Held:
    """A module's value rows, waiting until its names are known."""

    name: str
    first_ns: int
    rows: list[tuple[int, Parsed]] = field(default_factory=list)
    # Each name as printed and its first unit, in the order first printed.
    units: dict[str, str | None] = field(default_factory=dict)


@dataclass(slots=True)
class _Port:
    log: zelos_sdk.TraceSourceEvent
    modules: Namer = field(default_factory=lambda: Namer(("log", "values")))
    events: dict[str | None, _Event] = field(default_factory=dict)
    held: dict[str | None, _Held] = field(default_factory=dict)
    value_lines: int = 0
    modules_capped: bool = False


class Emitter:
    """Names, groups and writes trace events; the worker decides time."""

    def __init__(self, source: zelos_sdk.TraceSource) -> None:
        self._source = source
        # Each port is only ever touched by its own worker thread, so ports share nothing.
        self._ports: dict[str, _Port] = {}

    def add_port(self, port: str) -> None:
        """Register `<port>/log`."""
        self._ports[port] = _Port(self._source.add_event(f"{port}/log", schemas.Log))

    def line(self, port: str, parsed: Parsed, time_ns: int) -> None:
        """Write one device line to `<port>/log` now, and its values to their event.

        A module's value rows wait until a flush 2 s after its first, or until 64 names or 1,000
        rows are held; then its event is registered with every name seen and the rows are written
        with their own times.
        """
        state = self._ports[port]
        state.log.log_at(
            time_ns,
            level=parsed.level,
            message=parsed.message,
            name=parsed.name,
            file=parsed.file,
            line=parsed.line_no,
        )
        if parsed.values:
            self._values(port, state, parsed, time_ns)

    def flush(self, port: str, now_ns: int | None = None) -> None:
        """Register and write each held module whose 2 s have passed at `now_ns`; all when None."""
        state = self._ports[port]
        for module, held in list(state.held.items()):
            if now_ns is None or now_ns - held.first_ns >= _HOLD_NS:
                self._register(port, state, module)

    def note(self, port: str, level: str, message: str, time_ns: int, name: str = "serial") -> None:
        """Write one extension notice to `<port>/log`."""
        self._ports[port].log.log_at(
            time_ns, level=level, message=message, name=name, file="", line=0
        )

    def signals(self, port: str) -> list[SignalInfo]:
        """Every signal registered for the port; a held module's appear once it is registered."""
        return [
            SignalInfo(event.name, f.key, f.unit, f.last.rule, f.last.message)
            for event in self._ports[port].events.values()
            for f in event.fields.values()
        ]

    def counters(self, port: str) -> dict[str, int]:
        """`signals` and `late_names` as registered; `value_lines` counts held lines too."""
        state = self._ports[port]
        return {
            "signals": sum(len(event.fields) for event in state.events.values()),
            "late_names": sum(len(event.late) for event in state.events.values()),
            "value_lines": state.value_lines,
        }

    def _values(self, port: str, state: _Port, parsed: Parsed, time_ns: int) -> None:
        event = state.events.get(parsed.module)
        if event is not None:
            if _write(event, parsed, time_ns):
                state.value_lines += 1
            return
        held = state.held.get(parsed.module)
        if held is None:
            if len(state.events) + len(state.held) >= _MODULE_CAP:
                if not state.modules_capped:
                    state.modules_capped = True
                    logger.warning(
                        "%s: further modules are not recorded as signals; "
                        "their lines are still logged",
                        port,
                    )
                return
            module = "values" if parsed.module is None else state.modules.name(parsed.module)
            held = state.held[parsed.module] = _Held(f"{port}/{module}", time_ns)
        state.value_lines += 1
        for value in parsed.values:
            held.units.setdefault(value.name, value.unit)
        held.rows.append((time_ns, parsed))
        if len(held.units) >= _HOLD_NAMES or len(held.rows) >= _HOLD_ROWS:
            self._register(port, state, parsed.module)

    def _register(self, port: str, state: _Port, module: str | None) -> None:
        """Register a held module with every name it printed, then write its rows."""
        held = state.held.pop(module)
        # `time_ns` is the trace's time column, and `log_at(time_ns, **row)` takes it as an
        # argument: a field of that name would crash the call and is never written.
        names = Namer(("time_ns",))
        first = held.rows[0][1]
        fields = {raw: _Field(names.name(raw), unit, first) for raw, unit in held.units.items()}
        handle = self._source.add_event(
            held.name,
            [
                zelos_sdk.TraceEventFieldMetadata(f.key, zelos_sdk.DataType.Float64, f.unit)
                for f in fields.values()
            ],
        )
        event = state.events[module] = _Event(held.name, handle, fields)
        for time_ns, parsed in held.rows:
            _write(event, parsed, time_ns)


def _write(event: _Event, parsed: Parsed, time_ns: int) -> bool:
    """Write the values of `parsed` that `event` has a field for; False when it has none."""
    row: dict[str, float] = {}
    for value in parsed.values:
        registered = event.fields.get(value.name)
        if registered is None:
            _late(event, value.name)
            continue
        if value.unit != registered.unit and value.name not in event.unit_warned:
            event.unit_warned.add(value.name)
            logger.warning(
                "%s: '%s' printed in %s, first printed in %s; written anyway",
                event.name,
                value.name,
                value.unit or "no unit",
                registered.unit or "no unit",
            )
        registered.last = parsed
        row[registered.key] = value.value
    if row:
        event.handle.log_at(time_ns, **row)
    return bool(row)


def _late(event: _Event, name: str) -> None:
    """Warn once about a name first printed after `event` was registered."""
    if name in event.late or event.late_capped:
        return
    if len(event.late) < _LATE_CAP:
        event.late.add(name)
        logger.warning(
            "'%s' first appeared after %s's signals were set up, so it is not recorded. "
            "Restart the extension to include it.",
            name,
            event.name,
        )
    else:
        event.late_capped = True
        logger.warning("%s: further new values are dropped without a warning", event.name)
