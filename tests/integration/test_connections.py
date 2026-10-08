"""Ports that go away and come back, and a port handed to another program and taken back."""

import concurrent.futures
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.conftest import Recording, read, run, wait_until
from tests.integration.harness import App, Pty, Row, TcpServer, rows
from zelos_extension_serial.config import PortConfig
from zelos_extension_serial.transport import Fault, Transport, classify, open_transport


def logged(recording: Recording, port: str) -> list[Row]:
    return rows(recording.events()[f"{port}/log"])


def reconnected(where: str) -> list[Row]:
    """The log of a port that printed `before`, went away, came back and printed `after`."""
    return [
        Row("info", "serial", f"[serial] connected to {where}"),
        Row("info", "", "before"),
        Row("warn", "serial", f"[serial] disconnected from {where}"),
        Row("info", "serial", f"[serial] connected to {where}"),
        Row("info", "", "after"),
    ]


def disconnected(where: str) -> Callable[[dict[str, Any]], bool]:
    return lambda state: (
        state["health"] == f"Disconnected from {where}. Waiting for it to come back."
    )


def test_a_tcp_server_that_goes_away_is_reconnected(trace_file: Recording) -> None:
    with TcpServer() as server:
        where = f"127.0.0.1:{server.port}"
        tcp = {"connection": "tcp", "host": "127.0.0.1", "tcp_port": server.port}
        with App(trace_file, tcp) as app:
            [port] = app.ports
            server.accept().sendall(b"before\r\n")
            app.wait_counts(port, 1)

            server.restart()
            app.wait(port, disconnected(where))
            server.accept().sendall(b"after\r\n")
            app.wait_counts(port, 2)
            assert app.state(port)["counters"]["reconnects"] == 1

    assert port == f"127_0_0_1_{server.port}"
    assert logged(trace_file, port) == reconnected(where)


def test_a_pty_is_reconnected_by_path(trace_file: Recording, tmp_path: Path) -> None:
    link = tmp_path / "ttyDUT"
    with Pty() as first, Pty() as second:
        link.symlink_to(first.path)
        with App(trace_file, {"connection": "serial", "port": str(link), "name": "dut"}) as app:
            app.wait("dut", lambda state: state["state"] == "open")
            first.write(b"before\r\n")
            app.wait_counts("dut", 1)

            first.close()
            app.wait("dut", disconnected(str(link)))
            # Swapped in one step, as udev re-points a /dev/serial/by-id link.
            new = tmp_path / "ttyDUT.new"
            new.symlink_to(second.path)
            new.replace(link)
            app.wait("dut", lambda state: state["state"] == "open")
            second.write(b"after\r\n")
            app.wait_counts("dut", 2)

    assert logged(trace_file, "dut") == reconnected(str(link))


def open_when_free(cfg: PortConfig, refused: list[Fault]) -> Transport:
    """Open the port as another program would: retry until it is free."""
    deadline = time.monotonic() + 5
    while True:
        try:
            return open_transport(cfg)
        except OSError as e:
            refused.append(classify(e))
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def test_release_hands_the_port_to_a_waiting_program_until_acquire(
    trace_file: Recording,
) -> None:
    with Pty() as pty, App(trace_file, {"connection": "serial", "port": pty.path}) as app:
        [port] = app.ports
        app.wait(port, lambda state: state["state"] == "open")
        refused: list[Fault] = []
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            other = pool.submit(open_when_free, app.settings.ports[0], refused)
            wait_until(lambda: len(refused) >= 2)

            assert run("release", port=port) == {"ok": True}
            holder = other.result(timeout=5)
        line = b"for the other program\r\n"
        pty.write(line)
        assert read(holder, len(line)) == line
        holder.close()

        assert run("acquire", port=port) == {"ok": True}
        pty.write(b"back\r\n")
        app.wait_counts(port, 1)

    assert set(refused) == {"busy"}
    expected = [
        Row("info", "serial", f"[serial] connected to {pty.path}"),
        Row("info", "serial", f"[serial] released {pty.path}"),
        Row("info", "serial", f"[serial] acquired {pty.path}"),
        Row("info", "", "back"),
    ]
    assert logged(trace_file, port) == expected


def test_a_line_cut_by_a_disconnect_is_logged_without_values(trace_file: Recording) -> None:
    with TcpServer() as server:
        tcp = {"connection": "tcp", "host": "127.0.0.1", "tcp_port": server.port}
        with App(trace_file, {**tcp, "name": "dut"}) as app:
            server.accept().sendall(
                b"[00:00:00.000,000] <inf> dcdc: rail=13.80V in=4.30A\r\n"
                b"[00:00:00.050,000] <inf> dcdc: rail=13.79V in=4"
            )
            server.restart()
            # The reconnect comes 500 ms after the loss, well after an idle release at 100 ms.
            server.accept()
            app.wait("dut", lambda state: state["counters"]["reconnects"] == 1)

    events = trace_file.events()
    assert [(r["rail"], r["in"]) for r in events["dut/dcdc"]] == [(13.80, 4.30)]
    assert "dcdc: rail=13.79V in=4" in [r["message"] for r in events["dut/log"]]
