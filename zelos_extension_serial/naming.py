"""Trace-safe names."""

import unicodedata
from collections.abc import Iterable

import zelos_sdk

# The SDK's limit on a field or event name, in UTF-8 bytes.
MAX_BYTES = 128


def safe_name(text: str, max_bytes: int = MAX_BYTES) -> str:
    """A field-safe name of at most 64 characters and `max_bytes` bytes, never empty."""
    name = zelos_sdk.sanitize_name(text, kind="field")[:64]
    while name and len(name.encode()) > max_bytes:
        name = name[:-1]
    return name.rstrip(" _") or "unnamed"


def ascii_name(text: str) -> str | None:
    """`text` without accents or other non-ASCII; None when no letter or digit is left."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return folded if any(c.isalnum() for c in folded) else None


def unique_name(raw: str, used: set[str], max_bytes: int = MAX_BYTES) -> str:
    """The safe name for `raw`, with `_2`, `_3`, … until no name in `used` matches it ignoring
    case; adds it there.

    The trace store ignores case: two names that differ only in case leave the recording empty.
    """
    taken = {name.casefold() for name in used}
    name = base = safe_name(raw, max_bytes)
    n = 1
    while name.casefold() in taken:
        n += 1
        suffix = f"_{n}"
        # Cut the head, not the suffix, to fit.
        name = safe_name(base[: 64 - len(suffix)], max_bytes - len(suffix)) + suffix
    used.add(name)
    return name


class Namer:
    """Adds `_2`, `_3`, … when a different raw name maps to a safe name already used, ignoring
    case."""

    def __init__(self, reserved: Iterable[str] = (), max_bytes: int = MAX_BYTES) -> None:
        self._names: dict[str, str] = {}
        self._used = set(reserved)
        self._max_bytes = max_bytes

    def name(self, raw: str) -> str:
        """The safe name for `raw`, the same one on every call."""
        if raw not in self._names:
            self._names[raw] = unique_name(raw, self._used, self._max_bytes)
        return self._names[raw]
