"""Bytes to lines: terminators, escapes, decoding, cuts and idle release."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zelos_extension_serial.lines import Line, LineSplitter

# A Zephyr shell in Renode: each log line first erases the prompt the shell printed.
ZEPHYR = [
    b"\x1b[m",
    b"\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[J*** Booting Zephyr OS build dccb09599635 ***\r\n",
    b"\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[J[00:00:00.000,000] \x1b[0m<inf> dcdc: "
    b"rail=13.64V in=4.50A limit=4.5A temp=35.0C\x1b[0m\r\n",
    b"\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[J\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[J"
    b"[00:00:00.020,000] \x1b[0m<inf> dcdc: ...\x1b[0m\r\n",
    b"\x1b[1;32muart:~$ \x1b[mdcdc limit set 2.0\r\nlimit set to 2.0 A\r\n",
]
PROMPT = ("uart:~$ ", None, True)


def lines(*chunks: bytes, max_bytes: int = 4096) -> list[Line]:
    splitter = LineSplitter(max_bytes=max_bytes)
    return [line for chunk in chunks for line in splitter.feed(chunk, 0)]


def split(*chunks: bytes, max_bytes: int = 4096) -> list[tuple[str, str | None, bool]]:
    return [(line.text, line.colour, line.partial) for line in lines(*chunks, max_bytes=max_bytes)]


def one_byte_at_a_time(data: bytes) -> list[bytes]:
    return [data[i : i + 1] for i in range(len(data))]


@pytest.mark.parametrize("chunks", [ZEPHYR, one_byte_at_a_time(b"".join(ZEPHYR))])
def test_zephyr_log_lines_come_out_clean_of_the_prompt_they_erase(chunks: list[bytes]) -> None:
    assert split(*chunks) == [
        PROMPT,
        ("*** Booting Zephyr OS build dccb09599635 ***", None, False),
        PROMPT,
        (
            "[00:00:00.000,000] <inf> dcdc: rail=13.64V in=4.50A limit=4.5A temp=35.0C",
            None,
            False,
        ),
        PROMPT,
        PROMPT,
        ("[00:00:00.020,000] <inf> dcdc: ...", None, False),
        ("uart:~$ dcdc limit set 2.0", None, False),
        ("limit set to 2.0 A", None, False),
    ]


@pytest.mark.parametrize("stream", [b"a\nb\n", b"a\r\nb\r\n", b"a\rb\r"])
def test_lf_crlf_and_bare_cr_each_end_a_line(stream: bytes) -> None:
    assert split(stream) == [("a", None, False), ("b", None, False)]


def test_cr_releases_at_once_and_the_lf_in_the_next_chunk_adds_no_line() -> None:
    splitter = LineSplitter()
    assert [(line.text, line.host_ns) for line in splitter.feed(b"a\r", 1)] == [("a", 1)]
    assert [(line.text, line.host_ns) for line in splitter.feed(b"\nb\n", 2)] == [("b", 2)]


def test_empty_lines_are_dropped_and_trailing_spaces_kept() -> None:
    assert split(b"\n\r\n\r\r x  \n\n") == [(" x  ", None, False)]


@pytest.mark.parametrize("erase", [b"\x1b[J", b"\x1b[0J", b"\x1b[K", b"\x1b[2K"])
def test_erase_after_a_cursor_left_releases_pending_text_as_a_partial_line(erase: bytes) -> None:
    assert split(b"> \x1b[2D" + erase + b"log\n") == [("> ", None, True), ("log", None, False)]


def test_a_zephyr_erase_of_a_wrapped_command_line_releases_it() -> None:
    # z_shell_cmd_line_erase: cursor to the first column, up to the first row, erase to the end.
    erase = b"\x1b[11D\x1b[1A\x1b[J"
    assert split(
        b"\x1b[1;32muart:~$ \x1b[mdcdc limit", erase, b"[00:00:01.000,000] <inf> x\r\n"
    ) == [
        ("uart:~$ dcdc limit", None, True),
        ("[00:00:01.000,000] <inf> x", None, False),
    ]


def test_a_cursor_up_alone_before_an_erase_keeps_the_line_whole() -> None:
    assert split(b"a\x1b[1A\x1b[Jb\r\n") == [("ab", None, False)]


@pytest.mark.parametrize(
    ("stream", "line"),
    [
        # GNU grep --color and gcc diagnostics clear to the end of the line after each colour.
        (
            b"kernel: \x1b[01;31m\x1b[Kerror\x1b[m\x1b[K: disk full\r\n",
            ("kernel: error: disk full", "red", False),
        ),
        (b"a\x1b[2D\x1b[mb\x1b[Kc\r\n", ("abc", None, False)),
    ],
)
def test_an_erase_not_right_after_a_cursor_left_keeps_the_line_whole(
    stream: bytes, line: tuple[str, str | None, bool]
) -> None:
    assert split(stream) == [line]


def test_erase_with_nothing_pending_releases_nothing() -> None:
    assert split(b"50%\r\x1b[2K60%\r\x1b[2Kdone\n") == [
        ("50%", None, False),
        ("60%", None, False),
        ("done", None, False),
    ]


@pytest.mark.parametrize("osc", [b"\x1b]0;title\x07", b"\x1b]0;title\x1b\\"])
def test_osc_is_removed(osc: bytes) -> None:
    assert split(b"a" + osc + b"b\n") == [("ab", None, False)]


def test_an_unterminated_osc_ends_at_the_line_end() -> None:
    assert split(b"a\x1b]noise\nb\n") == [("a", None, False), ("b", None, False)]


def test_short_escapes_are_removed() -> None:
    assert split(b"a\x1b7b\x1b8c\x1bMd\x1b(Be\x1b=f\n") == [("abcdef", None, False)]


def test_cursor_moves_are_removed() -> None:
    assert split(b"a\x1b[8Db\x1b[?25lc\n") == [("abc", None, False)]


@pytest.mark.parametrize(
    "stream", [b"a\x1b\nb\n", b"a\x1b[1\nb\n", b"a\x1b(\nb\n", b"a\x1b[1\rb\r"]
)
def test_a_line_end_inside_an_escape_still_ends_the_line(stream: bytes) -> None:
    assert split(stream) == [("a", None, False), ("b", None, False)]


def test_noise_that_opens_a_sequence_swallows_at_most_64_bytes() -> None:
    assert split(b"\x1b[" + b"9" * 100 + b"\n") == [("9" * 36, None, False)]


def test_nul_bytes_are_removed_even_inside_escapes() -> None:
    assert split(b"a\0b\x1b[\x003\x003m\0\n") == [("ab", "yellow", False)]


def test_invalid_utf8_becomes_replacement_characters() -> None:
    assert split(b"a\xffb\n") == [("a�b", None, False)]


def test_a_character_split_across_chunks_decodes_whole() -> None:
    assert split(*one_byte_at_a_time("é ✓\n".encode())) == [("é ✓", None, False)]


def test_long_lines_are_cut_at_max_bytes_and_counted() -> None:
    splitter = LineSplitter(max_bytes=4)
    released = splitter.feed(b"abcdefghij\nabcd\n", 0)
    # Every piece is cut; only the last one ends at a line end.
    assert [(line.text, line.partial, line.cut) for line in released] == [
        ("abcd", True, True),
        ("efgh", True, True),
        ("ij", False, True),
        ("abcd", False, False),
    ]
    assert splitter.long_lines == 2


def test_the_rest_of_a_cut_line_released_by_idle_is_still_cut() -> None:
    clock = FakeClock()
    splitter = LineSplitter(max_bytes=4, idle_ns=100, now=clock)
    splitter.feed(b"abcdef", 0)
    clock.ns = 100
    assert [(line.text, line.partial, line.cut) for line in splitter.idle()] == [("ef", True, True)]
    assert [line.cut for line in splitter.feed(b"next\n", 0)] == [False]


def test_a_cut_never_splits_a_utf8_character() -> None:
    assert [line.text for line in lines("abcé✓x\n".encode(), max_bytes=4)] == ["abc", "é", "✓x"]


@pytest.mark.parametrize(
    ("sgr", "colour"),
    [
        (b"31", "red"),
        (b"1;91", "red"),
        (b"01;31", "red"),
        (b"41", "red"),
        (b"1;33", "yellow"),
        (b"93", "yellow"),
        (b"1;32", None),
    ],
)
def test_sgr_colour_anywhere_in_the_line_sets_its_colour(sgr: bytes, colour: str | None) -> None:
    assert split(b"[00:01] \x1b[" + sgr + b"m<wrn> x\x1b[0m\r\nnext\r\n") == [
        ("[00:01] <wrn> x", colour, False),
        ("next", None, False),
    ]


def test_sgr_without_a_colour_parameter_is_ignored() -> None:
    assert split(b"\x1b[m\x1b[;m\x1b[?1;m\x1b[38;5mx\n") == [("x", None, False)]


@pytest.mark.parametrize(
    "sgr", [b"38;5;31", b"48;5;31", b"38;2;31;120;200", b"38;2;200;31;31", b"48;2;1;2;31"]
)
def test_an_extended_colour_with_a_component_of_31_is_not_red(sgr: bytes) -> None:
    assert split(b"\x1b[" + sgr + b"mblue\n") == [("blue", None, False)]


def test_an_extended_colour_does_not_hide_a_red_after_it() -> None:
    assert split(b"\x1b[38;5;4;31mx\n") == [("x", "red", False)]


def test_colour_carries_across_a_cut() -> None:
    released = lines(b"\x1b[1;31m" + b"E" * 10 + b"\x1b[0m\r\nnext\n", max_bytes=4)
    assert [line.colour for line in released] == ["red", "red", "red", None]


def test_red_outranks_yellow_in_one_line() -> None:
    assert split(b"\x1b[33ma\x1b[31mb\x1b[33mc\n") == [("abc", "red", False)]


def test_colour_split_across_chunks_is_recognised() -> None:
    assert split(b"\x1b[1;3", b"3mwarn\n") == [("warn", "yellow", False)]


def test_colour_belongs_to_the_erased_partial_not_the_next_line() -> None:
    assert split(b"\x1b[1;31m> \x1b[2D\x1b[J log\n") == [
        ("> ", "red", True),
        (" log", None, False),
    ]


class FakeClock:
    def __init__(self) -> None:
        self.ns = 0

    def __call__(self) -> int:
        return self.ns


def test_idle_releases_a_pending_partial_after_idle_ns() -> None:
    clock = FakeClock()
    splitter = LineSplitter(idle_ns=100, now=clock)
    splitter.feed(b"\x1b[31muart:~$ ", 7)
    clock.ns = 99
    assert splitter.idle() == []
    clock.ns = 100
    [line] = splitter.idle()
    assert (line.text, line.host_ns, line.colour, line.partial) == ("uart:~$ ", 7, "red", True)
    assert splitter.idle() == []


def test_idle_waits_for_idle_ns_after_the_last_bytes() -> None:
    clock = FakeClock()
    splitter = LineSplitter(idle_ns=100, now=clock)
    splitter.feed(b"a", 1)
    clock.ns = 60
    splitter.feed(b"b", 2)
    clock.ns = 159
    assert splitter.idle() == []
    clock.ns = 160
    assert [(line.text, line.host_ns) for line in splitter.idle()] == [("ab", 2)]


def test_an_empty_read_does_not_hold_back_idle_release() -> None:
    clock = FakeClock()
    splitter = LineSplitter(idle_ns=100, now=clock)
    splitter.feed(b"a", 1)
    clock.ns = 60
    assert splitter.feed(b"", 2) == []
    clock.ns = 100
    assert [line.text for line in splitter.idle()] == ["a"]


def test_flush_releases_pending_text_as_a_partial_line_at_once() -> None:
    clock = FakeClock()
    splitter = LineSplitter(idle_ns=100, now=clock)
    splitter.feed(b"\x1b[31mKernel panic", 7)
    [line] = splitter.flush()
    assert (line.text, line.host_ns, line.colour, line.partial) == ("Kernel panic", 7, "red", True)
    assert splitter.flush() == []


def test_pending_since_is_when_text_began_waiting_for_a_line_end() -> None:
    clock = FakeClock()
    splitter = LineSplitter(max_bytes=4, idle_ns=100, now=clock)
    assert splitter.pending_since is None
    clock.ns = 5
    splitter.feed(b"\x1b[31m", 0)
    assert splitter.pending_since is None
    splitter.feed(b"ab", 0)
    clock.ns = 50
    # A cut is not a line end.
    splitter.feed(b"cdefg", 0)
    assert splitter.pending_since == 5
    splitter.feed(b"\n", 0)
    assert splitter.pending_since is None
    splitter.feed(b"x", 0)
    clock.ns = 150
    splitter.idle()
    assert splitter.pending_since is None


def test_reset_drops_pending_text_and_escape_state_but_keeps_the_count() -> None:
    splitter = LineSplitter(max_bytes=2)
    splitter.feed(b"abc\x1b[31", 0)
    splitter.reset()
    assert [(line.text, line.colour) for line in splitter.feed(b"m\n", 0)] == [("m", None)]
    assert splitter.long_lines == 1


# Fragments of what the splitter parses, so random streams form and break real sequences.
FRAGMENTS = (
    b"\x1b|\x1b[|\x1b]|[|]|(|\r|\n|\x07|\\|\0|J|K|D|m|31|38|5|2|93|;|7|\xc3|\xa9|\xe2|a| ".split(
        b"|"
    )
)
streams = st.lists(st.sampled_from(FRAGMENTS) | st.binary(max_size=4)).map(b"".join)


# Three feeds per example, one byte by byte; a slow runner can pass the 200 ms default deadline.
@settings(deadline=None)
@given(stream=streams, data=st.data())
def test_any_chunking_gives_the_same_lines(stream: bytes, data: st.DataObject) -> None:
    cuts = sorted(data.draw(st.lists(st.integers(0, len(stream)))))
    chunks = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
    whole = lines(stream, max_bytes=8)
    assert lines(*chunks, max_bytes=8) == whole
    assert lines(*one_byte_at_a_time(stream), max_bytes=8) == whole


@given(streams | st.binary())
def test_feed_accepts_any_bytes(stream: bytes) -> None:
    LineSplitter(max_bytes=16).feed(stream, 0)
