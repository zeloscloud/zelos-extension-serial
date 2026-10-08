"""A console played to the real app over TCP or a pseudo-terminal, and read back."""

from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import Recording
from tests.integration.harness import App, Console, Pty, TcpServer, play, rows


@pytest.fixture(params=["tcp", "pty"])
def recorded(
    request: pytest.FixtureRequest, trace_file: Recording
) -> Callable[[Console], dict[str, list[dict[str, Any]]]]:
    """Play a console to a port named `dut`, check its log, and return every recorded event."""

    def record(console: Console) -> dict[str, list[dict[str, Any]]]:
        if request.param == "tcp":
            with TcpServer() as server:
                where = f"127.0.0.1:{server.port}"
                tcp = {"connection": "tcp", "host": "127.0.0.1", "tcp_port": server.port}
                with App(trace_file, {**tcp, "name": "dut"}) as app:
                    # As Renode sends a console: one byte per send().
                    play(console, server.accept().sendall, one_byte=True)
                    app.wait_counts("dut", len(console.rows), console.prompts)
        else:
            with Pty() as pty:
                where = pty.path
                with App(trace_file, {"connection": "serial", "port": where, "name": "dut"}) as app:
                    app.wait("dut", lambda state: state["state"] == "open")
                    play(console, pty.write)
                    app.wait_counts("dut", len(console.rows), console.prompts)

        events = trace_file.events()
        log = events["dut/log"]
        assert [row["message"] for row in log if row["name"] == "serial"] == [
            f"[serial] connected to {where}"
        ]
        lines = [row for row in log if row["name"] != "serial"]
        assert rows(lines) == console.rows
        return events

    return record
