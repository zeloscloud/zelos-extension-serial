"""Configuration loading: defaults, mappings and the sentence for every kind of mistake."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from zelos_extension_serial.config import ConfigError, PortConfig, load

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "config.schema.json").read_text())
BRANCHES = SCHEMA["properties"]["ports"]["items"]["dependencies"]["connection"]["oneOf"]

SERIAL = {"connection": "serial", "port": "COM7"}
TCP = {"connection": "tcp", "host": "127.0.0.1", "tcp_port": 4001}
RFC2217 = {"connection": "rfc2217", "host": "10.0.0.5", "tcp_port": 2217}
DEMO = {"connection": "demo"}

COMMON = {
    "name": "",
    "baud": 115200,
    "data_bits": 8,
    "parity": "none",
    "stop_bits": 1.0,
    "rtscts": False,
    "xonxoff": False,
    "dtr": True,
    "rts": True,
    "reset_line": "none",
    "line_ending": b"\n",
    "prompt": None,
    "values": True,
}


def write(tmp_path: Path, config: Any) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def ports(tmp_path: Path, *items: dict[str, Any]) -> tuple[PortConfig, ...]:
    return load(write(tmp_path, {"ports": list(items)})).ports


def problems(tmp_path: Path, config: Any) -> list[str]:
    with pytest.raises(ConfigError) as raised:
        load(write(tmp_path, config))
    return raised.value.problems


def problems_at(path: Path) -> list[str]:
    with pytest.raises(ConfigError) as raised:
        load(path)
    return raised.value.problems


def advanced(base: dict[str, Any], **settings: Any) -> dict[str, Any]:
    return {**base, "advanced": settings}


# --- where the file is read from ---


def test_explicit_path_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    write(other, {"ports": [DEMO]})
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(other / "config.json"))
    assert ports(tmp_path, TCP)[0].connection == "tcp"


def test_path_comes_from_zelos_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(write(tmp_path, {"ports": [DEMO]})))
    assert load().ports[0].connection == "demo"


def test_path_falls_back_to_config_json_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"ports": [TCP]})
    monkeypatch.delenv("ZELOS_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load().ports[0].connection == "tcp"


def test_missing_file_asks_for_a_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        load(tmp_path / "absent.json")
    assert raised.value.problems == ["Add at least one port."]


def test_invalid_json_is_a_problem(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"ports": [')
    with pytest.raises(ConfigError) as raised:
        load(path)
    assert len(raised.value.problems) == 1
    assert raised.value.problems[0].startswith("The configuration is not valid JSON: ")


def test_file_is_read_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"ports": [{**TCP, "advanced": {"prompt": "é>"}}]}, ensure_ascii=False), "utf-8"
    )
    (port,) = load(path).ports
    assert port.prompt is not None
    assert port.prompt.pattern == "é>"


def test_unreadable_file_is_a_problem(tmp_path: Path) -> None:
    (problem,) = problems_at(tmp_path)
    assert problem.startswith(f"The configuration file {tmp_path} cannot be read: ")


def test_file_that_is_not_utf8_is_a_problem(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b'{"ports": [\xff\xfe]}')
    with pytest.raises(ConfigError) as raised:
        load(path)
    assert raised.value.problems == [f"The configuration file {path} is not UTF-8 text."]


# --- no ports ---


@pytest.mark.parametrize(
    "config",
    [
        {},
        [],
        {"ports": []},
        {"ports": None},
        {"advanced": {"prefix": "x"}},
        # Checked before the schema: a bad prefix does not add a second problem.
        {"ports": [], "advanced": {"prefix": "a/b"}},
    ],
)
def test_no_ports_asks_for_one_and_nothing_else(tmp_path: Path, config: Any) -> None:
    assert problems(tmp_path, config) == ["Add at least one port."]


# --- defaults ---


def test_serial_defaults(tmp_path: Path) -> None:
    assert ports(tmp_path, SERIAL) == (
        PortConfig(
            **COMMON | {"connection": "serial", "port": "COM7", "host": None, "tcp_port": None}
        ),
    )


def test_tcp_defaults(tmp_path: Path) -> None:
    assert ports(tmp_path, TCP) == (
        PortConfig(
            **COMMON | {"connection": "tcp", "port": None, "host": "127.0.0.1", "tcp_port": 4001}
        ),
    )


def test_rfc2217_defaults(tmp_path: Path) -> None:
    assert ports(tmp_path, RFC2217) == (
        PortConfig(
            **COMMON | {"connection": "rfc2217", "port": None, "host": "10.0.0.5", "tcp_port": 2217}
        ),
    )


def test_demo_has_fixed_values(tmp_path: Path) -> None:
    expected = PortConfig(
        **COMMON
        | {"connection": "demo", "port": None, "host": None, "tcp_port": None, "reset_line": "dtr"}
    )
    assert ports(tmp_path, DEMO) == (expected,)


def test_demo_ignores_every_other_setting(tmp_path: Path) -> None:
    noisy = {
        **DEMO,
        "baud": 9600,
        "host": "h",
        "advanced": {"parity": "odd", "line_ending": "CR", "prompt": "(", "values": False},
    }
    (port,) = ports(tmp_path, noisy)
    assert port == ports(tmp_path, DEMO)[0]


def explicit_defaults(branch: dict[str, Any]) -> dict[str, Any]:
    """The branch's required fields plus every default the schema declares, written out."""
    item: dict[str, Any] = {"connection": branch["properties"]["connection"]["enum"][0]}
    item.update(port="COM7", host="h", tcp_port=1)
    for name in ("port", "host", "tcp_port"):
        if name not in branch["properties"]:
            del item[name]
    group: dict[str, Any] = {}
    for name, schema in branch["properties"].items():
        if name == "advanced":
            group = {n: s["default"] for n, s in schema["properties"].items() if "default" in s}
        elif "default" in schema:
            item[name] = schema["default"]
    return {**item, "advanced": group}


@pytest.mark.parametrize(
    "branch", BRANCHES[:3], ids=lambda b: b["properties"]["connection"]["enum"][0]
)
def test_writing_out_the_schema_defaults_changes_nothing(
    tmp_path: Path, branch: dict[str, Any]
) -> None:
    item = explicit_defaults(branch)
    bare = {k: v for k, v in item.items() if k in ("connection", "port", "host", "tcp_port")}
    assert item["advanced"]
    assert ports(tmp_path, item) == ports(tmp_path, bare)


def test_a_setting_the_connection_does_not_take_is_ignored(tmp_path: Path) -> None:
    item = {**TCP, "baud": 9600, "advanced": {"parity": "even", "dtr": "off", "reset_line": "rts"}}
    assert ports(tmp_path, item) == ports(tmp_path, TCP)


@pytest.mark.parametrize(
    "setting",
    [
        {"data_bits": "eight"},
        {"parity": "bogus"},
        {"line_ending": "LFX"},
        {"prompt": 5},
        {"values": "no"},
        {"flow_control": 1},
    ],
)
def test_advanced_settings_are_only_read_from_the_advanced_group(
    tmp_path: Path, setting: dict[str, Any]
) -> None:
    assert ports(tmp_path, {**SERIAL, **setting}) == ports(tmp_path, SERIAL)
    (problem,) = problems(tmp_path, {"ports": [advanced(SERIAL, **setting)]})
    title = BRANCHES[0]["properties"]["advanced"]["properties"][next(iter(setting))]["title"]
    assert problem.startswith(f"Port 1 (serial), {title}: ")


def test_a_valid_advanced_setting_at_the_top_level_is_ignored(tmp_path: Path) -> None:
    (port,) = ports(tmp_path, {**SERIAL, "parity": "even", "values": False, "prompt": "x>"})
    assert (port.parity, port.values, port.prompt) == ("none", True, None)


def test_rfc2217_has_no_modem_line_settings(tmp_path: Path) -> None:
    item = advanced(RFC2217, dtr="off", rts="off", reset_line="dtr")
    (port,) = ports(tmp_path, item)
    assert (port.dtr, port.rts, port.reset_line) == (True, True, "none")


def test_settings_defaults(tmp_path: Path) -> None:
    settings = load(write(tmp_path, {"ports": [DEMO]}))
    assert (settings.prefix, settings.time_source, settings.log_level) == ("Serial", "auto", "INFO")


def test_settings_are_read(tmp_path: Path) -> None:
    advanced_settings = {"prefix": "Bench 1", "time_source": "host", "log_level": "DEBUG"}
    settings = load(write(tmp_path, {"ports": [DEMO], "advanced": advanced_settings}))
    assert (settings.prefix, settings.time_source, settings.log_level) == (
        "Bench 1",
        "host",
        "DEBUG",
    )


def test_ports_keep_their_order(tmp_path: Path) -> None:
    result = ports(tmp_path, TCP, DEMO, SERIAL)
    assert [p.connection for p in result] == ["tcp", "demo", "serial"]


# --- mappings ---


@pytest.mark.parametrize(
    ("flow", "rtscts", "xonxoff"),
    [("none", False, False), ("rts/cts", True, False), ("xon/xoff", False, True)],
)
def test_flow_control(tmp_path: Path, flow: str, rtscts: bool, xonxoff: bool) -> None:
    (port,) = ports(tmp_path, advanced(SERIAL, flow_control=flow))
    assert (port.rtscts, port.xonxoff) == (rtscts, xonxoff)


@pytest.mark.parametrize(("value", "expected"), [("on", True), ("off", False)])
@pytest.mark.parametrize("line", ["dtr", "rts"])
def test_modem_lines_at_open(tmp_path: Path, line: str, value: str, expected: bool) -> None:
    (port,) = ports(tmp_path, advanced(SERIAL, **{line: value}))
    other = "rts" if line == "dtr" else "dtr"
    assert getattr(port, line) is expected
    assert getattr(port, other) is True


@pytest.mark.parametrize(("name", "expected"), [("LF", b"\n"), ("CRLF", b"\r\n"), ("CR", b"\r")])
def test_line_ending(tmp_path: Path, name: str, expected: bytes) -> None:
    (port,) = ports(tmp_path, advanced(SERIAL, line_ending=name))
    assert port.line_ending == expected


@pytest.mark.parametrize(("text", "number"), [("1", 1.0), ("1.5", 1.5), ("2", 2.0)])
def test_stop_bits(tmp_path: Path, text: str, number: float) -> None:
    (port,) = ports(tmp_path, advanced(SERIAL, stop_bits=text))
    assert port.stop_bits == number
    assert isinstance(port.stop_bits, float)


def test_tcp_reads_its_advanced_group(tmp_path: Path) -> None:
    item = advanced(TCP, line_ending="CR", prompt="x>", values=False)
    (port,) = ports(tmp_path, item)
    assert (port.line_ending, port.prompt, port.values) == (b"\r", re.compile("x>"), False)


def test_rfc2217_reads_its_advanced_group(tmp_path: Path) -> None:
    item = {**RFC2217, "baud": 9600, "advanced": {"parity": "odd", "flow_control": "xon/xoff"}}
    (port,) = ports(tmp_path, item)
    assert (port.baud, port.parity, port.xonxoff) == (9600, "odd", True)


def test_every_setting_at_once(tmp_path: Path) -> None:
    item = {
        **SERIAL,
        "name": "dut",
        "baud": 57600,
        "advanced": {
            "data_bits": 7,
            "parity": "even",
            "stop_bits": "2",
            "flow_control": "rts/cts",
            "dtr": "off",
            "rts": "off",
            "reset_line": "rts",
            "line_ending": "CRLF",
            "prompt": r"dut> ",
            "values": False,
        },
    }
    (port,) = ports(tmp_path, item)
    assert port == PortConfig(
        name="dut",
        connection="serial",
        port="COM7",
        host=None,
        tcp_port=None,
        baud=57600,
        data_bits=7,
        parity="even",
        stop_bits=2.0,
        rtscts=True,
        xonxoff=False,
        dtr=False,
        rts=False,
        reset_line="rts",
        line_ending=b"\r\n",
        prompt=re.compile(r"dut> "),
        values=False,
    )


# --- names ---


def test_name_is_kept_as_typed(tmp_path: Path) -> None:
    assert ports(tmp_path, {**TCP, "name": " My Port_1-b "})[0].name == " My Port_1-b "


def test_unnamed_ports_are_not_duplicates(tmp_path: Path) -> None:
    assert [p.name for p in ports(tmp_path, TCP, TCP, {**TCP, "name": ""})] == ["", "", ""]


def test_duplicate_name_is_a_problem(tmp_path: Path) -> None:
    config = {"ports": [{**TCP, "name": "dut"}, DEMO, {**SERIAL, "name": "dut"}]}
    assert problems(tmp_path, config) == ["Port 3 (serial): name 'dut' is already used by port 1."]


def test_each_extra_duplicate_names_the_first_port(tmp_path: Path) -> None:
    config = {"ports": [{**TCP, "name": "a"}, {**TCP, "name": "a"}, {**TCP, "name": "a"}]}
    assert problems(tmp_path, config) == [
        "Port 2 (tcp): name 'a' is already used by port 1.",
        "Port 3 (tcp): name 'a' is already used by port 1.",
    ]


def test_names_that_differ_only_in_case_are_duplicates(tmp_path: Path) -> None:
    """The trace store ignores case, so dut and DUT would leave the recording empty."""
    config = {"ports": [{**TCP, "name": "dut"}, {**TCP, "name": "DUT"}]}
    assert problems(tmp_path, config) == ["Port 2 (tcp): name 'DUT' is already used by port 1."]


# --- prompt ---


def test_prompt_is_compiled(tmp_path: Path) -> None:
    (port,) = ports(tmp_path, advanced(TCP, prompt=r"^\w+@\w+:~\$ $"))
    assert port.prompt == re.compile(r"^\w+@\w+:~\$ $")


def test_empty_prompt_means_the_default_grammar(tmp_path: Path) -> None:
    assert ports(tmp_path, advanced(TCP, prompt=""))[0].prompt is None


def test_invalid_prompt_names_the_port(tmp_path: Path) -> None:
    config = {"ports": [DEMO, advanced(TCP, prompt="(unclosed")]}
    (problem,) = problems(tmp_path, config)
    with pytest.raises(re.error) as error:
        re.compile("(unclosed")
    assert problem == f"Port 2 (tcp), Prompt: is not a valid regular expression ({error.value})."


def test_prompt_and_duplicate_problems_are_reported_together(tmp_path: Path) -> None:
    config = {
        "ports": [
            {**advanced(TCP, prompt="["), "name": "a"},
            {**TCP, "name": "b"},
            {**TCP, "name": "b"},
        ]
    }
    first, second = problems(tmp_path, config)
    assert first.startswith("Port 1 (tcp), Prompt: is not a valid regular expression")
    assert second == "Port 3 (tcp): name 'b' is already used by port 2."


# --- mistakes ---


def test_a_mistake_names_the_port_and_field(tmp_path: Path) -> None:
    (low,) = problems(tmp_path, {"ports": [DEMO, {**SERIAL, "baud": 49}]})
    assert low == "Port 2 (serial), Baud: must be from 50 to 20000000."
    (missing,) = problems(tmp_path, {"ports": [DEMO, {"connection": "tcp", "host": "h"}]})
    assert missing == "Port 2 (tcp), TCP port: missing."


def test_only_the_ports_own_connection_is_reported(tmp_path: Path) -> None:
    assert problems(tmp_path, {"ports": [{"connection": "tcp"}]}) == [
        "Port 1 (tcp), Host: missing.",
        "Port 1 (tcp), TCP port: missing.",
    ]


@pytest.mark.parametrize(
    ("item", "problem"),
    [
        (
            {"connection": "usb"},
            'Port 2 (usb), Connection: must be "serial", "tcp", "rfc2217" or "demo".',
        ),
        ({"host": "h"}, "Port 2, Connection: missing."),
        ("COM7", "Port 2: must be a group of settings."),
        ({**SERIAL, "port": ""}, "Port 2 (serial), Port: must not be empty."),
        ({**SERIAL, "baud": "fast"}, "Port 2 (serial), Baud: must be a whole number."),
        ({**TCP, "tcp_port": 70000}, "Port 2 (tcp), TCP port: must be from 1 to 65535."),
        ({**TCP, "name": "a/b"}, "Port 2 (tcp), Name: use letters, digits, spaces, _ and - only."),
        ({**TCP, "name": "x" * 65}, "Port 2 (tcp), Name: must be at most 64 characters."),
        (advanced(SERIAL, stop_bits=1), 'Port 2 (serial), Stop bits: must be "1", "1.5" or "2".'),
        (advanced(SERIAL, data_bits=9), "Port 2 (serial), Data bits: must be 5, 6, 7 or 8."),
        (
            advanced(SERIAL, values="no"),
            "Port 2 (serial), Turn printed values into signals: must be true or false.",
        ),
        (
            advanced(TCP, line_ending="\n"),
            'Port 2 (tcp), Line ending: must be "LF", "CRLF" or "CR".',
        ),
        ({**SERIAL, "advanced": "x"}, "Port 2 (serial), Advanced: must be a group of settings."),
    ],
)
def test_each_kind_of_mistake_gives_one_plain_problem(
    tmp_path: Path, item: Any, problem: str
) -> None:
    assert problems(tmp_path, {"ports": [DEMO, item]}) == [problem]


def test_every_mistake_is_reported_in_one_error(tmp_path: Path) -> None:
    config = {"ports": [{"connection": "tcp", "host": "h"}, DEMO, {**SERIAL, "baud": 1}]}
    result = problems(tmp_path, config)
    assert [p.split(":")[0] for p in result] == ["Port 1 (tcp), TCP port", "Port 3 (serial), Baud"]


def test_error_message_joins_the_problems(tmp_path: Path) -> None:
    config = {"ports": [{"connection": "tcp", "host": "h"}, {**SERIAL, "baud": 1}]}
    with pytest.raises(ConfigError, match=r"TCP port: missing\.; Port 2 \(serial\), Baud: "):
        load(write(tmp_path, config))


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        ({"ports": "abc"}, "Settings, Ports: must be a list."),
        (
            {"ports": [DEMO], "advanced": {"prefix": "a/b"}},
            "Settings, Prefix: use letters, digits, spaces, _ and - only.",
        ),
        ({"ports": [DEMO], "advanced": {"prefix": ""}}, "Settings, Prefix: must not be empty."),
        (
            {"ports": [DEMO], "advanced": {"log_level": "TRACE"}},
            'Settings, Log level: must be "DEBUG", "INFO", "WARNING" or "ERROR".',
        ),
        (
            {"ports": [DEMO], "advanced": {"colour": 1}},
            "Settings, Advanced (all ports): unknown setting colour.",
        ),
        (
            {"ports": [DEMO], "advanced": {"time_source": 1}},
            'Settings, Time source: must be "auto" or "host".',
        ),
    ],
)
def test_mistake_in_the_settings(tmp_path: Path, config: Any, problem: str) -> None:
    assert problems(tmp_path, config) == [problem]


def test_whole_numbers_written_as_floats_become_ints(tmp_path: Path) -> None:
    item = {**RFC2217, "tcp_port": 2217.0, "baud": 9600.0, "advanced": {"data_bits": 7.0}}
    (port,) = ports(tmp_path, item)
    assert (port.tcp_port, port.baud, port.data_bits) == (2217, 9600, 7)
    assert {type(v) for v in (port.tcp_port, port.baud, port.data_bits)} == {int}


# --- the schema file ---


def test_schema_is_valid_draft_7() -> None:
    Draft7Validator.check_schema(SCHEMA)


def test_every_enum_label_list_matches_its_enum() -> None:
    def walk(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    labelled = [n for n in walk(SCHEMA) if "ui:enumNames" in n]
    assert labelled
    for node in labelled:
        assert len(node["ui:enumNames"]) == len(node["enum"]), node["title"]


def test_every_schema_example_loads(tmp_path: Path) -> None:
    for example in SCHEMA["examples"]:
        assert len(load(write(tmp_path, example)).ports) == len(example["ports"])
