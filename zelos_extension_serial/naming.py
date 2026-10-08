"""Trace-safe names."""

import unicodedata
from collections.abc import Iterable

import zelos_sdk


def safe_name(text: str) -> str:
    """A field-safe name of at most 64 characters, never empty."""
    return zelos_sdk.sanitize_name(text, kind="field")[:64].rstrip(" _") or "unnamed"


def ascii_name(text: str) -> str | None:
    """`text` without accents or other non-ASCII; None when no letter or digit is left."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return folded if any(c.isalnum() for c in folded) else None


def unique_name(raw: str, used: set[str]) -> str:
    """The safe name for `raw`, with `_2`, `_3`, … until no name in `used` matches it ignoring
    case; adds it there.

    The trace store ignores case: two names that differ only in case leave the recording empty.
    """
    taken = {name.casefold() for name in used}
    name = base = safe_name(raw)
    n = 1
    while name.casefold() in taken:
        n += 1
        suffix = f"_{n}"
        head = base[: 64 - len(suffix)]
        # The SDK cuts names at 128 bytes, which would cut the suffix off.
        while len(head.encode()) > 128 - len(suffix):
            head = head[:-1]
        name = safe_name(head) + suffix
    used.add(name)
    return name


class Namer:
    """Adds `_2`, `_3`, … when a different raw name maps to a safe name already used, ignoring
    case."""

    def __init__(self, reserved: Iterable[str] = ()) -> None:
        self._names: dict[str, str] = {}
        self._used = set(reserved)

    def name(self, raw: str) -> str:
        """The safe name for `raw`, the same one on every call."""
        if raw not in self._names:
            self._names[raw] = unique_name(raw, self._used)
        return self._names[raw]
