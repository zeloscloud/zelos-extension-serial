# Contributing

## Prerequisites

- [Zelos CLI](https://docs.zeloscloud.io/latest/getting-started/install-cli/) 0.1.10 or later
- Python 3.11
- [uv](https://docs.astral.sh/uv/)
- [just](https://github.com/casey/just)

The Zelos guide to [developing extensions](https://docs.zeloscloud.io/latest/sdk/how-to/develop-extensions/) covers the SDK, and [Package and Publish Extensions](https://docs.zeloscloud.io/latest/sdk/how-to/package-extensions/) covers the archive and the marketplace.

## Commands

| Command        | Description                                              |
| -------------- | -------------------------------------------------------- |
| `just install` | Install dependencies                                     |
| `just fmt`     | Format and fix lint with ruff                            |
| `just check`   | Format check, lint, pyright and pytest: the merge gate   |
| `just test`    | Run tests                                                |
| `just dev`     | Run the extension against `./config.json`                |
| `just package` | Package for the Zelos marketplace                        |
| `just clean`   | Remove build artifacts                                   |

CI runs `just check` on Linux, macOS and Windows for every pull request and every push to `main`.
The RFC 2217 workflow runs the RFC 2217 tests against ser2net 4.3.4, 4.6.0 and 4.6.7 on the same events.

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

## Local run

```bash
echo '{"ports": [{"connection": "demo"}]}' > config.json
just dev
```

`just dev` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`. To run it under the agent instead:

```bash
zelos extensions install-local .
zelos extensions start local.serial --config '{"ports": [{"connection": "demo"}]}'
zelos extensions logs local.serial
```

## Releasing

```bash
zelos extensions bump X.Y.Z   # writes extension.toml and pyproject.toml
# Set __version__ in zelos_extension_serial/__init__.py to X.Y.Z, and add the release to CHANGELOG.md.
uv lock
just check
git commit -am "Release vX.Y.Z" && git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push --follow-tags
```

The tag runs `.github/workflows/release.yml`:

1. It runs CI on every OS and the RFC 2217 tests on every ser2net version; both must pass.
2. It checks that the tag matches the version in `extension.toml` and `pyproject.toml`.
3. It runs `just package`, and checks that the archive's `actions.json` lists `list_ports` and `auto_config`.
4. It creates a GitHub release with `serial-X.Y.Z.tar.gz` attached.

The Zelos marketplace picks up each new release.
