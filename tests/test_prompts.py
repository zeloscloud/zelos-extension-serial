"""Shell prompts: whole prompts, redrawn prompts and log lines printed right after a prompt."""

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from zelos_log_lines.formats import read
from zelos_log_lines.lines import Line

from zelos_extension_serial.prompts import is_prompt, is_prompt_redraw, log_after_prompt

PROMPTS = Path(__file__).parent / "vectors" / "prompt.txt"
CONSTRUCTED = "# constructed "
ARROW = " ⇒ "


def load(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """The cases of a vector file: each line, and what it reads as."""
    cases: list[tuple[str, dict[str, Any]]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line, arrow, expected = raw.removeprefix(CONSTRUCTED).rpartition(ARROW)
        if not arrow:
            assert not raw or raw.startswith("#"), f"{path.name}: not a case: {raw!r}"
            continue
        cases.append((line, json.loads(expected)))
    return cases


@pytest.mark.parametrize(
    ("line", "expected"), [pytest.param(*case, id=case[0]) for case in load(PROMPTS)]
)
def test_vector(line: str, expected: dict[str, Any]) -> None:
    record = read(line)
    got = {key: is_prompt(line) if key == "prompt" else getattr(record, key) for key in expected}
    assert got == expected, line


def test_prompts_have_enough_cases() -> None:
    steps = [expected for _, expected in load(PROMPTS)]
    positives = sum(expected["prompt"] for expected in steps)
    assert positives >= 15
    assert len(steps) - positives >= 10


@pytest.mark.parametrize(
    ("text", "erased", "redraw"),
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
        ("Test #3: erasing flash", True, True),
        ("Test #3: erasing flash", False, False),
    ],
)
def test_prompt_redraw(text: str, erased: bool, redraw: bool) -> None:
    assert is_prompt_redraw(Line(text, 0, None, True, False, erased)) is redraw


def _prompt_check_seconds(*texts: str) -> list[float]:
    """Best of 7 for each text, timed in turn so a burst of load hits them all alike."""
    best = [float("inf")] * len(texts)
    for _ in range(7):
        for i, text in enumerate(texts):
            start = time.perf_counter()
            is_prompt(text)
            is_prompt_redraw(Line(text, 0, None, True, False, True))
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


def test_custom_prompt_replaces_the_default() -> None:
    custom = re.compile(r"nsh> ")
    assert is_prompt("nsh> ", custom)
    assert not is_prompt("uart:~$ ", custom)
    assert is_prompt_redraw(Line("nsh> help", 0, None, True, False, True), custom)
    assert not is_prompt_redraw(Line("uart:~$ help", 0, None, True, False, True), custom)


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
        ("esp32> >phase:0.245", ">phase:0.245"),
        ("esp32> >phase", None),
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
