"""The app on its own thread, and the consoles it records: a local TCP server or a pty pair."""

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

import pytest
import zelos_sdk

from tests.conftest import Recording, run, wait_until
from zelos_extension_serial import actions, app, config


class Row(NamedTuple):
    """One device line as the log records it."""

    level: str
    name: str
    message: str


@dataclass(frozen=True)
class Printed:
    """Bytes a device prints `at_s` after the first, and the log row they become, if any."""

    at_s: float
    data: bytes
    row: Row | None = None


@dataclass(frozen=True)
class Console:
    """What a device prints, and how many prompts are among it."""

    printed: list[Printed]
    prompts: int

    @property
    def rows(self) -> list[Row]:
        return [p.row for p in self.printed if p.row is not None]


def play(console: Console, write: Callable[[bytes], object], one_byte: bool = False) -> None:
    """Write each print at its time, one byte per write when `one_byte`."""
    start = time.monotonic()
    for printed in console.printed:
        time.sleep(max(0.0, start + printed.at_s - time.monotonic()))
        if not one_byte:
            write(printed.data)
            continue
        for i in range(len(printed.data)):
            write(printed.data[i : i + 1])


class App:
    """`app.run()` on its own thread, recording `ports` into `recording`."""

    def __init__(self, recording: Recording, *ports: dict[str, Any]) -> None:
        path = recording.path.with_name("config.json")
        path.write_text(json.dumps({"ports": list(ports)}))
        self.settings = config.load(path)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=app.run,
            args=(recording.source,),
            kwargs={"settings": self.settings, "stop": self._stop},
        )

    def __enter__(self) -> "App":
        # run() clears it too, but only once its thread starts.
        actions.workers.clear()
        self._thread.start()
        wait_until(lambda: len(actions.workers) == len(self.settings.ports))
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(5)
        actions.workers.clear()
        assert not self._thread.is_alive()

    @property
    def ports(self) -> list[str]:
        return sorted(actions.workers)

    def state(self, port: str) -> dict[str, Any]:
        return run("get_state", port=port)

    def wait(self, port: str, condition: Callable[[dict[str, Any]], object]) -> None:
        """Wait until `condition` holds for the port's state."""
        wait_until(lambda: condition(self.state(port)))

    def wait_counts(self, port: str, lines: int, prompts: int = 0) -> None:
        """Wait until the port has logged `lines` lines and counted `prompts` prompts."""
        self.wait(
            port, lambda s: (s["counters"]["lines"], s["counters"]["prompts"]) == (lines, prompts)
        )


class TcpServer:
    """A device on a local TCP port; it can go away and come back on the same port."""

    def __init__(self) -> None:
        self._listener = self._listen(0)
        self.port: int = self._listener.getsockname()[1]
        self._peer: socket.socket | None = None

    @staticmethod
    def _listen(port: int) -> socket.socket:
        listener = socket.create_server(("127.0.0.1", port))
        # accept() then fails instead of hanging when the app never connects.
        listener.settimeout(5)
        return listener

    def accept(self) -> socket.socket:
        """Wait for the app to connect."""
        self._peer, _ = self._listener.accept()
        self._peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self._peer

    def close(self) -> None:
        if self._peer is not None:
            self._peer.close()
        self._listener.close()

    def restart(self) -> None:
        """Close the connection and the listener, then listen again on the same port."""
        self.close()
        self._listener = self._listen(self.port)

    def __enter__(self) -> "TcpServer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class Pty:
    """A pseudo-terminal pair: the app opens `path`, the test writes the device's side."""

    def __init__(self) -> None:
        if sys.platform == "win32":
            pytest.skip("pseudo-terminals are POSIX only")
        self.controller, device = os.openpty()
        self.path = os.ttyname(device)
        self._fds = [self.controller, device]

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.controller, view) :]

    def close(self) -> None:
        """Hang up; the device node goes away once the app closes its end too."""
        while self._fds:
            os.close(self._fds.pop())

    def __enter__(self) -> "Pty":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def rows(log: list[dict[str, Any]]) -> list[Row]:
    """A log's rows in the order they were read back."""
    return [Row(r["level"], r["name"], r["message"]) for r in log]


def event_paths(recording: Recording) -> set[str]:
    """Every `source/event` in the recorded file."""
    with zelos_sdk.TraceReader(str(recording.path)) as reader:
        return {
            f"{source.name}/{event.name}"
            for source in reader.list_fields()
            for event in source.events
        }


def units(recording: Recording, event: str) -> dict[str, str | None]:
    """Each field of `event` and its unit, as registered."""
    return {field.name: field.unit for field in recording.source.get_event(event).schema}
