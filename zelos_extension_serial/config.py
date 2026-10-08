"""Configuration types and loading."""

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "config.schema.json").read_text(encoding="utf-8")
)
# Draft 7, because it is the draft that enforces the per-connection `dependencies`.
_VALIDATOR = Draft7Validator(_SCHEMA)
_BRANCHES: list[dict[str, Any]] = _SCHEMA["properties"]["ports"]["items"]["dependencies"][
    "connection"
]["oneOf"]
_CONNECTIONS: list[str] = [b["properties"]["connection"]["enum"][0] for b in _BRANCHES]
_SETTINGS: dict[str, Any] = _SCHEMA["properties"]["advanced"]["properties"]
_LINE_ENDINGS = {"LF": b"\n", "CRLF": b"\r\n", "CR": b"\r"}


def _fields(connection: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The schema of a connection's own settings, and of those in its Advanced group."""
    props = dict(_BRANCHES[_CONNECTIONS.index(connection)]["properties"])
    del props["connection"]
    advanced = props.pop("advanced", {}).get("properties", {})
    return props, advanced


# Every setting's default, so a connection that does not take one (TCP has no baud) still fills
# PortConfig.
_DEFAULTS = {
    name: schema.get("default")
    for c in _CONNECTIONS
    for group in _fields(c)
    for name, schema in group.items()
}


class ConfigError(Exception):
    """The configuration is invalid; one human sentence per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class PortConfig:
    """One configured port, with every default applied."""

    name: str
    connection: Literal["serial", "tcp", "rfc2217", "demo"]
    port: str | None
    host: str | None
    tcp_port: int | None
    baud: int
    data_bits: int
    parity: Literal["none", "even", "odd", "mark", "space"]
    stop_bits: float
    rtscts: bool
    xonxoff: bool
    dtr: bool
    rts: bool
    reset_line: Literal["none", "dtr", "rts"]
    line_ending: bytes
    prompt: re.Pattern[str] | None
    values: bool


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole configuration."""

    ports: tuple[PortConfig, ...]
    prefix: str
    time_source: Literal["auto", "host"]
    log_level: str


def load(path: Path | None = None) -> Settings:
    """Read, validate and default the configuration."""
    path = path or Path(os.environ.get("ZELOS_CONFIG_PATH", "config.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except OSError as e:
        raise ConfigError(
            [f"The configuration file {path} cannot be read: {e.strerror or e}."]
        ) from e
    except UnicodeDecodeError as e:
        raise ConfigError([f"The configuration file {path} is not UTF-8 text."]) from e
    except json.JSONDecodeError as e:
        raise ConfigError([f"The configuration is not valid JSON: {e}."]) from e
    if not isinstance(raw, dict) or not raw.get("ports"):
        raise ConfigError(["Add at least one port."])
    problems = _problems(raw)
    if problems:
        raise ConfigError(problems)

    ports: list[PortConfig] = []
    names: dict[str, int] = {}
    for index, item in enumerate(raw["ports"], 1):
        try:
            port = _port(item)
        except re.error as e:
            title = _fields(item["connection"])[1]["prompt"]["title"]
            problems.append(
                f"{_where(index, item)}, {title}: is not a valid regular expression ({e})."
            )
            continue
        # The trace store ignores case: two names that differ only in case leave it empty.
        key = port.name.casefold()
        if key and names.setdefault(key, index) != index:
            problems.append(
                f"{_where(index, item)}: name '{port.name}' is already used by port {names[key]}."
            )
        ports.append(port)
    if problems:
        raise ConfigError(problems)

    advanced = {name: schema["default"] for name, schema in _SETTINGS.items()}
    advanced.update(raw.get("advanced", {}))
    return Settings(
        ports=tuple(ports),
        prefix=advanced["prefix"],
        time_source=advanced["time_source"],
        log_level=advanced["log_level"],
    )


def _port(item: dict[str, Any]) -> PortConfig:
    """Raises re.error when the prompt is not a regular expression."""
    connection = item["connection"]
    if connection == "demo":
        v = {**_DEFAULTS, "reset_line": "dtr", "line_ending": "LF", "prompt": None, "values": True}
    else:
        own, group = _fields(connection)
        v = {**_DEFAULTS, **_read(item, own), **_read(item.get("advanced", {}), group)}
    return PortConfig(
        name=item.get("name", ""),
        connection=connection,
        port=v["port"],
        host=v["host"],
        tcp_port=None if v["tcp_port"] is None else int(v["tcp_port"]),
        baud=int(v["baud"]),
        data_bits=int(v["data_bits"]),
        parity=v["parity"],
        stop_bits=float(v["stop_bits"]),
        rtscts=v["flow_control"] == "rts/cts",
        xonxoff=v["flow_control"] == "xon/xoff",
        dtr=v["dtr"] == "on",
        rts=v["rts"] == "on",
        reset_line=v["reset_line"],
        line_ending=_LINE_ENDINGS[v["line_ending"]],
        prompt=re.compile(v["prompt"]) if v["prompt"] else None,
        values=v["values"],
    )


def _read(source: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """The value of each field in the source, else its schema default."""
    return {name: source.get(name, schema.get("default")) for name, schema in fields.items()}


def _leaves(error: ValidationError) -> Iterator[ValidationError]:
    """The errors worth reporting: for a failed connection branch, those of the port's own."""
    if error.validator != "oneOf":
        yield error
        return
    instance: Any = error.instance
    connection = instance.get("connection")
    # An unknown connection already has its own error from the item's `connection` enum.
    if connection in _CONNECTIONS:
        branch = _CONNECTIONS.index(connection)
        yield from (e for e in error.context if e.relative_schema_path[0] == branch)


def _where(index: int, item: Any) -> str:
    connection = item.get("connection") if isinstance(item, dict) else None
    return (
        f"Port {index} ({connection})"
        if isinstance(connection, str) and connection
        else f"Port {index}"
    )


def _problems(raw: dict[str, Any]) -> list[str]:
    """One sentence for each field with a mistake."""
    found: dict[tuple[Any, ...], str] = {}
    for error in _VALIDATOR.iter_errors(raw):
        for leaf in _leaves(error):
            for key, problem in _sentences(raw, leaf):
                # A value of the wrong type also fails the enum, which says more.
                if key not in found or leaf.validator == "enum":
                    found[key] = problem
    return list(found.values())


def _sentences(
    raw: dict[str, Any], error: ValidationError
) -> Iterator[tuple[tuple[Any, ...], str]]:
    """The problems `error` stands for, each keyed by the field it is about."""
    path: list[Any] = list(error.absolute_path)
    if path[0] == "ports" and len(path) > 1:
        where = _where(path[1] + 1, raw["ports"][path[1]])
    else:
        where = "Settings"
    schema: Any = error.schema
    if error.validator == "required":
        required: Any = error.validator_value
        instance: Any = error.instance
        for name in required:
            if name not in instance:
                title = schema.get("properties", {}).get(name, {}).get("title", name)
                yield (*path, name), f"{where}, {title}: missing."
        return
    # An error about a port as a whole has no field to name.
    title = None if path[:1] == ["ports"] and len(path) == 2 else schema.get("title")
    yield tuple(path), f"{where}{f', {title}' if title else ''}: {_wording(error, schema)}"


_KINDS = {
    "string": "text",
    "integer": "a whole number",
    "number": "a number",
    "boolean": "true or false",
    "object": "a group of settings",
    "array": "a list",
}


def _wording(error: ValidationError, schema: dict[str, Any]) -> str:
    """What a value must be, in words."""
    if error.validator == "enum":
        values = [json.dumps(v) for v in schema["enum"]]
        return f"must be {', '.join(values[:-1])} or {values[-1]}."
    if error.validator == "type":
        return f"must be {_KINDS[schema['type']]}."
    if error.validator in ("minimum", "maximum"):
        return f"must be from {schema['minimum']} to {schema['maximum']}."
    if error.validator == "minLength" or (error.validator == "pattern" and error.instance == ""):
        return "must not be empty."
    if error.validator == "pattern":
        # The one pattern in the schema: a name the trace accepts.
        return "use letters, digits, spaces, _ and - only."
    if error.validator == "maxLength":
        return f"must be at most {schema['maxLength']} characters."
    if error.validator == "additionalProperties":
        instance: Any = error.instance
        unknown = sorted(set(instance) - set(schema.get("properties", {})))
        return f"unknown setting {', '.join(unknown)}."
    return f"{error.message}."
