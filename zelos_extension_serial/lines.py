"""Bytes to lines."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

_TEXT, _ESC, _CSI, _OSC = range(4)
_CONTROL = re.compile(rb"[\r\n\x1b]")
# Foreground and background.
_RED = {31, 91, 41, 101}
_YELLOW = {33, 93, 43, 103}
# An extended colour (38 foreground, 48 background, 58 underline) is followed by `5;n` or
# `2;r;g;b`; these count the parameters after the 5 or the 2.
_EXTENDED = {38, 48, 58}
_EXTENDED_ARGS = {5: 1, 2: 3}
# Real sequences are a few bytes; the cap stops line noise that opens one from eating a log.
_MAX_PARAMS = 64


@dataclass(frozen=True, slots=True)
class Line:
    """One released line: decoded, escapes removed, no terminator."""

    text: str
    host_ns: int
    colour: Literal["red", "yellow"] | None
    # No line end released it: an erase, `idle_ns` without bytes, a cut or the port closing.
    partial: bool
    # A piece of a line longer than `max_bytes`; the last piece too.
    cut: bool


class LineSplitter:
    """Splits a byte stream into lines, invariant to how it is chunked."""

    long_lines: int
    # When the pending text began, in `now` time; None while nothing is pending.
    pending_since: int | None

    def __init__(
        self,
        max_bytes: int = 4096,
        idle_ns: int = 100_000_000,
        now: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.long_lines = 0
        self._max_bytes = max_bytes
        self._idle_ns = idle_ns
        self._now = now
        self.reset()

    def feed(self, data: bytes, host_ns: int) -> list[Line]:
        """Append bytes; return every line they complete, stamped `host_ns`."""
        if not data:
            return []
        self._last_ns = self._now()
        self._last_host_ns = host_ns
        data = data.replace(b"\0", b"")
        out: list[Line] = []
        i, n = 0, len(data)
        while i < n:
            if self._state == _TEXT:
                match = _CONTROL.search(data, i)
                end = match.start() if match else n
                if end > i:
                    self._append(data[i:end], out, host_ns)
                if match is None:
                    break
                i = end + 1
                if data[end] == 0x1B:
                    self._state = _ESC
                else:
                    # A `\r` ends the line at once rather than waiting to see whether `\n`
                    # follows (Renode delivers one byte per read); the empty line that a
                    # following `\n` closes is dropped.
                    self._release(out, host_ns)
            elif self._escape(data[i], out, host_ns):
                i += 1
        return out

    def idle(self) -> list[Line]:
        """Release a pending partial after `idle_ns` with no bytes."""
        if not self._line or self._now() - self._last_ns < self._idle_ns:
            return []
        return self.flush()

    def flush(self) -> list[Line]:
        """Release a pending partial now."""
        out: list[Line] = []
        self._release(out, self._last_host_ns, partial=True)
        return out

    def reset(self) -> None:
        """Drop all state; called whenever the port closes."""
        self._line = bytearray()
        self.pending_since = None
        self._params = bytearray()
        self._state = _TEXT
        self._colour: Literal["red", "yellow"] | None = None
        self._cut = False
        # The last sequence moved the cursor left: an erase now clears a redrawn prompt.
        self._left = False
        self._last_ns = 0
        self._last_host_ns = 0

    def _append(self, text: bytes, out: list[Line], host_ns: int) -> None:
        if not self._line:
            self.pending_since = self._last_ns
        self._line += text
        self._left = False
        while len(self._line) > self._max_bytes:
            # Not inside a UTF-8 character: back over at most 3 continuation bytes.
            end = self._max_bytes
            while end > max(1, self._max_bytes - 3) and self._line[end] & 0xC0 == 0x80:
                end -= 1
            piece = self._line[:end].decode("utf-8", "replace")
            del self._line[:end]
            out.append(Line(piece, host_ns, self._colour, True, True))
            self._cut = True
            self.long_lines += 1

    def _release(self, out: list[Line], host_ns: int, *, partial: bool = False) -> None:
        if self._line:
            text = self._line.decode("utf-8", "replace")
            out.append(Line(text, host_ns, self._colour, partial, self._cut))
            self._line = bytearray()
        self.pending_since = None
        self._colour = None
        self._cut = self._left = False

    def _escape(self, byte: int, out: list[Line], host_ns: int) -> bool:
        """Advance an escape sequence by one byte; False leaves the byte to be read as text."""
        state = self._state
        if state == _ESC:
            if byte == 0x5B:
                self._state = _CSI
                self._params.clear()
            elif byte == 0x5D:
                self._state = _OSC
            elif 0x30 <= byte <= 0x7E:
                self._state = _TEXT
            elif not 0x20 <= byte <= 0x2F:
                # Any other byte abandons the escape, so a stray ESC cannot eat a line end.
                self._state = _TEXT
                return False
        elif state == _CSI:
            if 0x20 <= byte <= 0x3F and len(self._params) < _MAX_PARAMS:
                self._params.append(byte)
            else:
                self._state = _TEXT
                if not 0x40 <= byte <= 0x7E:
                    return False
                self._final(byte, out, host_ns)
        elif state == _OSC:
            if byte == 0x07:
                self._state = _TEXT
            elif byte == 0x1B:
                # Ends the OSC; its `ESC \` terminator then reads as a two-byte escape.
                self._state = _ESC
            elif byte in (0x0A, 0x0D):
                # Line noise can open an OSC that never closes; a line end abandons it.
                self._state = _TEXT
                return False
        return True

    def _final(self, byte: int, out: list[Line], host_ns: int) -> None:
        # A cursor up keeps a cursor left: Zephyr erases a wrapped command line with
        # `ESC[nD ESC[nA ESC[J`.
        left, self._left = self._left, byte == 0x44 or (byte == 0x41 and self._left)
        # A shell redraws its prompt with a cursor left and then an erase (Zephyr: `ESC[8D ESC[J`).
        # An erase alone, as grep --color and gcc print after each colour, is not a line break.
        if byte in (0x4A, 0x4B) and left:
            self._release(out, host_ns, partial=True)
        elif byte == 0x6D:
            self._sgr()

    def _sgr(self) -> None:
        codes = [int(c) if c.isdigit() else -1 for c in bytes(self._params).split(b";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code in _EXTENDED:
                kind = codes[i + 1] if i + 1 < len(codes) else -1
                i += 2 + _EXTENDED_ARGS.get(kind, -1)
                continue
            if code in _RED:
                self._colour = "red"
            elif code in _YELLOW and self._colour is None:
                self._colour = "yellow"
            i += 1
