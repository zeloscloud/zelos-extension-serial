# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/) 0.9.5 or later

The Zelos guide to [developing extensions](https://docs.zeloscloud.io/latest/sdk/how-to/develop-extensions/) covers the SDK, and [Package and Publish Extensions](https://docs.zeloscloud.io/latest/sdk/how-to/package-extensions/) covers the archive and the marketplace.

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

The package `zelos_extension_serial/`:

| Module | What it holds |
| ------ | ------------- |
| `app.py` | The life cycle: one worker per configured port until SIGTERM or SIGINT |
| `config.py` | The configuration types, loaded and checked against `config.schema.json` |
| `worker.py` | One thread per port: it opens, reads, writes and closes its transport |
| `transport.py` | The byte pipe a worker owns (serial, TCP, RFC 2217), and its faults |
| `rfc2217.py` | serialx's RFC 2217 client, with fixes for ser2net |
| `demo.py` | The demo board, a simulated Zephyr board |
| `prompts.py` | Shell prompt detection |
| `actions.py` | The actions the Action panel, notebooks and Zelos AI run |
| `discovery.py` | The serial devices on this machine |
| `baud.py` | The baud a device prints at, found by listening at each common rate |

`docs/reference.md` documents every format, configuration key, action and health message.
Tests live in `tests/`: `tests/integration/` plays consoles to the running extension over TCP and a pseudo-terminal, and `tests/perf/` measures throughput.

## Commands

| Command                | Description                                         |
| ---------------------- | --------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks       |
| `just format`          | Format code with ruff                               |
| `just check`           | Check formatting, lint (ruff), type-check (pyright) |
| `just test`            | Run tests (pytest)                                  |
| `just ci`              | Install from the lockfile, check, test and package  |
| `just run`             | Run the extension against `./config.json`           |
| `just package`         | Build and package for the Zelos marketplace         |
| `just release VERSION` | Bump version, check, test, build, commit, tag       |
| `just clean`           | Remove build output, caches and packages            |

`just --list` also shows the other recipes. `just install` installs the pre-commit hooks in `.pre-commit-config.yaml`.

CI runs `just check` and `just test` on Linux, macOS and Windows for every pull request and every push.
The RFC 2217 workflow runs the RFC 2217 tests against ser2net 4.3.4, 4.6.0 and 4.6.7 for every pull request and every push to `main`.

The throughput test checks for zero loss and runs on Linux only:

```bash
uv run pytest -m perf
```

## RFC 2217 tests against ser2net

The RFC 2217 tests in `tests/test_rfc2217.py` also run against a real ser2net.
ser2net serves one end of a pseudo-terminal pair as RFC 2217, and a writer prints `L<8 digits>` lines into the other end about every 5 ms.
`.github/workflows/rfc2217.yml` builds that setup.
The tests skip when these are unset:

| Variable | Value |
|---|---|
| `SER2NET_HOST`, `SER2NET_PORT` | Where ser2net accepts RFC 2217 |
| `SER2NET_DEVICE` | The pseudo-terminal ser2net serves |
| `SER2NET_PEER` | The other end, where the writer prints |
| `SER2NET_KILL` | A command that stops ser2net, such as `pkill -x ser2net` |

```bash
SER2NET_HOST=127.0.0.1 SER2NET_PORT=2217 SER2NET_DEVICE=/tmp/devA SER2NET_PEER=/tmp/devB \
  SER2NET_KILL='pkill -x ser2net' uv run pytest tests/test_rfc2217.py -v
```

## Run it in Zelos

```bash
zelos extensions install-local .
zelos extensions start local.serial --config '{"ports": [{"connection": "demo"}]}'
zelos extensions logs local.serial
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall local.serial`.
- `extension.toml`: run `zelos extensions install-local .` again.

To run it outside the agent:

```bash
echo '{"ports": [{"connection": "demo"}]}' > config.json
just run
```

`just run` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

Add the release to `CHANGELOG.md` and commit it, then:

```bash
just release 1.0.0
git push --follow-tags
```

The tag runs `.github/workflows/release.yml`:

1. It runs CI on every OS and the RFC 2217 tests on every ser2net version; both must pass.
2. It checks that the tag matches the version in `extension.toml` and `pyproject.toml`.
3. It checks, tests and runs `just package`, and checks that the archive's `actions.json` lists `list_ports` and `auto_config`.
4. It creates a GitHub release with `serial-1.0.0.tar.gz` attached.

The Zelos marketplace picks up each new release.
