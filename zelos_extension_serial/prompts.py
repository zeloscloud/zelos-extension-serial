"""Shell prompts: a whole prompt, a redrawn prompt, and a prompt with a log line after it."""

import re

from zelos_log_lines.formats import read
from zelos_log_lines.lines import Line
from zelos_log_lines.values import is_teleplot

# Each repeated class excludes what follows it, so possessive repeats match the same strings
# without backtracking: a prompt check on a long line costs linear time.
_PROMPT = re.compile(
    r"uart:~\$ ?|(?:[^\WA-Za-z]|[.@~/-])*+[A-Za-z][\w.@:~/-]*+(?: ?[#$]|>) ?|\[[\w.-]++\]> ?"
    r"|[#$] |=> ?|>>> ?|\.\.\. |[\w.-]++ login: ?|[Pp]assword: ?"
)


def is_prompt(text: str, custom: re.Pattern[str] | None = None) -> bool:
    """True when the whole line is a shell prompt."""
    return (custom or _PROMPT).fullmatch(text) is not None


def is_prompt_redraw(line: Line, custom: re.Pattern[str] | None = None) -> bool:
    """True when an erased line is a prompt followed by more text that is not a log line.

    Only an erase: a line released by a pause or a close, such as `Test #3: erasing flash`, can
    start like a prompt and is still the device's output.
    """
    if not line.erased:
        return False
    m = (custom or _PROMPT).match(line.text)
    return (
        m is not None and m.end() < len(line.text) and log_after_prompt(line.text, custom) is None
    )


def log_after_prompt(text: str, custom: re.Pattern[str] | None = None) -> str | None:
    """The log line or Teleplot point a prompt runs into with no line end between, or None."""
    m = (custom or _PROMPT).match(text)
    if m is None or not 0 < m.end() < len(text):
        return None
    log = text[m.end() :]
    return log if read(log).prefixed or is_teleplot(log) else None
