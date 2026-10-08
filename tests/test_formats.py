"""Line parsing: log prefixes, value habits, prompts and the per-port trackers."""

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_serial.formats import (
    Parsed,
    RepeatTracker,
    ShapeTracker,
    is_prompt,
    is_prompt_redraw,
    log_after_prompt,
    parse,
)
from zelos_extension_serial.lines import Line

VECTORS = Path(__file__).parent / "vectors"
CONSTRUCTED = "# constructed "
ARROW = " ⇒ "


def load(path: Path) -> list[list[tuple[str, dict[str, Any]]]]:
    """Cases from one vector file; a `---` block is one case whose lines share history."""
    cases: list[list[tuple[str, dict[str, Any]]]] = []
    block: list[tuple[str, dict[str, Any]]] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw == "---":
            if block is not None:
                cases.append(block)
            block = [] if block is None else None
            continue
        line, arrow, expected = raw.removeprefix(CONSTRUCTED).rpartition(ARROW)
        if not arrow:
            assert not raw or raw.startswith("#"), f"{path.name}: not a case: {raw!r}"
            continue
        step = (line, json.loads(expected))
        if block is None:
            cases.append([step])
        else:
            block.append(step)
    assert block is None, f"{path.name}: unclosed --- block"
    return cases


FILES = sorted(VECTORS.glob("*.txt"))
CASES = [
    pytest.param(case, id=f"{path.stem}:{case[0][0]}") for path in FILES for case in load(path)
]


def actual(parsed: Parsed, line: str, key: str) -> Any:
    if key == "prompt":
        return is_prompt(line)
    if key == "values":
        return [[v.name, v.value, v.unit] for v in parsed.values]
    return getattr(parsed, key)


@pytest.mark.parametrize("case", CASES)
def test_vector(case: list[tuple[str, dict[str, Any]]]) -> None:
    shape, repeat = ShapeTracker(), RepeatTracker()
    for line, expected in case:
        parsed = parse(line, None, shape, repeat)
        got = {key: actual(parsed, line, key) for key in expected}
        assert got == expected, line


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_every_rule_has_enough_cases(path: Path) -> None:
    def positive(expected: dict[str, Any]) -> bool:
        if path.stem == "prompt":
            return expected["prompt"]
        return path.stem in expected["rule"].split("+")

    steps = [expected for case in load(path) for _, expected in case]
    positives = sum(map(positive, steps))
    assert positives >= 15
    assert len(steps) - positives >= 10


@pytest.mark.parametrize(
    ("colour", "level"), [(None, "info"), ("yellow", "warn"), ("red", "error")]
)
def test_colour_sets_the_level_of_a_plain_line(colour: str | None, level: str) -> None:
    assert parse("motor stalled", colour, ShapeTracker(), RepeatTracker()).level == level


@pytest.mark.parametrize(
    "text",
    [
        "[00:00:01.000,000] <inf> main: ok",
        "I (56) boot: ok",
        "[    0.000000] ok",
        "Info: ok",
    ],
)
def test_colour_never_overrides_a_prefix(text: str) -> None:
    assert parse(text, "red", ShapeTracker(), RepeatTracker()).level == "info"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[00:00:05.000,000] <wrn> bms: cell imbalance 30mV", "bms: cell imbalance 30mV"),
        ("[00:00:00.106,051] <inf> [  0 main] app.inst1: up", "[  0 main] app.inst1: up"),
        ("[0000003001] <inf> sample_module: log 11", "sample_module: log 11"),
        ("E (588) SPIFFS: mount failed, -10025", "SPIFFS: mount failed, -10025"),
        ("I (558) wifi:wifi driver task: 3ffc1e4c", "wifi:wifi driver task: 3ffc1e4c"),
        (
            "[  1065][E][sd_diskio.cpp:807] sdcard_mount(): f_mount failed",
            "[sd_diskio.cpp:807] sdcard_mount(): f_mount failed",
        ),
        ("[    12][I][sketch.ino:7] hello", "[sketch.ino:7] hello"),
        ("[    1.234567] usb 1-1: new device", "usb 1-1: new device"),
        ("[    0.000000][    T0] Booting Linux", "[    T0] Booting Linux"),
        ("ERROR: x", "x"),
        ("[WRN] low battery", "low battery"),
        ("warning: gcc style", "warning: gcc style"),
        ("--- 3 messages dropped ---", "--- 3 messages dropped ---"),
    ],
)
def test_the_message_drops_only_the_stamp_and_a_level_marker_the_format_reads(
    text: str, message: str
) -> None:
    assert parse(text, None, ShapeTracker(), RepeatTracker()).message == message


@pytest.mark.parametrize(
    "text",
    [
        "[00:00:00.020,000] <inf> dcdc: 1 2 3",
        "I (5) dcdc: 1 2 3",
        "[  5][I][dcdc.cpp:1] poll(): 1 2 3",
        "[    1.000000][    T1] 1 2 3",
    ],
)
def test_values_never_come_from_the_module_file_or_caller(text: str) -> None:
    shape, repeat = ShapeTracker(), RepeatTracker()
    values = [parse(text, None, shape, repeat).values for _ in range(3)]
    assert [[v.value for v in line] for line in values] == [[], [], [1, 2, 3]]


@pytest.mark.parametrize(
    ("lines", "rule"),
    [
        (["[00:00:00.020,000] <inf> dcdc: up"], "zephyr"),
        (["[00:00:00.020,000] <inf> dcdc: rail=13.65V"], "zephyr+kv"),
        (["[    0.000000] up"], "linux"),
        (["1,2", "1,2", "1,2"], "plain+plotter"),
        (["I (56) wifi: Retries: 3", "I (57) wifi: Retries: 4"], "esp-idf+label-value"),
    ],
)
def test_rule_names_the_prefix_then_the_habit(lines: list[str], rule: str) -> None:
    shape, repeat = ShapeTracker(), RepeatTracker()
    parsed = [parse(line, None, shape, repeat) for line in lines]
    assert parsed[-1].rule == rule


def test_shape_tracker_needs_three_equal_counts_in_a_row() -> None:
    shape = ShapeTracker()
    assert [shape.observe(n) for n in (2, 2, 2, 2, 3, 3, 2, 3, 3, 3)] == [
        *(False, False, True, True),
        *(False, False, False, False, False, True),
    ]


def test_shape_tracker_never_plots_zero_columns() -> None:
    shape = ShapeTracker()
    assert not any(shape.observe(0) for _ in range(5))


def test_shape_is_observed_once_for_every_line() -> None:
    class Counting(ShapeTracker):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[int] = []

        def observe(self, columns: int) -> bool:
            self.calls.append(columns)
            return super().observe(columns)

    shape, repeat = Counting(), RepeatTracker()
    lines = [">a:1", "a=1", "a:1", "1 2", "Temp: 5", "--- 1 messages dropped ---", "x", ""]
    for line in lines:
        parse(line, None, shape, repeat)
    assert shape.calls == [0, 0, 0, 2, 0, 0, 0, 0]


def test_repeat_tracker_forgets_everything_at_1024_labels() -> None:
    repeat = RepeatTracker()
    for n in range(1024):
        repeat.seen(f"label {n}")
    assert repeat.seen("label 1023")
    assert not repeat.seen("label 1024")
    assert not repeat.seen("label 0")


def test_repeat_tracker_remembers_each_label() -> None:
    repeat = RepeatTracker()
    assert [repeat.seen(label) for label in ("a", "b", "a", "a", "b")] == [
        False,
        False,
        True,
        True,
        True,
    ]


@pytest.mark.parametrize(
    ("text", "partial", "redraw"),
    [
        ("uart:~$ dcdc li", True, True),
        ("uart:~$ dcdc li", False, False),
        ("uart:~$ ", True, False),
        ("root@host:~# ls -l", True, True),
        (">>> print(", True, True),
        ("[00:00:00.020,000] <inf> dc", True, False),
        ("dcdc li", True, False),
        ("[esp32]> help", True, True),
        ("pi@raspberrypi:~ $ ls", True, True),
        ("uart:~$ [00:00:19.024,000] <inf> pump: rpm=10", True, False),
        ("# [   12.345678] eth0: li", True, False),
    ],
)
def test_prompt_redraw(text: str, partial: bool, redraw: bool) -> None:
    assert is_prompt_redraw(Line(text, 0, None, partial, False)) is redraw


def _prompt_check_seconds(*texts: str) -> list[float]:
    """Best of 7 for each text, timed in turn so a burst of load hits them all alike."""
    best = [float("inf")] * len(texts)
    for _ in range(7):
        for i, text in enumerate(texts):
            start = time.perf_counter()
            is_prompt(text)
            is_prompt_redraw(Line(text, 0, None, True, False))
            best[i] = min(best[i], time.perf_counter() - start)
    return best


@pytest.mark.parametrize("unit", ["a", "0123456789abcdef", "0a"], ids=["letters", "hex", "mixed"])
def test_prompt_check_is_linear_on_a_long_line(unit: str) -> None:
    # A ratio, not a time limit, so a slow runner cannot fail it: eight times the text costs
    # about eight times as long when linear, and sixty-four times when quadratic.
    # Short enough that a quadratic grammar fails in seconds: 8 KiB costs it about 2 s.
    short = unit * (1024 // len(unit))
    short_s, long_s = _prompt_check_seconds(short, short * 8)
    assert long_s / short_s < 24


def _parse_seconds(*texts: str) -> list[float]:
    """Best of 7 for each text, timed in turn so a burst of load hits them all alike."""
    best = [float("inf")] * len(texts)
    for _ in range(7):
        for i, text in enumerate(texts):
            start = time.perf_counter()
            parse(text, None, ShapeTracker(), RepeatTracker())
            best[i] = min(best[i], time.perf_counter() - start)
    return best


@pytest.mark.parametrize(
    ("head", "unit", "tail"),
    [
        ("", "1", ""),
        ("a=", "0", "g"),
        ("a=", "1", "g"),
        ("a=", "1f", "g"),
        ("", "a=1 ", ""),
        (">a:", "1;", "x"),
        ("", "0a", ""),
        ("Temp: ", "1", " C x"),
    ],
    ids=["digits", "zeros", "kv", "hex-kv", "kv-list", "teleplot", "hex", "label-value"],
)
def test_parse_is_linear_on_a_long_line(head: str, unit: str, tail: str) -> None:
    # The same ratio as the prompt check: linear costs about 8 times, quadratic 64.
    short = unit * (512 // len(unit))
    short_s, long_s = _parse_seconds(head + short + tail, head + short * 8 + tail)
    assert long_s / short_s < 24


def test_custom_prompt_replaces_the_default() -> None:
    custom = re.compile(r"nsh> ")
    assert is_prompt("nsh> ", custom)
    assert not is_prompt("uart:~$ ", custom)
    assert is_prompt_redraw(Line("nsh> help", 0, None, True, False), custom)
    assert not is_prompt_redraw(Line("uart:~$ help", 0, None, True, False), custom)


@pytest.mark.parametrize(
    ("text", "log"),
    [
        (
            "uart:~$ [00:00:19.024,000] <inf> pump: rpm=1042 temp=23.5C",
            "[00:00:19.024,000] <inf> pump: rpm=1042 temp=23.5C",
        ),
        ("# [   12.345678] eth0: link up", "[   12.345678] eth0: link up"),
        ("esp32> I (558) wifi: started", "I (558) wifi: started"),
        ("uart:~$ ", None),
        ("$ ls -la", None),
        ("# comment", None),
        (">>> 1+1", None),
        ("# ERROR: no such file", None),
        ("uart:~$ --- 3 messages dropped ---", None),
        ("[00:00:19.024,000] <inf> pump: rpm=1042", None),
    ],
)
def test_a_log_line_printed_right_after_a_prompt(text: str, log: str | None) -> None:
    assert log_after_prompt(text) == log


def test_a_log_line_after_a_custom_prompt() -> None:
    line = "nsh> [   12.345678] eth0: link up"
    assert log_after_prompt(line, re.compile(r"nsh> ")) == "[   12.345678] eth0: link up"
    assert log_after_prompt(line, re.compile(r"ok> ")) is None
    # A prompt pattern that can match nothing does not make every log line a prompt.
    assert log_after_prompt("[   12.345678] eth0: link up", re.compile(r"(ok> )?")) is None


@pytest.mark.parametrize(
    "text",
    [
        "[" + "1" * 5000 + ":00:00.000,000] <inf> main: x",
        "[" + "1" * 5000 + ".000000] <inf> main: x",
        "[" + "1" * 5000 + ".000000] x",
        "[" + "1" * 5000 + "][I][main.cpp:" + "1" * 5000 + "] x",
        "I (" + "1" * 5000 + ") main: x",
    ],
    ids=["zephyr", "zephyr-seconds", "linux", "arduino-esp32", "esp-idf"],
)
def test_a_clock_too_long_for_int_is_no_clock(text: str) -> None:
    parsed = parse(text, None, ShapeTracker(), RepeatTracker())
    assert parsed.prefixed
    assert parsed.device_ns is None


SYMBOLS = "abcxyzEeXx:=;,.>-+[]()<>§|%°µ_ \t"


# Letters whose Unicode case folding reaches an ASCII letter, as `ı` and `İ` reach `i`.
LOOKALIKES = {"i": "ıİ", "s": "ſ", "k": "\u212a"}
LEVEL_WORDS = [
    "fatal",
    "critical",
    "error",
    "err",
    "warning",
    "warn",
    "info",
    "debug",
    "dbg",
    "trace",
]
PREFIXES = [
    "[{d}:{d}:{d}.{d},{d}] <inf> {t}",
    "[ {d}.{d}] <wrn> {t}",
    "[{d}] <dbg> {t}",
    "E ({d}) {t}: {t}",
    "[ {d}][E][{t}:{d}] {t}",
    "[ {d}.{d}] {t}",
    "[ {d}.{d}][ T{d}] {t}",
    "[{d}-{d}-{d} {d}:{d}:{d}.{d},{d}] <err> {t}",
    "[{d}-{d}-{d}T{d}:{d}:{d},{d}Z] <err> {t}",
    "[{d}:{d}:{d}.{d},{d}] <inf> [ {d} {t}] [core {d}] {t}",
    "I ({d}) {t}:{t}",
    "{t}={d}{t}",
]


@st.composite
def disguised_level_words(draw: st.DrawFn) -> str:
    """A bracketed level word with letters swapped for case-folding lookalikes."""
    word = draw(st.sampled_from(LEVEL_WORDS))
    letters = (draw(st.sampled_from([c, c.upper(), *LOOKALIKES.get(c, "")])) for c in word)
    return f"[{''.join(letters)}] {draw(st.text())}"


@st.composite
def prefixed_lines(draw: st.DrawFn) -> str:
    """A known log prefix with digits from any script and arbitrary text in each slot."""
    digits = draw(st.text(st.characters(categories=["Nd"]), min_size=1, max_size=6))
    prefix = draw(st.sampled_from(PREFIXES)).format(d=digits, t=draw(st.text(max_size=8)))
    return prefix + draw(st.text())


@given(
    st.lists(
        st.one_of(
            st.text(),
            st.text("0123456789" + SYMBOLS),
            disguised_level_words(),
            prefixed_lines(),
        ),
        max_size=5,
    ),
    st.sampled_from([None, "red", "yellow"]),
)
def test_parse_never_raises(lines: list[str], colour: str | None) -> None:
    shape, repeat = ShapeTracker(), RepeatTracker()
    for line in lines:
        parse(line, colour, shape, repeat)


@given(st.one_of(st.text(st.characters(exclude_categories=["Nd"])), st.text(SYMBOLS)))
def test_no_digit_no_value(text: str) -> None:
    shape, repeat = ShapeTracker(), RepeatTracker()
    for _ in range(3):
        assert parse(text, None, shape, repeat).values == ()
