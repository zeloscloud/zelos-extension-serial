# Changelog

All notable changes to this extension are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the extension uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-07

### Added

- Record local serial ports, raw TCP ports and RFC 2217 ports, plus a demo device that prints like a Zephyr board. Runs on Linux, macOS and Windows, and needs Zelos 26.0.8 or later.
- Log every line to `Serial/<port>/log` as the device printed it, with its time stamp and level marker moved to their own columns and its module as the name. Read the log prefixes of Zephyr (uptime, date and tick stamps), ESP-IDF, Arduino on ESP32 and the Linux kernel, and a leading level word. Mark sent text with `> ` and the extension's notes with `[serial] `. Take the level of an unprefixed line from its colour. Leave shell prompts and the echo of sent text out of the log.
- Log a line still pending when the port closes, and cut a line longer than 4096 bytes into logged pieces.
- Turn printed values into signals, one event per module: Teleplot, `key=value`, the Arduino plotter's labelled and bare forms, and a repeated `label: value`. Hold a module's rows for up to 2 s, so its event gets every name, even from a format that prints one name per line. Give no values from a line of hex, a hex dump after a log prefix, a partial line or a piece of a long line.
- Place lines that carry the device's uptime on the host clock by that uptime, and note a device restart.
- Name a port from its config alone, and find a USB device by its ID, `usb:vvvv:pppp:SERIAL`, on any path.
- Reconnect when a device or server comes back. Open serial ports exclusively on Linux and macOS, and detect a vanished network peer with TCP keepalive.
- Fill the config form: List Ports for the **Port** field, and Auto-configure to add a port for each new USB serial device.
- Add the actions Send, Run Command, Reset Device, Release Port, Acquire Port, Get State and Sample; each acts on the first configured port when the caller names none. Release and acquire hand the port to a flasher; acquire waits for a board that appears again after its flash.
- Report each port's health in one sentence, and each config mistake in one sentence that names the port and the field.
- Work with ser2net 4.3.4, 4.6.0 and 4.6.7 over RFC 2217.
