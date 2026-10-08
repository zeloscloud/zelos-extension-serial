"""The measured throughput, as a table in the terminal and the job summary."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

_HEADER = (
    "| Test | Lines | Lines/s | Lost | Delay p50 ms | p99 ms | max ms | CPU % |\n"
    "|---|---|---|---|---|---|---|---|"
)
_rows: list[str] = []


def _report(
    test: str, lines: int, rate: float, lost: int, delays_ms: list[float], cpu: float
) -> None:
    ranked = sorted(delays_ms)
    p50, p99 = (ranked[min(len(ranked) - 1, int(q * len(ranked)))] for q in (0.5, 0.99))
    _rows.append(
        f"| {test} | {lines} | {rate:,.0f} | {lost} | {p50:.1f} | {p99:.1f} "
        f"| {ranked[-1]:.1f} | {cpu:.0f} |"
    )


@pytest.fixture
def report() -> Callable[..., None]:
    """Adds a row to the throughput table."""
    return _report


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _rows:
        return
    table = "\n".join([_HEADER, *_rows])
    terminalreporter.write_sep("-", "serial throughput")
    terminalreporter.write_line(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as file:
            file.write(f"### Serial throughput\n\n{table}\n")
