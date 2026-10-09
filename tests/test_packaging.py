"""What `zelos extensions package` and the marketplace read, checked before a tag is pushed."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from zelos_extension_serial import ACTION_PREFIX, __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((REPO_ROOT / "extension.toml").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_entry_module_re_exports_the_action_prefix() -> None:
    """The inventory dump reads the prefix off the entry module and falls back to its file name,
    so a missing re-export ships `main/list_ports` beside a live `Serial/list_ports`."""
    import main

    assert main.ACTION_PREFIX == ACTION_PREFIX == MANIFEST["name"] == "Serial"


def test_versions_agree() -> None:
    assert __version__ == MANIFEST["version"] == PYPROJECT["project"]["version"]


@pytest.mark.parametrize("relative", MANIFEST["package"]["paths"])
def test_package_path_exists(relative: str) -> None:
    assert (REPO_ROOT / relative).exists()


README_MEDIA = re.findall(
    r"\]\((assets/[^)\s]+)\)", (REPO_ROOT / MANIFEST["readme"]).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("relative", README_MEDIA)
def test_readme_media_is_packaged(relative: str) -> None:
    """The app shows an installed extension's README pictures from its package, not from GitHub."""
    assert (REPO_ROOT / relative).is_file()
    assert any(relative.startswith(f"{path}/") for path in MANIFEST["package"]["paths"])


def test_readme_has_media() -> None:
    assert len(README_MEDIA) >= 3


@pytest.mark.parametrize("key", ["icon", "readme"])
def test_referenced_file_exists(key: str) -> None:
    assert (REPO_ROOT / MANIFEST[key]).is_file()


def test_entry_point_is_packaged() -> None:
    assert MANIFEST["host"]["agent"]["entry"] in MANIFEST["package"]["paths"]


def test_config_schema_is_valid_and_its_examples_validate() -> None:
    """Draft 7, because it is the draft that enforces the per-connection `dependencies`."""
    schema = json.loads((REPO_ROOT / MANIFEST["config"]["schema"]).read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    for example in schema["examples"]:
        validator.validate(example)


def test_inventory_holds_the_two_read_only_standalone_actions(tmp_path: Path) -> None:
    """The agent serves this file while the extension is stopped; the config form's port list and
    Auto-configure need both actions in it."""
    out = tmp_path / "actions.json"
    subprocess.run(
        [
            *(sys.executable, "-m", "zelos_sdk.extensions.actions", "dump"),
            *("--entry", MANIFEST["host"]["agent"]["entry"], "--out", str(out)),
        ],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
    )

    inventory = json.loads(out.read_text(encoding="utf-8"))
    assert inventory["action_prefix"] == ACTION_PREFIX
    assert {a["path"]: a["read_only"] for a in inventory["actions"]} == {
        "list_ports": True,
        "auto_config": True,
    }


def test_importing_every_module_starts_nothing() -> None:
    """Packaging and standalone action runs import the entry module; it must not start the app."""
    script = (
        "import importlib, pkgutil, threading\n"
        "import main, zelos_extension_serial as package\n"
        "for module in pkgutil.iter_modules(package.__path__):\n"
        "    importlib.import_module(f'{package.__name__}.{module.name}')\n"
        "assert threading.active_count() == 1, threading.enumerate()\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, check=True, timeout=60)
