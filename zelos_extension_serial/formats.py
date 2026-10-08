"""Log levels, device time and printed values from one line."""

import math
import re
from dataclasses import dataclass, replace
from typing import Literal

from .lines import Line

_Level = Literal["trace", "debug", "info", "warn", "error", "critical"]


@dataclass(frozen=True, slots=True)
class Value:
    """One printed value; `name` as printed, before safe_name()."""

    name: str
    value: float
    unit: str | None


@dataclass(frozen=True, slots=True)
class Parsed:
    """What one line says."""

    level: _Level
    name: str
    module: str | None
    device_ns: int | None
    message: str
    file: str
    line_no: int
    values: tuple[Value, ...]
    rule: str
    prefixed: bool


class ShapeTracker:
    """Per-port history of bare-number column counts, for the plotter rule."""

    def __init__(self) -> None:
        self._last = (0, 0)

    def observe(self, columns: int) -> bool:
        """True when this and the two previous calls were the same non-zero count."""
        before, previous = self._last
        self._last = (previous, columns)
        return columns > 0 and before == previous == columns


class RepeatTracker:
    """Per-port memory of labels offered by the label-value rule."""

    def __init__(self) -> None:
        self._labels: set[str] = set()

    def seen(self, label: str) -> bool:
        """True when this label was offered on an earlier line."""
        if label in self._labels:
            return True
        # Labels that embed a counter would otherwise grow this for as long as the port is open.
        if len(self._labels) >= 1024:
            self._labels.clear()
        self._labels.add(label)
        return False


_LEVELS: dict[str, _Level] = {
    "err": "error",
    "wrn": "warn",
    "inf": "info",
    "dbg": "debug",
    "E": "error",
    "W": "warn",
    "I": "info",
    "D": "debug",
    "V": "trace",
    "fatal": "critical",
    "critical": "critical",
    "crit": "critical",
    "error": "error",
    "warning": "warn",
    "warn": "warn",
    "info": "info",
    "debug": "debug",
    "trace": "trace",
}
_COLOUR_LEVELS: dict[str | None, _Level] = {"red": "error", "yellow": "warn"}

# A zero-padded integer of up to three digits is decimal (`09`, `007`); four or more led by a zero
# is hex printed with `%04x` or wider (`0403`, `00010020`). The lookahead keeps `0xFF` from reading
# as 0 with unit `xFF`.
_NUMBER = (
    r"[-+]?(?:\d++\.\d*+|\.\d++|0\d{0,2}+(?!\d)|[1-9]\d*+)(?:[eE][-+]?\d++)?(?![xX][0-9A-Fa-f])"
)
_UNIT = r"[A-Za-z%°µΩ][A-Za-z%°µΩ²³]{0,7}(?:/[A-Za-z0-9%°µΩ²³/]{1,7})?"

# The thread (`[  0 main] `, `[irq] `) and core (`[core 0] `) prefixes are options.
_ZEPHYR_TAIL = (
    r"<(err|wrn|inf|dbg)> ((?:\[(?:irq| *+-?\d++ [^\]]*+)\] )?(?:\[core \d++\] )?"
    r"(?:[\w-]+/)?([\w-]+)(?:\.[\w-]+)?:? (.*))$"
)
_ZEPHYR = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\.(\d{3}),(\d{3})\] " + _ZEPHYR_TAIL)
# The date and ISO 8601 stamps are wall-clock time, not uptime.
_ZEPHYR_DATE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2}\.\d{3},\d{3}|T\d{2}:\d{2}:\d{2},\d{6}Z)\] "
    + _ZEPHYR_TAIL
)
# Zephyr's Linux-style stamp; the level tag keeps kernel lines out.
_ZEPHYR_SECONDS = re.compile(r"^\[ *(\d+)\.(\d{6})\] " + _ZEPHYR_TAIL)
# 32-bit and 64-bit tick counters.
_ZEPHYR_TICKS = re.compile(r"^\[(?:\d{10}|\d{20})\] " + _ZEPHYR_TAIL)
_ZEPHYR_DROPPED = re.compile(r"--- \d+ messages dropped ---")
# The Wi-Fi library prints no space after its tag: `I (558) wifi:wifi driver task: …`.
_ESP_IDF = re.compile(r"^([EWIDV]) \((\d+|[\d:. -]+)\) (([^:]++): ?(.*))$")
_ARDUINO_ESP32 = re.compile(
    r"^\[ *(\d+)\]\[([VDIWE])\](\[([^:\]]+):(\d+)\] (?:[\w:~<>]+\(\): )?(.*))$"
)
# CONFIG_PRINTK_CALLER adds the task or CPU: `[    0.000000][    T0] `.
_LINUX = re.compile(r"^\[ *(\d+)\.(\d{6})\]((?:\[ *+[TC]\d++\])? (.*))$")
# A lower-case-led word needs a colon right after it: `Error count: 5` is prose.
# ASCII-only case folding: Unicode folding lets `ı` match `i`, and the word is looked up by name.
_LEVEL_WORD = re.compile(
    r"^(?:\[ *(?ai:(fatal|critical|crit|error|err|warning|warn|wrn|info|inf|debug|dbg|trace))"
    r" *\]\s*"
    r"|(FATAL|CRITICAL|ERROR|ERR|WARNING|WARN|INFO|DEBUG|TRACE)(?::\s*|\s+)"
    r"|(Fatal|Critical|Error|Warning|Warn|Info|Debug|Trace):\s*)(.*)$"
)

_TELEPLOT = re.compile(
    rf">([A-Za-z_][\w ./-]{{0,63}}):(?:(?:{_NUMBER}:)?{_NUMBER};)*(?:{_NUMBER}:)?({_NUMBER})"
    rf"(?:§({_UNIT}))?(?:\|(.*))?"
)
_KV = re.compile(rf"(?<![^ ,;(\[])([A-Za-z_][\w.]*)=({_NUMBER})({_UNIT})?(?![^ ,;)\]])")
# A value that only hex explains marks a line of hex, whose other values are hex too
# (`vaddr=40080000`): four or more digits led by a zero (`paddr=00010020`), a digit after a letter
# (`crc=7bd5c66f`, `size=0a3ech`), or digits then lower-case a-f (`x=1f`). Units such as `dB`, `A`
# or `C` are none of these.
_HEX_KV = re.compile(r"(?<![^ ,;(\[])[A-Za-z_][\w.]*=([0-9A-Fa-f]++h?)(?![^ ,;)\]])")
_HEX_SUFFIX = re.compile(r"\d++[a-f]++")
_VALUE = re.compile(rf"{_NUMBER}(?:{_UNIT})?")
_DELIMITERS = re.compile(r"[,\t ]+")
_LABELLED = re.compile(rf"([A-Za-z_][\w-]{{0,63}}):({_NUMBER})")
_BARE = re.compile(_NUMBER)
# ESP_LOG_BUFFER_HEX prints each byte as two digits after a log prefix: eight or more there is a
# dump, not columns.
_HEX_DUMP = re.compile(r"[0-9a-f]{2}(?: [0-9a-f]{2}){7,}")
_LABEL_VALUE = re.compile(rf"^([A-Za-z][\w -]{{1,31}}):\s+({_NUMBER})\s*({_UNIT})?$")
_HEX_LITERAL = re.compile(r"\b0[xX][0-9A-Fa-f]")

# Each repeated class excludes what follows it, so possessive repeats match the same strings
# without backtracking: a prompt check on a long line costs linear time.
_PROMPT = re.compile(
    r"uart:~\$ ?|(?:[^\WA-Za-z]|[.@~/-])*+[A-Za-z][\w.@:~/-]*+(?: ?[#$]|>) ?|\[[\w.-]++\]> ?"
    r"|[#$] |=> ?|>>> ?|\.\.\. |[\w.-]++ login: ?|[Pp]assword: ?"
)


def _head(
    level: _Level,
    message: str,
    body: str,
    rule: str,
    *,
    name: str = "",
    module: str | None = None,
    device_ns: int | None = None,
    file: str = "",
    line_no: int = 0,
    prefixed: bool = True,
) -> tuple[Parsed, str]:
    """The line's head and the body its values are read from.

    The message is the line as printed without what has its own column: the time stamp and
    the level. The body also drops the module, file, function or caller.
    """
    return Parsed(level, name, module, device_ns, message, file, line_no, (), rule, prefixed), body


def _int(digits: str) -> int | None:
    """None past 18 digits.

    No clock or line number prints that many, and CPython refuses to convert integers longer
    than 4,300 digits (its int max-str-digits limit).
    """
    return int(digits) if len(digits) <= 18 else None


def _zephyr(text: str) -> tuple[Parsed, str] | None:
    if m := _ZEPHYR.match(text):
        hours, minutes, seconds, ms, us = m.groups()[:5]
        h = _int(hours)
        s = None if h is None else (h * 60 + int(minutes)) * 60 + int(seconds)
        us_total = None if s is None else (s * 1000 + int(ms)) * 1000 + int(us)
    elif m := _ZEPHYR_SECONDS.match(text):
        seconds, us = m.groups()[:2]
        s = _int(seconds)
        us_total = None if s is None else s * 1_000_000 + int(us)
    elif m := _ZEPHYR_DATE.match(text):
        us_total = None
    else:
        return None
    level, message, module, body = m.groups()[-4:]
    device_ns = None if us_total is None else us_total * 1000
    return _head(
        _LEVELS[level], message, body, "zephyr", name=module, module=module, device_ns=device_ns
    )


def _zephyr_ticks(text: str) -> tuple[Parsed, str] | None:
    m = _ZEPHYR_TICKS.match(text)
    if not m:
        return None
    level, message, module, body = m.groups()
    return _head(_LEVELS[level], message, body, "zephyr-ticks", name=module, module=module)


def _zephyr_dropped(text: str) -> tuple[Parsed, str] | None:
    if not _ZEPHYR_DROPPED.fullmatch(text):
        return None
    return _head("warn", text, text, "zephyr-dropped", prefixed=False)


def _esp_idf(text: str) -> tuple[Parsed, str] | None:
    m = _ESP_IDF.match(text)
    if not m:
        return None
    level, stamp, message, tag, body = m.groups()
    # Only the default stamp is uptime; the others are wall-clock time.
    ms = _int(stamp) if stamp.isdecimal() else None
    device_ns = None if ms is None else ms * 1_000_000
    return _head(
        _LEVELS[level], message, body, "esp-idf", name=tag, module=tag, device_ns=device_ns
    )


def _arduino_esp32(text: str) -> tuple[Parsed, str] | None:
    m = _ARDUINO_ESP32.match(text)
    if not m:
        return None
    stamp, level, message, file, line_no, body = m.groups()
    module = file.rpartition(".")[0] or file
    ms = _int(stamp)
    return _head(
        _LEVELS[level],
        message,
        body,
        "arduino-esp32",
        name=module,
        module=module,
        device_ns=None if ms is None else ms * 1_000_000,
        file=file,
        line_no=_int(line_no) or 0,
    )


def _linux(text: str) -> tuple[Parsed, str] | None:
    m = _LINUX.match(text)
    if not m:
        return None
    seconds, us, message, body = m.groups()
    s = _int(seconds)
    device_ns = None if s is None else s * 1_000_000_000 + int(us) * 1000
    # Its own module: kernel lines with values never join `<port>/values`.
    return _head(
        "info",
        message.removeprefix(" "),
        body,
        "linux",
        name="kernel",
        module="kernel",
        device_ns=device_ns,
    )


def _level_word(text: str) -> tuple[Parsed, str] | None:
    m = _LEVEL_WORD.match(text)
    if not m:
        return None
    word = m[1] or m[2] or m[3]
    return _head(_LEVELS[word.lower()], m[4], m[4], "level-word", prefixed=False)


def _plain(text: str, colour: str | None) -> tuple[Parsed, str]:
    return _head(_COLOUR_LEVELS.get(colour, "info"), text, text, "plain", prefixed=False)


def _number(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) else None


def _teleplot(body: str) -> tuple[Value, ...]:
    m = _TELEPLOT.fullmatch(body)
    if not m:
        return ()
    name, number, unit, flags = m.groups()
    value = _number(number)
    # `t` marks text and `xy` a point pair, neither a time series.
    if value is None or (flags and ("t" in flags or "xy" in flags)):
        return ()
    return (Value(name, value, unit),)


def _kv(body: str) -> tuple[Value, ...]:
    for token in _HEX_KV.findall(body):
        if not token.isalpha() and (not _VALUE.fullmatch(token) or _HEX_SUFFIX.fullmatch(token)):
            return ()
    return tuple(
        Value(name, value, unit or None)
        for name, number, unit in _KV.findall(body)
        if (value := _number(number)) is not None
    )


def _plotter_labelled(tokens: list[str]) -> tuple[Value, ...]:
    values: list[Value] = []
    for token in tokens:
        m = _LABELLED.fullmatch(token)
        if not m:
            return ()
        if (value := _number(m[2])) is not None:
            values.append(Value(m[1], value, None))
    return tuple(values)


def _plotter(tokens: list[str]) -> tuple[Value, ...]:
    return tuple(
        Value(f"value{i}", value, None)
        for i, token in enumerate(tokens, 1)
        if (value := _number(token)) is not None
    )


def _label_value(body: str, repeat: RepeatTracker) -> tuple[Value, ...]:
    m = _LABEL_VALUE.match(body)
    if not m:
        return ()
    label = m[1].strip()
    # A longer label is prose: `Hit any key to stop autoboot:  2`. One with a hex ID reports on
    # that ID, not a quantity: `send of 0x300: -114`.
    if len(label.split()) > 3 or _HEX_LITERAL.search(label):
        return ()
    value = _number(m[2])
    if not repeat.seen(label) or value is None:
        return ()
    return (Value(label, value, m[3] or None),)


def _values(
    body: str, prefixed: bool, shape: ShapeTracker, repeat: RepeatTracker
) -> tuple[str, tuple[Value, ...]]:
    """The first habit that accepts the body, and the values it gives."""
    body = body.strip()
    # Arduino's plotter accepts a delimiter before the line end.
    tokens = [token for token in _DELIMITERS.split(body) if token]
    bare = (
        len(tokens) >= 2
        and all(_BARE.fullmatch(token) for token in tokens)
        and not (prefixed and _HEX_DUMP.fullmatch(body))
    )
    plotting = shape.observe(len(tokens) if bare else 0)
    if body.startswith(">"):
        return "teleplot", _teleplot(body)
    if values := _kv(body):
        return "kv", values
    if values := _plotter_labelled(tokens):
        return "plotter-labelled", values
    if bare:
        return "plotter", _plotter(tokens) if plotting else ()
    return "label-value", _label_value(body, repeat)


def _parse_head(text: str, colour: str | None) -> tuple[Parsed, str]:
    return (
        _zephyr(text)
        or _zephyr_ticks(text)
        or _zephyr_dropped(text)
        or _esp_idf(text)
        or _arduino_esp32(text)
        or _linux(text)
        or _level_word(text)
        or _plain(text, colour)
    )


def parse(text: str, colour: str | None, shape: ShapeTracker, repeat: RepeatTracker) -> Parsed:
    """Parse one line; total, never raises."""
    head, body = _parse_head(text, colour)
    habit, values = _values(body, head.prefixed, shape, repeat)
    if not values:
        return head
    return replace(head, values=values, rule=f"{head.rule}+{habit}")


def is_prompt(text: str, custom: re.Pattern[str] | None = None) -> bool:
    """True when the whole line is a shell prompt."""
    return (custom or _PROMPT).fullmatch(text) is not None


def is_prompt_redraw(line: Line, custom: re.Pattern[str] | None = None) -> bool:
    """True when a partial line is a prompt followed by more text that is not a log line."""
    if not line.partial:
        return False
    m = (custom or _PROMPT).match(line.text)
    return (
        m is not None and m.end() < len(line.text) and log_after_prompt(line.text, custom) is None
    )


def log_after_prompt(text: str, custom: re.Pattern[str] | None = None) -> str | None:
    """The log line a prompt runs into with no line end between them, or None."""
    m = (custom or _PROMPT).match(text)
    if m is None or not 0 < m.end() < len(text):
        return None
    log = text[m.end() :]
    # The head alone: parse() would also move the trackers.
    return log if _parse_head(log, None)[0].prefixed else None
