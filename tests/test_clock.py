"""Device time to host time: minimum latency over a window, restarts."""

from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_serial.clock import DeviceClock

S = 1_000_000_000
MS = 1_000_000


def test_in_use_after_the_first_device_time() -> None:
    clock = DeviceClock()
    assert not clock.in_use
    assert clock.map(5 * S, 1 * S) == (5 * S, False)
    assert clock.in_use


def test_the_lowest_latency_in_the_window_sets_the_offset() -> None:
    clock = DeviceClock()
    clock.map(100 * S + 5 * MS, 1 * S)
    assert clock.map(100 * S + 1003 * MS, 2 * S) == (100 * S + 1003 * MS, False)
    assert clock.map(100 * S + 2009 * MS, 3 * S) == (100 * S + 2003 * MS, False)


def test_a_low_latency_sample_expires_after_the_window() -> None:
    clock = DeviceClock(window_ns=10 * S)
    clock.map(100 * S + 1 * MS, 0)
    clock.map(105 * S + 3 * MS, 5 * S)
    assert clock.map(109 * S + 9 * MS, 9 * S)[0] == 109 * S + 1 * MS
    assert clock.map(110 * S + 9 * MS, 10 * S)[0] == 110 * S + 3 * MS


def test_a_drop_is_a_restart_when_the_next_time_stays_below_the_old_maximum() -> None:
    clock = DeviceClock()
    clock.map(100 * S, 50 * S)
    # In doubt until the next line: host time.
    assert clock.map(101 * S, 3 * S) == (101 * S, False)
    assert clock.map(102 * S, 4 * S) == (102 * S, True)
    # The window starts again from the restart.
    assert clock.map(103 * S + 2 * MS, 5 * S) == (103 * S, False)


def test_a_crash_loop_under_a_second_is_a_restart_every_boot() -> None:
    clock = DeviceClock()
    restarts = []
    for boot in range(5):
        for ms in (31, 200, 480):
            restarts.append(clock.map(boot * 800 * MS + (ms + 2) * MS, ms * MS)[1])
    assert restarts == [False, False, False] + [False, True, False] * 4


def test_one_early_time_in_a_boot_is_not_a_restart() -> None:
    # ESP-IDF's second core prints `I (0) cpu_start: App cpu up.` between the first core's lines.
    clock = DeviceClock()
    clock.map(100 * S, 198 * MS)
    assert clock.map(100 * S + 5 * MS, 0) == (100 * S + 5 * MS, False)
    assert clock.map(100 * S + 18 * MS, 216 * MS) == (100 * S + 18 * MS, False)
    assert clock.map(100 * S + 40 * MS, 240 * MS) == (100 * S + 40 * MS, False)


def test_an_early_time_then_the_latest_again_is_not_a_restart() -> None:
    # ESP-IDF prints `I (229)`, then the second core's `I (0)`, then `I (229)` again.
    clock = DeviceClock()
    clock.map(100 * S, 229 * MS)
    assert clock.map(100 * S + 1 * MS, 0)[1] is False
    assert clock.map(100 * S + 2 * MS, 229 * MS)[1] is False


def test_a_drop_of_up_to_100_ms_is_not_a_restart() -> None:
    clock = DeviceClock()
    clock.map(100 * S, 500 * MS)
    assert clock.map(100 * S, 400 * MS)[1] is False
    assert clock.map(100 * S, 400 * MS)[1] is False


def test_a_kernel_log_replayed_by_dmesg_reads_as_one_restart() -> None:
    # Indistinguishable from a reboot on its first two lines; the times stay on the host clock.
    clock = DeviceClock()
    clock.map(100 * S, 120 * S)
    replay = [clock.map(101 * S + i * MS, device) for i, device in enumerate([0, S // 2, 119 * S])]
    assert [restarted for _, restarted in replay] == [False, True, False]
    assert all(mapped <= 101 * S + 2 * MS for mapped, _ in replay)
    assert clock.map(102 * S, 121 * S) == (102 * S, False)


ns = st.integers(0, 10_000 * S)


@given(latency=ns, start=ns, gaps=st.lists(st.integers(0, 20 * S), max_size=50))
def test_constant_latency_keeps_the_device_spacing_exactly(
    latency: int, start: int, gaps: list[int]
) -> None:
    clock = DeviceClock()
    device = start
    for gap in gaps:
        device += gap
        assert clock.map(device + latency, device) == (device + latency, False)


@given(
    samples=st.lists(st.tuples(st.integers(0, 30 * S), ns), max_size=50),
    drop=st.integers(100 * MS + 1, 10_000 * S),
    # At least 1: a time equal to the latest is the same boot.
    back=st.integers(1, 10_000 * S),
)
def test_a_drop_of_more_than_100_ms_then_an_earlier_time_is_always_reported(
    samples: list[tuple[int, int]], drop: int, back: int
) -> None:
    clock = DeviceClock()
    host = 0
    for step, device in samples:
        host += step
        clock.map(host, device)
    top = max((device for _, device in samples), default=0) + drop
    clock.map(host, top)
    assert clock.map(host, top - drop)[1] is False
    assert clock.map(host, max(0, top - back))[1] is True


@given(
    devices=st.lists(st.integers(0, 10_000 * S), min_size=1, max_size=50),
    early=st.integers(0, 10_000 * S),
    later=st.integers(1, 10 * S),
)
def test_one_early_time_followed_by_a_later_one_is_never_reported(
    devices: list[int], early: int, later: int
) -> None:
    clock = DeviceClock()
    for host, device in enumerate(sorted(devices)):
        assert clock.map(host * S, device)[1] is False
    top = max(devices)
    assert clock.map(len(devices) * S, min(early, top))[1] is False
    assert clock.map(len(devices) * S, top + later)[1] is False


@given(
    devices=st.lists(st.integers(0, 10_000 * S), min_size=1, max_size=50),
    decrease=st.integers(1, 100 * MS),
)
def test_a_decrease_of_up_to_100_ms_is_never_reported(devices: list[int], decrease: int) -> None:
    clock = DeviceClock()
    for host, device in enumerate(sorted(devices)):
        assert clock.map(host * S + device, device)[1] is False
    last = max(devices)
    for _ in range(2):
        assert clock.map(len(devices) * S + last, max(0, last - decrease))[1] is False


@given(st.lists(st.tuples(st.integers(0, 30 * S), ns), max_size=100))
def test_a_mapped_time_is_never_later_than_its_host_time(samples: list[tuple[int, int]]) -> None:
    clock = DeviceClock()
    host = 0
    for step, device in samples:
        host += step
        assert clock.map(host, device)[0] <= host
