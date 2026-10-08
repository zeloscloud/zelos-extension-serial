# Zelos extension for serial consoles

Serial records what a device prints on its console: every line becomes a log event, and every printed value becomes a signal.
It reads local serial ports, raw TCP ports and RFC 2217 ports, on Linux, macOS and Windows.

## Quick start

1. **Install** Serial from the extension marketplace in the Zelos App (Zelos 26.0.8 or later).
2. **Configure** a port: pick a device in the **Port** list, or press **Auto-configure** to add one port for each USB serial device that no port uses yet.
3. **Start** the extension. Lines appear in the Log panel, and values appear as signals.

Configure Serial from a Zelos App 26.0.8 or later: an app 26.0.7 or older cannot show its config form.

No hardware? Set **Connection** to **Demo board**, or press Auto-configure with no port and no device.
The demo device prints the console of a Zephyr board: a power converter status line every 20 ms and a battery warning every 5 s.
It answers the shell commands `dcdc limit get` and `dcdc limit set <amps>`.

## What it records

All events go under one trace source, `Serial` by default (**Prefix** in Advanced (all ports)).
Each port has a name, set by **Name** in the config.

| Event | Holds |
|---|---|
| `Serial/<port>/log` | Every line the device printed, as it printed it, without the time stamp and level marker: those have their own columns. The level, the module (`name`), and the file and line where the format prints them. Also the text sent by Send and Run Command (name `tx`), and the extension's notes on the port (name `serial`). |
| `Serial/<port>/<module>` | The values from lines that carry a module, such as Zephyr's `dcdc` or an ESP-IDF tag. One event per module, one field per value. |
| `Serial/<port>/kernel` | The values from Linux kernel lines. |
| `Serial/<port>/values` | The values from lines without a module. |
| `Serial/log` | The extension's own log: warnings about late names, units and caps, failed opens, and unexpected errors. |

In the Log panel, the **Source** column shows the name: the module for a device line, `tx` for sent text, and `serial` for a note.

A port without a Name takes a name from its config: the **Port** field, `<host>:<tcp port>` for a network port, or `demo`.
The name is made safe for the trace: `/dev/ttyUSB0` becomes `dev_ttyUSB0`, `usb:0403:6001:A50285BI` becomes `usb_0403_6001_A50285BI`, `COM7` stays `COM7`, and `bench.local:2217` becomes `bench_local_2217`.
It does not depend on which devices are plugged in, so data from one board always lands under one name.
Names compare without regard to case.
Two ports that would get the same name get `_2`, `_3` and so on; two Names you set that differ only in case are a config error.

The notes on `Serial/<port>/log` are: `[serial] connected to <where>`, `[serial] disconnected from <where>`, `[serial] released <where>`, `[serial] acquired <where>`, `[serial] reset pulse on dtr` (or `rts`), and `[serial] device restarted`.
A `tx` row holds `> ` and the text sent, `> hex: <bytes>` for a hex send, or `> (line ending)` when the text was empty.
The marks keep these rows apart from device output where only the message shows.

**Lines.** A line ends at CR, LF or CRLF.
Terminal escape sequences and NUL bytes are removed.
A line with no level of its own is logged as `error` when the device printed it in red (colour codes 31, 91, 41 or 101), and as `warn` in yellow (33, 93, 43 or 103).

A line with no line end yet is released as a partial line after 100 ms without bytes, when a shell redraws its prompt (a cursor-left, maybe a cursor-up for a wrapped command, then an erase, as Zephyr's shell does), and when the port closes: on a disconnect, on Release Port and on stop.
So the last line a device printed before it crashed is logged.
A partial line is logged but gives no values: a pause can split `rail=13.64V` into `rail=13.6`.

A line longer than 4096 bytes is cut into pieces of at most 4096 bytes, never inside a UTF-8 character.
Each piece is logged with the line's colour; no piece gives values or counts as a prompt or an echo.
The `long_lines` counter counts the cuts.

**First line.** After each connect, the first line is logged as usual.
When it has no log prefix and carries values, its values are dropped: the device was most likely in the middle of printing it when the port opened, so its first value may be cut.

**Prompts.** Shell prompts are not logged and never carry values.
The default set covers prompts such as `uart:~$ `, `# `, `$ `, `root@host:~# `, `esp32> `, `[esp32]> `, `=> `, `>>> `, `... `, `buildroot login: ` and `Password: `.
A redrawn prompt with half-typed text after it is not logged either.
When a line with a log prefix follows a prompt with no line end between them, as when a device logs right after printing its prompt, the prompt is counted, not logged, and the log line is logged on its own with its level, module and time, and with its values unless the line is partial.
Set **Prompt** in Advanced to a regular expression for your prompt; it must match the whole line, and it replaces the default set.

**Device clock.** With **Time source** `auto`, a line that carries the device's uptime is placed on the host clock by that uptime.
The extension uses the smallest offset between the two clocks over the last 10 s, so a line delayed on its way to the host keeps its device time.
When the uptime drops by more than 100 ms, that line takes host time, and the next line with an uptime decides:

- Still below the highest uptime: the device restarted. The note `[serial] device restarted` lands on that line, and the clock starts again from it. A device that restarts more often than once a second is caught too.
- At or above it again: the drop was one stray line, such as `I (0) cpu_start: App cpu up.` from ESP-IDF's second core, and nothing restarts.

Two known cases write a `[serial] device restarted` note when the device did not restart; the times stay right in both:

- Zephyr's 32-bit log timestamp wraps: after 36.4 h with a 32768 Hz clock, or after 49.7 days with `k_uptime_get_32`. Each wrap writes one note.
- Typing `dmesg` at a Linux console prints the old kernel lines again, and writes one note.

Lines without an uptime take the time the host read them.
With `host`, every line takes the time the host read it.
Host time follows the computer's wall clock when the two differ by more than 1 s. After a sleep or a step forward, it jumps to the wall clock. After a step back, line times advance 1 µs per line until the wall clock catches up.
Within a port, times always increase, so lines keep the order the device printed them in.

**Signals.** The trace fixes the fields of an event when it creates the event.
So the extension holds a module's value rows until it knows the module's names.
It waits until 2 s have passed since the first held row, until 64 names or 1,000 rows are held, or until the port closes or the extension stops.
Then it creates `Serial/<port>/<module>` with every name it saw, each with the unit it was first printed with, and writes the held rows with their own times.
Log rows are never held.
So Teleplot and `label: value`, which print one name per line, record every name they print in those 2 s.
A module's signals appear about 2 s after its first value, and Get State lists them from then on.

A later line can leave fields out; they are empty for that sample.

**Late names.** A name that first appears after its module's event exists is not recorded, with one warning on `Serial/log`:

```
'<name>' first appeared after <port>/<module>'s signals were set up, so it is not recorded. Restart the extension to include it.
```

A module warns about at most 32 late names; after that, one last warning says `<port>/<module>: further new values are dropped without a warning`.
A value printed later with a different unit is written anyway, with one warning per name: `<port>/<module>: '<name>' printed in <unit>, first printed in <unit>; written anyway`.
A port records at most 64 modules as signals.
The lines of further modules are still logged, and one warning says `<port>: further modules are not recorded as signals; their lines are still logged`.

Names pass through the trace's naming rules: `motor.rpm` becomes the field `motor_rpm`.
Module and field names compare without regard to case: a module `MAIN` after `main` becomes `MAIN_2`.
A module named `log` or `values`, in any case, gets `_2` (`Values` becomes `Values_2`); like any two names that differ only in case, a later `values` then gets `_3`.
A value named `time_ns` becomes the field `time_ns_2`.

## Recognised formats

The extension reads the log prefix first, then looks for values in the text after the module.
Everything else is a plain log line at level `info`.
The logged message is the line as printed, without the time stamp and the level marker.

### Log prefixes

| Format | Example | Logged message | Level | Module | Device time |
|---|---|---|---|---|---|
| Zephyr | `[00:00:05.000,000] <wrn> bms: cell imbalance 30mV` | `bms: cell imbalance 30mV` | warn | `bms` | yes |
| Zephyr, date | `[2026-10-07 12:00:01.122,348] <err> main: Error message example.` | `main: Error message example.` | error | `main` | no |
| Zephyr, ticks | `[0000032768] <err> main: Error message example.` | `main: Error message example.` | error | `main` | no |
| ESP-IDF | `E (588) SPIFFS: mount failed, -10025` | `SPIFFS: mount failed, -10025` | error | `SPIFFS` | yes, from the default millisecond stamp |
| Arduino on ESP32 | `[  1065][E][sd_diskio.cpp:807] sdcard_mount(): f_mount failed` | `[sd_diskio.cpp:807] sdcard_mount(): f_mount failed` | error | `sd_diskio`, with file and line | yes |
| Linux kernel | `[    1.234567] usb 1-1: new device` | `usb 1-1: new device` | info | `kernel` | yes |
| A level word | `ERROR: invalid argument` | `invalid argument` | error | none | no |

Zephyr also prints a seconds stamp, `[    1.106051] <wrn> main: low battery`, an ISO 8601 stamp, `[2026-10-07T12:00:01,122348Z]`, and 20-digit ticks; all read as Zephyr.
Zephyr's thread (`[  0 main] `), interrupt (`[irq] `) and core (`[core 1] `) prefixes stay in the message; the module and values are read after them: `[00:00:00.106,051] <inf> [  0 main] app.inst1: up` is logged as `[  0 main] app.inst1: up`, module `app`.
Zephyr's `--- 3 messages dropped ---` is logged as `warn`.
ESP-IDF's Wi-Fi library prints no space after its tag, `I (558) wifi:wifi driver task: 3ffc1e4c`, and reads the same way; it is logged as `wifi:wifi driver task: 3ffc1e4c`.
The Linux kernel's caller ID stays in the message: `[    0.000000][    T0] Booting Linux` is logged as `[    T0] Booting Linux`.
A level word can also sit in brackets (`[ERR] timeout`, `[WRN]`, `[INF]`, `[CRIT]`) or be written in title case with a colon (`Warn: retrying`); the word is not part of the message, so `[WRN] low battery` is logged as `low battery`.
`warning: gcc style` is not a level word and is logged unchanged.
`Error count: 5` is not a level word: a title-case word needs a colon right after it.

### Values

The first habit that matches the text after the module wins.

| Habit | Example | Signals |
|---|---|---|
| Teleplot | `>myValue:1234§km²` | `myValue` = 1234, unit `km²` |
| key=value | `rail=13.65V in=4.50A limit=4.5A temp=35.0C` | `rail` = 13.65 V, `in` = 4.5 A, `limit` = 4.5 A, `temp` = 35 C |
| Arduino plotter, labelled | `label_1:1,label_2:2` | `label_1` = 1, `label_2` = 2 |
| Arduino plotter, bare | `5,6` | `value1` = 5, `value2` = 6, from the third line in a row with the same number of columns |
| label: value | `Voltage: 3.29 V` | `Voltage` = 3.29 V, from the second line with the same label; the label has at most three words and no hex literal |

The plotter habits split on commas, tabs and spaces.
A Teleplot name can hold `.` and `/`, such as `>motor.rpm:1200`.
A Teleplot line with several points records the last one, and a line flagged `|t` (text) or `|xy` records nothing.

A number is decimal, with an optional `+` or `-` sign and an optional exponent.
A whole number with leading zeros and up to three digits is decimal: `09` is 9, `007` is 7, `00` is 0.
Four or more digits led by a zero, such as `0403` or `00010020`, is not a number.
`0xFF`, `nan` and `inf` are not numbers, and `flags=0xFF` gives no value.
A unit is up to 8 letters and symbols such as `%`, `°`, `µ`, `Ω` and `²`; a digit can appear only after a `/`, as in `m/s2`.
So `5V2` gives no value.

In key=value, a value that only hex explains marks a line of hex, and the line gives no values at all, because its other values are hex too.
A value only hex explains is four or more digits led by a zero (`idVendor=0403`, `paddr=00010020`), a digit after a letter (`crc=7bd5c66f`, `size=0a3ech`, `flags=0b1010`), or digits followed by lower-case a–f (`x=1f`).
A unit made of hex letters, such as `5dB`, `2A` or `25C`, is a value.
A word with no digit, such as `id=dead`, is ignored.

After a log prefix, a message of eight or more two-digit hex bytes, such as `I (123) tag: 10 20 30 40 50 60 70 80` from `ESP_LOG_BUFFER_HEX`, is a hex dump and never plotted.
Without a prefix, such a line is plotter columns.
The label: value rule waits for a second appearance, so a one-off sentence such as `Note: 5 items` stays a log line.
A label with a hex literal names what the line reports on, so `send of 0x300: -114` stays a log line too.

Values work after any log prefix.
`I (146929) deep_sleep: Enabling timer wakeup, timeout=1000000us` gives `timeout` = 1000000 us on `Serial/<port>/deep_sleep`.

## Configuration

The config holds a list of `ports` and one `advanced` group.
Each port picks a **Connection**: `serial` (Serial port), `tcp` (TCP), `rfc2217` (RFC 2217) or `demo` (Demo board).

| Key | Title | serial | tcp | rfc2217 | Default |
|---|---|---|---|---|---|
| `connection` | Connection | yes | yes | yes | `serial` |
| `name` | Name | yes | yes | yes | see [What it records](#what-it-records) |
| `port` | Port | required | | | |
| `host` | Host | | required | required | |
| `tcp_port` | TCP port | | required | required | |
| `baud` | Baud | yes | | yes | `115200` |

A `demo` port takes only `connection` and `name`.
Names use letters, digits, space, `_` and `-`, up to 64 characters.
Baud runs from 50 to 20000000.

### Advanced, per port

These go in the port's own `advanced` object.

| Key | Title | serial | tcp | rfc2217 | Default | Values |
|---|---|---|---|---|---|---|
| `data_bits` | Data bits | yes | | yes | `8` | 5, 6, 7, 8 |
| `parity` | Parity | yes | | yes | `none` | none, even, odd, mark, space |
| `stop_bits` | Stop bits | yes | | yes | `"1"` | "1", "1.5", "2" |
| `flow_control` | Flow control | yes | | yes | `none` | none, rts/cts, xon/xoff |
| `dtr` | DTR at open | yes | | | `on` | on, off |
| `rts` | RTS at open | yes | | | `on` | on, off |
| `reset_line` | Reset line | yes | | | `none` | none, dtr, rts |
| `line_ending` | Line ending | yes | yes | yes | `LF` | LF, CRLF, CR |
| `prompt` | Prompt | yes | yes | yes | empty: the default prompts | a regular expression |
| `values` | Turn printed values into signals | yes | yes | yes | `true` | true, false |

**Line ending** is what Send and Run Command add after the text.
Received lines end at CR, LF or CRLF whatever it says.

### Advanced, for all ports

These go in the top-level `advanced` object, titled **Advanced (all ports)** in the form.

| Key | Title | Default | Values |
|---|---|---|---|
| `prefix` | Prefix | `Serial` | letters, digits, space, `_` and `-` |
| `time_source` | Time source | `auto` | auto, host |
| `log_level` | Log level | `INFO` | DEBUG, INFO, WARNING, ERROR |

### Config errors

The extension checks the config when it starts.
For each mistake it logs one sentence that names the port and the field, then exits:

```
Port 1 (serial), Stop bits: must be "1", "1.5" or "2".
Port 2 (demo): name 'dut' is already used by port 1.
Port 3 (tcp), Prompt: is not a valid regular expression (unterminated character set at position 0).
Settings, Advanced (all ports): unknown setting foo.
```

A setting a port does not use, such as `baud` on a `tcp` port, is ignored.

### Device IDs

The **Port** list shows each serial device by product name and device path, such as `FT232R USB UART (/dev/ttyUSB0)`.
Below it are the `/dev/serial/by-id` path where it differs, the manufacturer, the vendor and product ID, and the serial number.
On Windows, the product name is the name the driver gives the port, without the port itself.
The built-in USB CDC driver lists a device as `USB Serial Device (COM4)`, made by `Microsoft`, so Auto-configure names it `USB Serial Device`.
When the device has a serial number, the list stores its device ID instead of the path:

```
usb:0403:6001:A50285BI
```

The ID is `usb:`, the vendor ID and product ID in lower-case hex, and the serial number.
The operating system assigns the path, such as `COM7` or `/dev/ttyUSB0`, each time the device appears, so it can change when you replug or add a device.
The vendor ID, product ID and serial number are stored in the device, so the ID finds the same device on any USB port.
A serial number shorter than 4 characters is not used: Windows makes up a short one for a device that has none.
For such a device, the list stores the path.
When two connected devices share one ID, the extension uses the first by path and logs a warning.

The first time the Port list opens after Serial is installed, the agent can take more than 10 s to start it, and the list gives up after 10 s.
Open the list again. Auto-configure waits up to 30 s.

### Examples

```json
{ "ports": [{ "connection": "serial", "port": "usb:0403:6001:A50285BI", "baud": 115200, "name": "dut" }] }
```

```json
{ "ports": [{ "connection": "tcp", "host": "127.0.0.1", "tcp_port": 4001, "name": "dut" }, { "connection": "tcp", "host": "127.0.0.1", "tcp_port": 4002, "name": "linux" }] }
```

```json
{ "ports": [{ "connection": "demo" }] }
```

### Reconnects

When a port fails or the device is unplugged, the extension closes it and tries again.
An absent device or a missing path is retried every 0.5 s, so a replugged board is open in time for its boot output.
Other failed opens back off from 0.5 s, doubling up to 10 s; the backoff resets once bytes arrive.
A port that sends nothing for 5 s is checked for its device, because Windows can leave a removed device open and silent.
On Windows, that check uses the device listing, and only for a path that was listed when the port opened.
A virtual port, such as com0com, may not be listed: it is opened anyway, and never judged gone by the listing.
An unsupported setting, such as a baud the driver refuses, stops that port until you change the config.

A network port looks up its host for at most 2 s, then tries each address the lookup returns for at most 2 s.
On an open TCP or RFC 2217 connection, the extension turns on TCP keepalive: after 10 s without bytes it sends a probe every 5 s.
A server that vanished without closing the connection is noticed after about 30 s, or about 60 s on Windows, which always sends 10 probes.
On Linux, a Send during the outage restarts that count from the Send, so detection takes up to about 60 s; on macOS it stays about 30 s.
A Send during the outage still reports the bytes it handed to the operating system as written, though they never arrive.

On Linux and macOS, the extension opens a serial port exclusively: a program that opens the port after it gets "device busy".
Two limits remain.
A program that opened the port before the extension keeps reading it and can take every byte.
Measured on Linux with an ST-LINK V3: the other program got all 1914 bytes of three boots, and the extension none.
Health then shows `Connected. 0 lines, …`, and after 10 s `Connected. No bytes yet. …`.
Once the other program closes the port, the extension gets the bytes.
A program that runs as root can open the port anyway.
macOS pseudo-terminals ignore the exclusive open.
Windows always opens a serial port exclusively.

## Actions

Every action except the first two takes `port`, titled **Port name**: the port's **Name** from the config, not its path.
A port without a Name uses its default name, the `<port>` in `Serial/<port>/log`.
`port` is optional. The form starts on the first configured port, in config order, and a caller that leaves `port` out, such as a notebook, Zelos AI or the CLI, acts on that port.
With no port configured, leaving it out fails with `no port is configured`.

| Action | Path | What it does | Read-only |
|---|---|---|---|
| List Ports | `Serial/list_ports` | Lists every serial port on this machine, USB or not, for the **Port** field. Works while the extension is stopped. | yes |
| Auto-configure | `Serial/auto_config` | Keeps the configured ports and adds one `serial` port for each USB serial device that no port uses yet. Opens no port. Works while the extension is stopped. | yes |
| Send | `Serial/send` | Writes `text` and the line ending. With `hex`, writes `text` as hex bytes, such as `0d0a`, and adds nothing. | no |
| Run Command | `Serial/command` | Sends `text` as a shell command and returns the lines the device prints in reply. | no |
| Reset Device | `Serial/reset` | Pulses the port's reset line for 100 ms. | no |
| Release Port | `Serial/release` | Closes the port so another program can open it. DTR and RTS stay as they are. | no |
| Acquire Port | `Serial/acquire` | Opens a released port again. | no |
| Get State | `Serial/get_state` | Returns the port's state, health, clock, counters and signals. | yes |
| Sample | `Serial/sample` | Returns the last lines the port received, or its last raw reads as hex. | yes |

**List Ports** returns one choice per port: its device ID, else its path; the label and detail that the [Port list](#device-ids) shows.
With no port to list, it returns the message `No serial devices found. Type a path such as /dev/ttyUSB0 or COM7.`; apps newer than 26.0.9 show it under the field.

**Auto-configure** treats a device as used when the **Port** field of a `serial` port holds its device ID or one of its paths; a `port` key left on a TCP, RFC 2217 or demo entry does not count; on Windows, paths compare without regard to case or a `\\.\` prefix.
A new port takes the device's product name as its Name, kept unique; a device that reports no product name gets the default name.
When there are no ports and no USB serial devices, it adds a `demo` port.
It returns one of these messages: `Added 1 serial device.` (or `Added <n> serial devices.`), `No new USB serial devices found.` or `No USB serial devices found; added the demo device.`
Zelos 26.0.8 sends no config to it: there it returns only the devices it finds (or the demo port), which replace the ports in the form, and the app does not show the message.

**Run Command** takes `timeout_s`, from 0.1 to 60, default 2.
The reply ends at the next prompt, after 300 ms without bytes once a reply line has arrived, or at the timeout.
A prompt that runs into a log line ends the reply too, and the log line stays out of it.
Lines with a log prefix (Zephyr, ESP-IDF, Arduino on ESP32, Linux kernel) are logged but left out of the reply, and so is Zephyr's `--- n messages dropped ---` notice.
Every other line the device prints during the command joins the reply, such as boot lines without a log prefix after a reset.
The device's echo of a line sent by Send or Run Command, bare or after a prompt within 2 s, is neither logged nor part of the reply; the `tx` row on the port's log already records the text.
When the device echoes a line twice, only the first echo is hidden; the second is logged and joins the reply.
A prompt ends the reply only after the echo or a reply line, so a command on a device that neither echoes nor prints ends at the timeout.
A port runs one command at a time; Send still works during a command.
A caller that stops waiting early, such as a CLI with a shorter timeout, does not end the command: until `timeout_s` passes, another Run Command fails with `command in progress`.

```json
{ "reply": ["limit set to 2.0 A"], "ended_by": "prompt", "duration_ms": 18 }
```

`ended_by` is `prompt`, `quiet`, `timeout`, or `aborted` when the port was released, lost or stopped.

**Send** gives each write a time budget: 1 s, plus the bytes' time on the wire at the port's baud and framing for a serial port, or a flat 1 s for a TCP or RFC 2217 port.
A write that runs out of time fails with `write timed out after <n> of <m> bytes`, where at least `n` bytes went out, and the `tx` row records those `n` bytes.
When no byte went out, it fails with `write timed out; is flow control holding the line?`.

The Send action itself answers within 5 s, so a serial Send answers in time only while its bytes take at most 4.4 s on the wire.
A byte takes 1 start bit, the data bits, the parity bit if any and the stop bits: 10 bits with 8N1.
That is about 4,224 bytes at 9600 baud, or 50,688 bytes at 115200.
A longer Send returns `<port> did not answer within 4.5 s`, but the write still finishes and its `tx` row is logged.

**Get State** returns:

| Key | Value |
|---|---|
| `state` | `connecting`, `open`, `released`, `failed` or `stopped` |
| `health` | One sentence; see [Health](#health) |
| `where` | The path, `host:port` or `demo` |
| `clock` | `in use`, `not seen` or `host` |
| `counters` | `rx_bytes`, `tx_bytes`, `lines`, `prompts`, `echoes`, `reconnects`, `faults`, `errors`, `long_lines`, `signals`, `late_names`, `value_lines` |
| `signals` | One entry per signal: `event` (`<port>/<module>`, without the prefix), `field`, `unit`, `rule` (the format and habit, such as `zephyr+kv`), and `example` (the last message that carried it) |

`errors` counts unexpected errors on the port; `Serial/log` holds the first with its traceback, then one summary a minute with the last.

**Sample** takes `lines`, from 1 to 1000, default 50.
It returns `{"lines": [...]}`, the last lines received, prompts included.
With `hex`, it returns `{"chunks": [...]}`, the last raw reads as hex strings.

Send, Reset Device, Release Port and Acquire Port return `{"bytes_written": n}` or `{"ok": true}`.

### Errors

A failed action returns one sentence:

| Error | Meaning |
|---|---|
| `port is released` | Run Acquire Port first. |
| `port is still connecting` | The port is not open yet; Get State says why. |
| `port failed to open; see Get State` | An unsupported setting stopped the port. |
| `port is stopped` | The extension is stopping. |
| `port is not released` | Acquire Port needs a released port. |
| `command in progress` | Another Run Command on this port has not ended. |
| `this port has no reset line; set Reset line in Advanced` | Set **Reset line** for this serial port. |
| `a TCP port has no reset line`, `an RFC 2217 port has no reset line` | Reset Device works only on serial ports and the demo device. |
| `hex must be pairs of hex digits, such as 0d0a` | Fix the text of a hex Send. |
| `write timed out after <n> of <m> bytes`, `write timed out; is flow control holding the line?` | See Send above. |
| `disconnected from <where>` | The port failed during the write; the extension reconnects. |
| `<port> did not answer within <n> s` | The port's thread is busy, such as in a slow open or a long Send. |
| `no port is configured` | `port` was left out, and the config has no port. |
| Acquire Port: the port's health sentence | See [Flashing](#flashing-without-touching-the-app). |

Through the agent, the sentence arrives after the platform's own prefix, such as `Execution error: … ActionExecutionError: port is released`.
A failed action is not written to `Serial/log`; the caller already has the error.

## Flashing without touching the app

A flasher needs the port, and the extension holds it open.
Release the port before you flash and acquire it after:

```bash
zelos actions execute Serial/release --params '{"port": "dut"}'
idf.py -p /dev/ttyUSB0 flash
zelos actions execute Serial/acquire --params '{"port": "dut"}'
```

`dut` is the port's Name; with `--params '{}'` the actions act on the first configured port.
Acquire Port waits until the port opens, for up to 14 s, so a board that appears again after its flash is acquired.
It fails at once on a permission error or an unsupported setting.
After 14 s it fails with the port's health sentence, and the extension keeps trying to open the port.
It also fails with the health sentence before an open that would end past the 14 s, judged by how long the port's last open took, so the caller gets the reason.
One case can still exceed the wait: the first open of a network port whose host has 7 or more unreachable addresses, at 2 s each.

### Boot output after a flash

Bytes that arrive while the port is released are lost: no program reads them.
A USB-serial bridge that stays connected while the target resets, such as an ST-LINK or the bridge on many development boards, delivers the boot output the target prints right after the flash into that gap, so it is lost.
Measured on Linux with an ST-LINK V3: with Acquire Port started 0 to 47 ms after the flasher ended, little or none of the boot output arrived; at 47 ms, the last 3 of its 9 lines did.

To log the boot output, acquire the port first, then reset the target.
When the port has a **Reset line**, such as an ESP32 board set up as in [Board reset](#board-reset), run Reset Device after the flash:

```bash
./flash-dut idf.py -p /dev/ttyUSB0 flash && zelos actions execute Serial/reset --params '{"port": "dut"}'
```

Otherwise, run the flasher's own reset command after Acquire Port.
An ST-LINK resets the target through its debug connection, not the serial port, so this works while the extension holds the port:

```bash
st-flash --connect-under-reset reset
```

### PlatformIO

Add a script to the environment in `platformio.ini`:

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
extra_scripts = post:zelos_serial.py
```

`zelos_serial.py`, next to `platformio.ini`:

```python
import json
import subprocess

Import("env")

PARAMS = json.dumps({"port": "dut"})


def zelos(action, check=False):
    return subprocess.run(["zelos", "actions", "execute", action, "--params", PARAMS], check=check)


def release(source, target, env):
    zelos("Serial/release", check=True)


def acquire(source, target, env):
    if zelos("Serial/acquire").returncode:
        print("acquire failed; run Serial/get_state to see why")


env.AddPreAction("upload", release)
env.AddPostAction("upload", acquire)
```

When release fails, for example because the agent is not running, the upload stops before it touches the port.
PlatformIO runs the post action only when the upload succeeds.
After a failed upload, run acquire by hand.
To log the boot output that the upload's reset prints, see [Boot output after a flash](#boot-output-after-a-flash).

### Arduino CLI and idf.py

Save this as `flash-dut` and make it executable:

```bash
#!/usr/bin/env bash
set -euo pipefail
params='{"port": "dut"}'
zelos actions execute Serial/release --params "$params"
trap 'zelos actions execute Serial/acquire --params "$params" || echo "flash-dut: acquire failed; run Serial/get_state to see why" >&2' EXIT
"$@"
```

Put it in front of the flash command:

```bash
./flash-dut arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32 .
./flash-dut idf.py -p /dev/ttyUSB0 flash
```

It acquires the port again whether the flash succeeds or fails, and exits with the flash's status.
When acquire fails, it prints a warning and still exits with the flash's status.
When release fails, for example because the agent is not running, it stops before flashing and exits with release's status.
To log the boot output that the flash's reset prints, see [Boot output after a flash](#boot-output-after-a-flash).

## Board reset

**Reset Device** drives the **Reset line** away from its level at open for 100 ms, then back.
Set the lines to match the board's reset circuit:

| Board | DTR at open | RTS at open | Reset line |
|---|---|---|---|
| ESP32 board with an auto-reset circuit | `off` | `off` | `rts` |
| Arduino board with a DTR capacitor | `on` | `on` | `dtr` |

An ESP32 auto-reset circuit holds EN low while RTS is on and DTR is off, so the pulse on RTS resets the board.
An Arduino board resets on the edge where DTR turns back on.
The demo device resets on a DTR pulse.

These recipes are tested against simulated ports only, not yet on physical boards.

On Linux and macOS, the kernel raises DTR and RTS for a moment when a port opens, whatever the settings say.
Some boards therefore reset once when the extension connects, and again on Acquire Port.
On Windows, `off` asks the driver to keep the line deasserted from the open; whether a driver still pulses the lines is not yet confirmed on hardware.
Release Port and stopping the extension leave DTR and RTS as they are.
RFC 2217 and TCP ports have no reset line, and an RFC 2217 port keeps the DTR and RTS levels the server sets.

## Health

Get State returns one health sentence. The first match wins.

| Health | What to do |
|---|---|
| `Unsupported setting: <error>. Change it in the config.` | The driver refused a setting, such as the baud or parity. Change it in the config. The port does not retry. |
| `Permission denied on <where>. Add the agent's user to the dialout group (uucp on Arch): sudo usermod -aG dialout $USER, then log in again.` | Linux. Run the command as the user the agent runs as, then log out and in. |
| `Permission denied on <where>. Check the device's permissions.` | macOS. Let the agent's user open the device. |
| `<where> is in use by another program, or access was denied. Close the other program. If another Zelos agent holds it, run Release Port there.` | Close the other program. Two ports in this config that name one device also land here: the second cannot open it. |
| `No device matches <port>. Connected: <devices or none>.` | Plug the device in, or pick one of the listed IDs in **Port**. The port connects when the device appears. Once the device is listed again, health is `Connecting to <where>.` until the next open, within 0.5 s. |
| `Cannot resolve <host>. Check the host name.` | The host name did not resolve, or not within 2 s. |
| `<where> accepted the connection but does not answer RFC 2217. Is it a raw TCP port? Use tcp instead.` | Set **Connection** to `tcp`, or point it at an RFC 2217 server. |
| `Cannot connect to <where>. Check that the server is running.` | Start the TCP or RFC 2217 server, and check the host and port. |
| `Cannot open <where>: <error>.` | The open failed for another reason, which the error names. The port keeps trying. |
| `Released. Run Acquire Port to take the port back.` | Run Acquire Port. |
| `Disconnected from <where>. Waiting for it to come back.` | Plug the device back in, or restart the server. |
| `Connecting to <where>.` | Wait. |
| `Connected. No bytes yet. Check the baud rate and the wiring, and close any program that opened the port first.` | No byte has arrived in the 10 s since the port opened. Set **Baud** to the device's rate: an ST-LINK V3 bridge at a wrong baud delivers no bytes at all. Check the TX, RX and ground wires. On Linux and macOS, a program that opened the port before the extension can take every byte; close it. A device that prints only when asked stays silent until you run Send or Run Command. Once a byte arrives, this sentence does not return until the port opens again. |
| `Bytes arrive but no line ends. Check the baud rate, and run Sample with hex to see the raw bytes.` | Bytes have arrived for 10 s without a line end, a prompt or a 100 ms pause. Set **Baud** to the device's rate. At a wrong baud the noise often holds line ends too; then it is logged as lines of noise, and this sentence does not appear. |
| `No values found in <n> lines. Run Sample to see the lines.` | The lines match no value habit. Run Sample, or turn off **Turn printed values into signals**. |
| `Connected. <n> lines, <n> signals, device clock in use.` | Working. The ending is `device clock not seen` when no line carries an uptime, and `host clock` with **Time source** `host`. |

When the extension cannot list the devices to check whether a port is connected, `Serial/log` gets one warning: `<port>: cannot check whether <path> is connected (<error>); assuming it is.`

## Limits

- Binary protocols are not decoded.
- Device protocols, such as GPS NMEA sentences, are not decoded; they are logged as plain lines.
- RFC 2217 is tested with ser2net 4.3.4, 4.6.0 and 4.6.7.
- Opening an RFC 2217 port waits up to 2 s for each reply from the server. While a slow server answers, the port's actions wait too and can time out.
- There is no live console tab. The Log panel shows the lines, and Send and Run Command write to the port.
- The baud and the DTR and RTS lines cannot change while a port runs, and the extension cannot send a break.
- A port records at most 64 modules as signals, and a module warns about at most 32 late names; see [Late names](#what-it-records).
- A value name that first appears more than about 2 s after its module's first value needs a restart.
- Lines are decoded as UTF-8; invalid bytes are replaced.
- The exclusive open on Linux and macOS does not keep out a program that opened the port first, or one that runs as root. A program that opened the port first can take every byte.
- A headless agent older than 26.0.9 that listens on a port other than 2300 does not tell its extensions where it is, so Serial cannot reach it. Run that agent on port 2300, or update it.
- On Windows, the agent ends the extension without a final flush, so the last batch of rows may be lost.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the prerequisites and every `just` command.

```bash
just install                # dependencies
just check                  # format check, lint, pyright and tests
uv run pytest -m perf       # throughput with zero loss; Linux only
```

Run the extension against a local agent:

```bash
echo '{"ports": [{"connection": "demo"}]}' > config.json
just dev
```

The RFC 2217 tests in `tests/test_rfc2217.py` also run against a real ser2net.
ser2net serves one end of a pseudo-terminal pair as RFC 2217, and a writer prints `L<8 digits>` lines into the other end about every 5 ms.
`.github/workflows/rfc2217.yml` builds that setup. The tests skip when these are unset:

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

## Links

- **Repository**: [github.com/zeloscloud/zelos-extension-serial](https://github.com/zeloscloud/zelos-extension-serial)
- **Issues**: [github.com/zeloscloud/zelos-extension-serial/issues](https://github.com/zeloscloud/zelos-extension-serial/issues)
- **Extensions in Zelos**: [docs.zeloscloud.io/latest/app/extensions](https://docs.zeloscloud.io/latest/app/extensions/)

## License

MIT. See [LICENSE](LICENSE).
