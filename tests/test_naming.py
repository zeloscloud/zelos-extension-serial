"""Port names: an ASCII name made from a device's product name fits the config's Name field."""

import re

from hypothesis import given
from hypothesis import strategies as st
from zelos_log_lines.naming import unique_name

from zelos_extension_serial import config
from zelos_extension_serial.actions import ascii_name

CONFIG_NAME = config._SCHEMA["properties"]["ports"]["items"]["properties"]["name"]["pattern"]


@given(raw=st.text())
def test_an_ascii_name_fits_the_config(raw: str) -> None:
    name = ascii_name(raw)
    assert name is None or re.fullmatch(CONFIG_NAME, unique_name(name, set()))
