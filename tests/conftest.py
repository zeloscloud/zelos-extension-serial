"""Shared by every test: an isolated trace recording, a fake transport, a faked device listing,
a port config, and the actions run as the agent runs them."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import zelos_sdk

from zelos_extension_serial import ACTION_PREFIX

# Not reader.time_range(): it raises on a trace that holds no rows.
ALL_TIME = ("1970-01-01T00:00:00Z", "2200-01-01T00:00:00Z")


@dataclass
class Recording:
    """A trace source in its own namespace, written to `path`."""

    source: zelos_sdk.TraceSource
    path: Path
    writer: zelos_sdk.TraceWriter

    def events(self) -> dict[str, list[dict[str, Any]]]:
        """Close the recording and read it back with read_events()."""
        self.source.flush()
        # A .trz reads back empty until its writer has closed it.
        self.writer.close()
        return read_events(self.path)


# No field can be named this: sanitising turns `@` into `_`, so a printed `time_s` stays a field.
TIME = "@time_s"


def read_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Event name (`dut/log`) to its rows, each `{TIME: float, field: value, ...}`."""
    events: dict[str, list[dict[str, Any]]] = {}
    with zelos_sdk.TraceReader(str(path)) as reader:
        segments = [segment.id for segment in reader.list_data_segments()]
        for source in reader.list_fields():
            for event in source.events:
                fields = [field.path for field in event.fields]
                data = reader.query(segments, fields, *ALL_TIME).to_arrow()
                # Fields are named `<source>/<event>.<field>`, the time `time_s`; an event with no
                # rows returns b"".
                prefix = f"{source.name}/{event.name}."
                rows = pa.ipc.open_stream(data).read_all().to_pylist() if data else []
                events[event.name] = [
                    {
                        key.removeprefix(prefix) if key.startswith(prefix) else TIME: value
                        for key, value in row.items()
                    }
                    for row in rows
                ]
    return events


@pytest.fixture
def trace_file(tmp_path: Path) -> Iterator[Recording]:
    """A recording that never touches the global source or an agent."""
    namespace = zelos_sdk.TraceNamespace(f"test-{uuid.uuid4().hex}")
    source = zelos_sdk.TraceSource(ACTION_PREFIX, namespace=namespace)
    path = tmp_path / "trace.trz"
    writer = zelos_sdk.TraceWriter(str(path), namespace=namespace)
    writer.open()
    yield Recording(source, path, writer)
    writer.close()
