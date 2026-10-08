"""Trace-safe names: sanitising, the 64-character limit, de-duplication."""

import re

import zelos_sdk
from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_serial import config
from zelos_extension_serial.naming import Namer, ascii_name, safe_name, unique_name

CONFIG_NAME = config._SCHEMA["properties"]["ports"]["items"]["properties"]["name"]["pattern"]


def is_trace_valid(name: str) -> bool:
    return zelos_sdk.sanitize_name(name, kind="field") == name and 0 < len(name) <= 64


def test_safe_name_sanitises_and_cuts_at_64_characters() -> None:
    assert safe_name("Engine.RPM[0]") == "Engine_RPM_0"
    assert safe_name("...") == "unnamed"
    assert safe_name("x" * 63 + ".y") == "x" * 63


def test_the_same_raw_name_always_gets_the_same_name() -> None:
    namer = Namer()
    assert namer.name("a.b") == "a_b"
    assert namer.name("a:b") == "a_b_2"
    assert namer.name("a.b") == "a_b"
    assert namer.name("a:b") == "a_b_2"


def test_reserved_names_are_never_handed_out() -> None:
    namer = Namer(["log", "values"])
    assert namer.name("log") == "log_2"
    assert namer.name("values") == "values_2"
    assert namer.name("dcdc") == "dcdc"


def test_names_that_differ_only_in_case_are_distinct() -> None:
    namer = Namer(["log", "values", "time_ns"])
    assert [namer.name(raw) for raw in ["A", "a", "MAIN", "main", "Main"]] == [
        "A",
        "a_2",
        "MAIN",
        "main_2",
        "Main_3",
    ]
    assert [namer.name(raw) for raw in ["LOG", "Values", "TIME_NS"]] == [
        "LOG_2",
        "Values_2",
        "TIME_NS_2",
    ]


def test_a_reserved_name_in_another_case_is_suffixed_like_any_case_variant() -> None:
    namer = Namer(["log", "values"])
    assert [namer.name(raw) for raw in ["Values", "values", "Values", "VALUES"]] == [
        "Values_2",
        "values_3",
        "Values_2",
        "VALUES_4",
    ]


def test_unique_name_ignores_case_in_names_already_used() -> None:
    used = {"DUT"}
    assert unique_name("dut", used) == "dut_2"
    assert used == {"DUT", "dut_2"}


def test_suffixes_skip_names_already_taken() -> None:
    namer = Namer()
    assert [namer.name(raw) for raw in ["a", "a_2", "a.", "a:"]] == ["a", "a_2", "a_3", "a_4"]


def test_a_suffixed_name_stays_within_64_characters() -> None:
    namer = Namer()
    raw = "a" * 61 + " " + "b" * 10
    assert namer.name(raw) == "a" * 61 + " bb"
    assert namer.name(raw + "!") == "a" * 61 + "_2"
    assert namer.name("x" * 70) == "x" * 64
    assert namer.name("x" * 71) == "x" * 62 + "_2"


def test_a_suffixed_name_stays_within_128_bytes() -> None:
    # The SDK cuts a name at 128 bytes, which would cut the suffix off.
    namer = Namer()
    raw = "\N{MATHEMATICAL BOLD CAPITAL A}" * 32
    assert namer.name(raw) == raw
    assert namer.name(raw + "!") == raw[:31] + "_2"
    assert is_trace_valid(raw[:31] + "_2")


def test_ascii_name_drops_accents_and_other_non_ascii() -> None:
    assert ascii_name("Écran série") == "Ecran serie"
    assert ascii_name("µC board") == "C board"
    assert ascii_name("串口") is None
    assert ascii_name("串口 _") is None


@given(raw=st.text())
def test_an_ascii_name_fits_the_config(raw: str) -> None:
    name = ascii_name(raw)
    assert name is None or re.fullmatch(CONFIG_NAME, unique_name(name, set()))


# Long shared prefixes make names collide after the cut at 64 characters.
raw_names = st.text() | st.builds(
    lambda n, tail: "x" * n + tail, st.integers(0, 70), st.text("ab. ", max_size=3)
)


@given(raws=st.lists(raw_names), reserved=st.lists(raw_names.map(safe_name)))
def test_names_are_stable_distinct_and_trace_valid(raws: list[str], reserved: list[str]) -> None:
    namer = Namer(reserved)
    names = {raw: namer.name(raw) for raw in raws}
    assert all(namer.name(raw) == name for raw, name in names.items())
    folded = {name.casefold() for name in names.values()}
    assert len(folded) == len(names)
    assert not folded & {name.casefold() for name in reserved}
    assert all(is_trace_valid(name) for name in names.values())
