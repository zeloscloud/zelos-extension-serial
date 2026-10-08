"""Real consoles recorded end to end, over TCP and over a pseudo-terminal."""

from itertools import pairwise

from tests.conftest import TIME, Recording
from tests.integration.consoles import ESP_IDF, LINUX, STATUS_LINES, ZEPHYR, status
from tests.integration.harness import event_paths, units


def test_zephyr_shell(recorded, trace_file: Recording) -> None:
    events = recorded(ZEPHYR)

    assert event_paths(trace_file) == {"Serial/log", "Serial/dut/log", "Serial/dut/dcdc"}
    assert units(trace_file, "dut/dcdc") == {"rail": "V", "in": "A", "limit": "A", "temp": "C"}
    rows = events["dut/dcdc"]
    assert [
        f"rail={r['rail']:.2f}V in={r['in']:.2f}A limit={r['limit']:.1f}A temp={r['temp']:.1f}C"
        for r in rows
    ] == [status(i) for i in range(STATUS_LINES)]
    # A line maps to its device time plus the lowest host-minus-device offset seen so far, so the
    # offset holds or drops; a late first line drops it once. Host time would raise it 25 ms a line.
    offsets = [row[TIME] - 0.05 * i for i, row in enumerate(rows)]
    assert all(later <= earlier + 1e-6 for earlier, later in pairwise(offsets))


def test_esp_idf_boot(recorded, trace_file: Recording) -> None:
    events = recorded(ESP_IDF)

    assert event_paths(trace_file) == {"Serial/log", "Serial/dut/log", "Serial/dut/app"}
    assert units(trace_file, "dut/app") == {"temp": "C", "vbat": "V"}
    assert [(r["temp"], r["vbat"]) for r in events["dut/app"]] == [(24.5, 3.71), (24.6, 3.70)]


def test_linux_boot(recorded, trace_file: Recording) -> None:
    events = recorded(LINUX)

    assert event_paths(trace_file) == {"Serial/log", "Serial/dut/log", "Serial/dut/values"}
    assert units(trace_file, "dut/values") == {"cpu_temp": "C", "load": None}
    assert [(r["cpu_temp"], r["load"]) for r in events["dut/values"]] == [
        (48.2, 0.31),
        (48.4, 0.28),
    ]
