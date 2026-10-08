import dataclasses
import gc
import logging
import sys
from typing import Any

import pytest

from tests.conftest import TIME, Recording
from zelos_extension_serial.emitter import Emitter, SignalInfo
from zelos_extension_serial.formats import Parsed, Value

S = 1_000_000_000

LATE = (
    "'temp2' first appeared after dut/dcdc's signals were set up, so it is not recorded. "
    "Restart the extension to include it."
)


def parsed(
    *values: tuple[str, float, str | None],
    module: str | None = "dcdc",
    message: str = "msg",
    rule: str = "zephyr+kv",
) -> Parsed:
    """A device line carrying `values` as (name, value, unit)."""
    return Parsed(
        level="info",
        name=module or "",
        module=module,
        device_ns=None,
        message=message,
        file="",
        line_no=0,
        values=tuple(Value(*value) for value in values),
        rule=rule,
        prefixed=True,
    )


@pytest.fixture
def emitter(trace_file: Recording) -> Emitter:
    emitter = Emitter(trace_file.source)
    emitter.add_port("dut")
    return emitter


def events(emitter: Emitter, trace_file: Recording) -> dict[str, list[dict[str, Any]]]:
    """Flush the port, then read the recording back."""
    emitter.flush("dut")
    return trace_file.events()


def setup(emitter: Emitter, *values: tuple[str, float, str | None]) -> None:
    """Set up dut/dcdc with exactly `values`, from two lines."""
    emitter.line("dut", parsed(*values), 1_000)
    emitter.line("dut", parsed(*values), 2_000)
    emitter.flush("dut")


def test_every_line_and_note_is_a_log_row(emitter: Emitter, trace_file: Recording) -> None:
    line = dataclasses.replace(parsed(message="boot"), level="warn", file="main.cpp", line_no=42)
    emitter.line("dut", line, 1_000)
    emitter.note("dut", "error", "device restarted", 2_000)
    device = {"level": "warn", "message": "boot", "name": "dcdc", "file": "main.cpp", "line": 42}
    notice = {
        "level": "error",
        "message": "device restarted",
        "name": "serial",
        "file": "",
        "line": 0,
    }
    assert trace_file.events()["dut/log"] == [
        {TIME: 1e-6, **device},
        {TIME: 2e-6, **notice},
    ]


def test_values_group_by_module_with_missing_fields_null(
    emitter: Emitter, trace_file: Recording
) -> None:
    emitter.line("dut", parsed(("vout", 3.3, "V"), ("iout", 0.5, "A")), 1_000)
    emitter.line("dut", parsed(("vout", 3.2, "V")), 2_000)
    emitter.line("dut", parsed(("rpm", 900.0, None), module="fan"), 3_000)
    emitter.line("dut", parsed(("x", 1.0, None), module=None), 4_000)
    emitter.line("dut", parsed(), 5_000)
    recorded = events(emitter, trace_file)
    # TraceReader does not return units; the registered schema does.
    schema = trace_file.source.get_event("dut/dcdc").schema
    assert [(field.name, field.unit) for field in schema] == [("vout", "V"), ("iout", "A")]
    assert recorded["dut/dcdc"] == [
        {TIME: 1e-6, "vout": 3.3, "iout": 0.5},
        {TIME: 2e-6, "vout": 3.2, "iout": None},
    ]
    assert recorded["dut/fan"] == [{TIME: 3e-6, "rpm": 900.0}]
    assert recorded["dut/values"] == [{TIME: 4e-6, "x": 1.0}]
    assert len(recorded["dut/log"]) == 5


def test_names_printed_one_per_line_share_one_event(
    emitter: Emitter, trace_file: Recording
) -> None:
    for i, name in enumerate(["rpm", "temp", "duty", "rpm", "temp"]):
        emitter.line("dut", parsed((name, float(i), None), module=None), 1_000 * (i + 1))
    assert events(emitter, trace_file)["dut/values"] == [
        {TIME: 1e-6, "rpm": 0.0, "temp": None, "duty": None},
        {TIME: 2e-6, "rpm": None, "temp": 1.0, "duty": None},
        {TIME: 3e-6, "rpm": None, "temp": None, "duty": 2.0},
        {TIME: 4e-6, "rpm": 3.0, "temp": None, "duty": None},
        {TIME: 5e-6, "rpm": None, "temp": 4.0, "duty": None},
    ]


def test_a_name_printed_slower_than_another_is_recorded_within_two_seconds(
    emitter: Emitter, trace_file: Recording
) -> None:
    emitter.line("dut", parsed(("fast", 1.0, None)), 0)
    emitter.line("dut", parsed(("fast", 2.0, None)), S // 2)
    emitter.line("dut", parsed(("fast", 3.0, None)), S)
    assert emitter.signals("dut") == []
    emitter.line("dut", parsed(("slow", 9.0, None)), 3 * S // 2)
    emitter.flush("dut", 2 * S)
    assert [s.field for s in emitter.signals("dut")] == ["fast", "slow"]
    assert [row["slow"] for row in trace_file.events()["dut/dcdc"]] == [None, None, None, 9.0]


def test_a_module_is_set_up_two_seconds_after_its_first_held_line(emitter: Emitter) -> None:
    emitter.line("dut", parsed(("a", 1.0, None)), 1 * S)
    emitter.line("dut", parsed(("b", 2.0, None)), 2 * S)
    emitter.flush("dut", 3 * S - 1)
    assert emitter.signals("dut") == []
    emitter.flush("dut", 3 * S)
    assert [s.field for s in emitter.signals("dut")] == ["a", "b"]


def test_only_a_flush_sets_up_a_module_whatever_the_time_of_a_later_line(
    emitter: Emitter,
) -> None:
    emitter.line("dut", parsed(("a", 1.0, None)), 1 * S)
    emitter.line("dut", parsed(message="no values"), 3 * S)
    emitter.line("dut", parsed(("a", 2.0, None)), 5 * S)
    assert emitter.signals("dut") == []
    emitter.flush("dut", 3 * S)
    assert [s.field for s in emitter.signals("dut")] == ["a"]


def test_held_rows_wait_for_the_flush_and_log_rows_do_not(
    emitter: Emitter, trace_file: Recording
) -> None:
    emitter.line("dut", parsed(("a", 1.0, None), message="a=1"), 1_000)
    recorded = trace_file.events()
    assert [row["message"] for row in recorded["dut/log"]] == ["a=1"]
    assert "dut/dcdc" not in recorded


def test_flush_sets_up_every_held_module(emitter: Emitter, trace_file: Recording) -> None:
    emitter.line("dut", parsed(("a", 1.0, None)), 1_000)
    emitter.line("dut", parsed(("b", 2.0, None), module="fan"), 2_000)
    emitter.flush("dut")
    assert {s.event for s in emitter.signals("dut")} == {"dut/dcdc", "dut/fan"}
    emitter.flush("dut")
    recorded = trace_file.events()
    assert recorded["dut/dcdc"] == [{TIME: 1e-6, "a": 1.0}]
    assert recorded["dut/fan"] == [{TIME: 2e-6, "b": 2.0}]


def test_held_names_are_capped_and_the_rest_are_late(
    emitter: Emitter, trace_file: Recording, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        for i in range(70):
            emitter.line("dut", parsed((f"pkt_{i}", 1.0, None)), 1_000 + i)
    assert emitter.counters("dut") == {"signals": 64, "late_names": 6, "value_lines": 64}
    assert len(caplog.records) == 6
    assert len(events(emitter, trace_file)["dut/dcdc"]) == 64


def test_held_rows_are_capped_at_1000(emitter: Emitter) -> None:
    for i in range(999):
        emitter.line("dut", parsed(("a", float(i), None)), 1_000 + i)
    assert emitter.signals("dut") == []
    emitter.line("dut", parsed(("a", 999.0, None)), 2_000)
    assert [s.field for s in emitter.signals("dut")] == ["a"]
    assert emitter.counters("dut") == {"signals": 1, "late_names": 0, "value_lines": 1000}


def test_one_line_with_more_names_than_the_cap_records_them_all(emitter: Emitter) -> None:
    emitter.line("dut", parsed(*[(f"ch{i}", float(i), None) for i in range(100)]), 1_000)
    assert emitter.counters("dut") == {"signals": 100, "late_names": 0, "value_lines": 1}


def test_counters_count_held_lines_but_not_their_signals(emitter: Emitter) -> None:
    emitter.line("dut", parsed(("a", 1.0, None)), 1_000)
    emitter.line("dut", parsed(("b", 1.0, None)), 2_000)
    assert emitter.counters("dut") == {"signals": 0, "late_names": 0, "value_lines": 2}
    emitter.flush("dut")
    assert emitter.counters("dut") == {"signals": 2, "late_names": 0, "value_lines": 2}


def test_late_name_is_dropped_with_one_warning(
    emitter: Emitter, trace_file: Recording, caplog: pytest.LogCaptureFixture
) -> None:
    setup(emitter, ("temp", 40.0, None))
    with caplog.at_level(logging.WARNING):
        emitter.line("dut", parsed(("temp2", 41.0, None)), 3_000)
        emitter.line("dut", parsed(("temp", 42.0, None), ("temp2", 43.0, None)), 4_000)
    assert [record.getMessage() for record in caplog.records] == [LATE]
    assert emitter.counters("dut") == {"signals": 1, "late_names": 1, "value_lines": 3}
    assert events(emitter, trace_file)["dut/dcdc"] == [
        {TIME: 1e-6, "temp": 40.0},
        {TIME: 2e-6, "temp": 40.0},
        {TIME: 4e-6, "temp": 42.0},
    ]


def test_unit_mismatch_is_written_with_one_warning(
    emitter: Emitter, trace_file: Recording, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        emitter.line("dut", parsed(("vout", 3.3, "V")), 1_000)
        emitter.line("dut", parsed(("vout", 3300.0, "mV")), 2_000)
        emitter.line("dut", parsed(("vout", 3301.0, "mV")), 3_000)
        emitter.flush("dut")
    assert [record.getMessage() for record in caplog.records] == [
        "dut/dcdc: 'vout' printed in mV, first printed in V; written anyway"
    ]
    assert [row["vout"] for row in events(emitter, trace_file)["dut/dcdc"]] == [
        3.3,
        3300.0,
        3301.0,
    ]


def test_names_are_safe_and_never_collide(emitter: Emitter, trace_file: Recording) -> None:
    emitter.line("dut", parsed(("a.b", 1.0, None), ("a_b", 2.0, None), ("x y!", 3.0, None)), 1_000)
    emitter.line("dut", parsed(("v", 1.0, None), module="log"), 2_000)
    emitter.line("dut", parsed(("v", 2.0, None), module="values"), 3_000)
    emitter.line("dut", parsed(("v", 3.0, None), module="m.x"), 4_000)
    emitter.line("dut", parsed(("v", 4.0, None), module="m_x"), 5_000)
    recorded = events(emitter, trace_file)
    assert recorded["dut/dcdc"] == [{TIME: 1e-6, "a_b": 1.0, "a_b_2": 2.0, "x y": 3.0}]
    assert recorded["dut/log_2"] == [{TIME: 2e-6, "v": 1.0}]
    assert recorded["dut/values_2"] == [{TIME: 3e-6, "v": 2.0}]
    assert recorded["dut/m_x"] == [{TIME: 4e-6, "v": 3.0}]
    assert recorded["dut/m_x_2"] == [{TIME: 5e-6, "v": 4.0}]


def test_names_that_differ_only_in_case_are_all_recorded(
    emitter: Emitter, trace_file: Recording
) -> None:
    # The trace store folds case: two such names in one recording leave it empty.
    emitter.line("dut", parsed(("A", 1.0, None), ("a", 2.0, None)), 1_000)
    emitter.line("dut", parsed(("TIME_NS", 3.0, None), ("Time_Ns", 4.0, None)), 2_000)
    emitter.line("dut", parsed(("v", 5.0, None), module="MAIN"), 3_000)
    emitter.line("dut", parsed(("v", 6.0, None), module="main"), 4_000)
    emitter.line("dut", parsed(("v", 7.0, None), module="LOG"), 5_000)
    emitter.line("dut", parsed(("v", 8.0, None), module="Values"), 6_000)
    recorded = events(emitter, trace_file)
    assert recorded["dut/dcdc"] == [
        {TIME: 1e-6, "A": 1.0, "a_2": 2.0, "TIME_NS_2": None, "Time_Ns_3": None},
        {TIME: 2e-6, "A": None, "a_2": None, "TIME_NS_2": 3.0, "Time_Ns_3": 4.0},
    ]
    assert recorded["dut/MAIN"] == [{TIME: 3e-6, "v": 5.0}]
    assert recorded["dut/main_2"] == [{TIME: 4e-6, "v": 6.0}]
    assert recorded["dut/LOG_2"] == [{TIME: 5e-6, "v": 7.0}]
    assert recorded["dut/Values_2"] == [{TIME: 6e-6, "v": 8.0}]
    assert len(recorded["dut/log"]) == 6


def test_duplicate_name_in_one_line_last_wins(emitter: Emitter, trace_file: Recording) -> None:
    emitter.line("dut", parsed(("a", 1.0, None), ("a", 2.0, None)), 1_000)
    emitter.line("dut", parsed(("a", 3.0, None), ("a", 4.0, None)), 2_000)
    emitter.flush("dut")
    assert emitter.counters("dut")["signals"] == 1
    assert events(emitter, trace_file)["dut/dcdc"] == [
        {TIME: 1e-6, "a": 2.0},
        {TIME: 2e-6, "a": 4.0},
    ]


def test_signals_report_the_last_line_that_wrote_each_field(emitter: Emitter) -> None:
    emitter.line("dut", parsed(("vout", 3.3, "V"), ("iout", 0.5, "A"), message="first"), 1_000)
    second = parsed(("vout", 3.2, "V"), message="second", rule="zephyr+label-value")
    emitter.line("dut", second, 2_000)
    emitter.line("dut", parsed(("x", 1.0, None), module=None, rule="plain+plotter"), 3_000)
    emitter.flush("dut")
    assert emitter.signals("dut") == [
        SignalInfo("dut/dcdc", "vout", "V", "zephyr+label-value", "second"),
        SignalInfo("dut/dcdc", "iout", "A", "zephyr+kv", "first"),
        SignalInfo("dut/values", "x", None, "plain+plotter", "msg"),
    ]
    assert emitter.counters("dut") == {"signals": 3, "late_names": 0, "value_lines": 3}


def test_late_names_are_capped(emitter: Emitter, caplog: pytest.LogCaptureFixture) -> None:
    setup(emitter, ("v", 1.0, None))
    late = [(f"pkt_{i}", 1.0, None) for i in range(40)]
    with caplog.at_level(logging.WARNING):
        emitter.line("dut", parsed(*late), 3_000)
        emitter.line("dut", parsed(("pkt_99", 1.0, None)), 4_000)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 33
    assert all(message.startswith("'pkt_") for message in messages[:32])
    assert messages[32] == "dut/dcdc: further new values are dropped without a warning"
    assert emitter.counters("dut")["late_names"] == 32


def test_late_names_do_not_grow_the_emitter(emitter: Emitter) -> None:
    setup(emitter, ("v", 1.0, None))

    def flood(start: int) -> int:
        for i in range(start, start + 1_000):
            emitter.line("dut", parsed((f"pkt_{i}", 1.0, None)), 3_000 + i)
        return deep_size(emitter)

    assert flood(0) == flood(1_000)


def test_values_named_like_log_at_parameters_reach_the_trace(
    emitter: Emitter, trace_file: Recording
) -> None:
    emitter.line(
        "dut", parsed(("self", 1.0, None), ("name", 2.0, None), ("level", 3.0, None)), 1_000
    )
    emitter.line("dut", parsed(("time_ns", 4.0, None), ("v", 5.0, None), module="t"), 2_000)
    recorded = events(emitter, trace_file)
    assert recorded["dut/dcdc"] == [{TIME: 1e-6, "self": 1.0, "name": 2.0, "level": 3.0}]
    assert recorded["dut/t"] == [{TIME: 2e-6, "time_ns_2": 4.0, "v": 5.0}]


def test_a_value_named_time_s_keeps_its_own_column(emitter: Emitter, trace_file: Recording) -> None:
    emitter.line("dut", parsed(("time_s", 4.0, None)), 1_000)
    assert events(emitter, trace_file)["dut/dcdc"] == [{TIME: 1e-6, "time_s": 4.0}]


def test_modules_are_capped_and_their_lines_still_logged(
    emitter: Emitter, trace_file: Recording, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        for i in range(100):
            emitter.line("dut", parsed(("v", 1.0, None), module=f"tag{i}"), 1_000 + i)
    assert [record.getMessage() for record in caplog.records] == [
        "dut: further modules are not recorded as signals; their lines are still logged"
    ]
    emitter.flush("dut")
    assert emitter.counters("dut") == {"signals": 64, "late_names": 0, "value_lines": 64}
    recorded = trace_file.events()
    assert len(recorded) == 65
    assert "dut/tag63" in recorded
    assert "dut/tag64" not in recorded
    assert len(recorded["dut/log"]) == 100


def test_modules_past_the_cap_do_not_grow_the_emitter(emitter: Emitter) -> None:
    for i in range(64):
        emitter.line("dut", parsed(("v", 1.0, None), module=f"tag{i}"), 1_000 + i)

    def flood(start: int) -> int:
        for i in range(start, start + 1_000):
            emitter.line("dut", parsed(("v", 1.0, None), module=f"tag{i}"), 2_000 + i)
        return deep_size(emitter)

    assert flood(64) == flood(1_064)


def deep_size(root: object) -> int:
    """Bytes held by `root` and everything it references, classes excluded."""
    seen: set[int] = set()
    stack = [root]
    total = 0
    while stack:
        obj = stack.pop()
        if id(obj) in seen or isinstance(obj, type):
            continue
        seen.add(id(obj))
        total += sys.getsizeof(obj)
        stack.extend(gc.get_referents(obj))
    return total
