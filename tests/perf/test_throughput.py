"""Lines a second the app records with none lost: a paced pty, and TCP one byte per send()."""

import re
import subprocess
import sys
import time
from array import array
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import TIME, Recording, wait_until
from tests.integration.harness import App, Pty

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="measured on Linux"),
]

WRITER = Path(__file__).with_name("writer.py")
SECONDS = 10
SEQ = re.compile(r"seq=(\d+) ")


class Cpu:
    """This process's CPU time over the `with` block, in percent of one core."""

    percent = 0.0

    def __enter__(self) -> "Cpu":
        self._wall, self._cpu = time.monotonic(), time.process_time()
        return self

    def __exit__(self, *_: object) -> None:
        wall = time.monotonic() - self._wall
        self.percent = 100 * (time.process_time() - self._cpu) / wall


def sent_times(out: Path) -> array:
    """The writer's send time of each line, by sequence number."""
    return array("q", out.read_bytes())


def drain(app: App, lines: int) -> None:
    """Wait until the port has logged `lines` lines, or has logged none for 2 s."""
    seen, since = -1, time.monotonic()
    while (logged := app.state("dut")["counters"]["lines"]) < lines:
        if logged != seen:
            seen, since = logged, time.monotonic()
        elif time.monotonic() - since > 2:
            return
        time.sleep(0.1)


def check(
    test: str, recording: Recording, sent: array, cpu: Cpu, report: Callable[..., None]
) -> None:
    recorded = {
        int(m[1]): row[TIME]
        for row in recording.events()["dut/log"]
        if (m := SEQ.match(row["message"]))
    }
    lost = len(set(range(len(sent))) - recorded.keys())
    delays_ms = [(t * 1e9 - sent[seq]) / 1e6 for seq, t in recorded.items()]
    report(test, len(sent), len(sent) / SECONDS, lost, delays_ms, cpu.percent)
    assert lost == 0
    assert len(recorded) == len(sent)


def test_a_pty_at_5000_lines_a_second(
    trace_file: Recording, tmp_path: Path, report: Callable[..., None]
) -> None:
    out = tmp_path / "sent"
    with (
        Pty() as pty,
        App(trace_file, {"connection": "serial", "port": pty.path, "name": "dut"}) as app,
    ):
        app.wait("dut", lambda state: state["state"] == "open")
        args = ["pty", str(pty.controller), "5000", "100", str(SECONDS), str(out)]
        with Cpu() as cpu:
            subprocess.run(
                [sys.executable, WRITER, *args],
                pass_fds=(pty.controller,),
                check=True,
                timeout=SECONDS + 10,
            )
            sent = sent_times(out)
            # One more: the banner.
            drain(app, len(sent) + 1)
    check("pty, 100-byte lines at 5,000/s", trace_file, sent, cpu, report)


def test_tcp_one_byte_per_send_as_fast_as_it_goes(
    trace_file: Recording, tmp_path: Path, report: Callable[..., None]
) -> None:
    out = tmp_path / "sent"
    args = ["tcp", "20", str(SECONDS), str(out)]
    with subprocess.Popen(
        [sys.executable, WRITER, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as writer:
        assert writer.stdout is not None
        tcp = {"connection": "tcp", "host": "127.0.0.1", "tcp_port": int(writer.stdout.readline())}
        with App(trace_file, {**tcp, "name": "dut"}) as app, Cpu() as cpu:
            wait_until(out.exists, timeout=SECONDS + 10)
            sent = sent_times(out)
            drain(app, len(sent) + 1)
    check("tcp, 20-byte lines one byte per send()", trace_file, sent, cpu, report)
