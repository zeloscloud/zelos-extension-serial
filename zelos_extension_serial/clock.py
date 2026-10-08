"""Device time to host time."""

from collections import deque

# Two cores stamping their own lines print slightly out of order; a reboot drops further.
_RESTART_NS = 100_000_000


class DeviceClock:
    """Maps device time stamps onto the host clock, one per port."""

    def __init__(self, window_ns: int = 10_000_000_000) -> None:
        self._window_ns = window_ns
        # (host_ns, host_ns - device_ns) with offsets strictly increasing: the front is the minimum.
        self._samples: deque[tuple[int, int]] = deque()
        # The latest device time since the last restart, and whether the previous one fell below it.
        self._top: int | None = None
        self._dropped = False

    def map(self, host_ns: int, device_ns: int) -> tuple[int, bool]:
        """Return (mapped time, restarted).

        A time more than 100 ms before the latest is a restart once the next time is earlier than
        the latest too, so it is reported one line late. Until then the line takes host time:
        ESP-IDF's second core prints `I (0) cpu_start: App cpu up.` once in the middle of a boot.
        """
        top = self._top
        restarted = self._dropped and top is not None and device_ns < top
        if not restarted and top is not None and device_ns < top - _RESTART_NS:
            self._dropped = True
            return host_ns, False
        self._dropped = False
        if restarted or top is None:
            self._samples.clear()
            self._top = device_ns
        else:
            self._top = max(top, device_ns)
        offset = host_ns - device_ns
        while self._samples and self._samples[-1][1] >= offset:
            self._samples.pop()
        self._samples.append((host_ns, offset))
        while self._samples[0][0] < host_ns - self._window_ns:
            self._samples.popleft()
        return device_ns + self._samples[0][1], restarted

    @property
    def in_use(self) -> bool:
        """True after the first device time."""
        return self._top is not None
