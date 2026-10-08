"""The demo port end to end, driven through the actions."""

import re
from itertools import groupby

from tests.conftest import TIME, Recording, run
from tests.integration.harness import App

BANNER = "*** Booting Zephyr OS build dccb09599635 ***"
# 60 status lines are 1.2 s of device time, well past the 100 ms drop that a restart must show.
RESTARTABLE = 60


def advance(app: App, lines: int) -> None:
    """Wait until the demo port has logged `lines` more lines."""
    start = app.state("demo")["counters"]["lines"]
    app.wait("demo", lambda state: state["counters"]["lines"] >= start + lines)


def test_demo_port_through_the_actions(trace_file: Recording) -> None:
    with App(trace_file, {"connection": "demo"}) as app:
        advance(app, RESTARTABLE)

        answer = run("command", port="demo", text="dcdc limit set 7.5")
        assert (answer["reply"], answer["ended_by"]) == (["limit set to 7.5 A"], "prompt")
        advance(app, 2)
        # A module's signals are registered 2 s after its first value.
        app.wait("demo", lambda state: state["counters"]["signals"] == 4)

        state = app.state("demo")
        assert re.fullmatch(
            r"Connected\. \d+ lines, 4 signals, device clock in use\.", state["health"]
        )
        assert [(s["event"], s["field"], s["unit"], s["rule"]) for s in state["signals"]] == [
            ("demo/dcdc", "rail", "V", "zephyr+kv"),
            ("demo/dcdc", "in", "A", "zephyr+kv"),
            ("demo/dcdc", "limit", "A", "zephyr+kv"),
            ("demo/dcdc", "temp", "C", "zephyr+kv"),
        ]
        sampled = run("sample", port="demo", lines=1000)["lines"]
        assert {"uart:~$ ", "limit set to 7.5 A"} <= set(sampled)

        assert run("reset", port="demo") == {"ok": True}
        advance(app, RESTARTABLE)

        assert run("release", port="demo") == {"ok": True}
        assert app.state("demo")["health"] == "Released. Run Acquire Port to take the port back."
        assert run("acquire", port="demo") == {"ok": True}
        # The banner, then two status lines: the second shows the restart.
        advance(app, 3)

    events = trace_file.events()
    log = [(r["level"], r["name"], r["message"]) for r in events["demo/log"]]
    assert [row for row in log if row[1] in ("serial", "tx")] == [
        ("info", "serial", "[serial] connected to demo"),
        ("info", "tx", "> dcdc limit set 7.5"),
        ("info", "serial", "[serial] reset pulse on dtr"),
        ("warn", "serial", "[serial] device restarted"),
        ("info", "serial", "[serial] released demo"),
        ("info", "serial", "[serial] acquired demo"),
        # Acquire opens a new demo board, which boots.
        ("warn", "serial", "[serial] device restarted"),
    ]
    assert log.count(("info", "", BANNER)) == 3
    assert ("info", "", "limit set to 7.5 A") in log

    # The limit set by the command holds until the board restarts with the default.
    def since(message: str) -> float:
        return next(r[TIME] for r in events["demo/log"] if r["message"] == message)

    dcdc = events["demo/dcdc"]
    before = [r["limit"] for r in dcdc if r[TIME] < since("[serial] reset pulse on dtr")]
    after = [r["limit"] for r in dcdc if r[TIME] >= since("[serial] device restarted")]
    assert [limit for limit, _ in groupby(before)] == [4.5, 7.5]
    assert after[0] == 4.5
