"""Three device consoles, byte for byte: a Zephyr shell, an ESP-IDF boot and a Linux boot."""

from tests.integration.harness import Console, Printed, Row

_PROMPT = b"\x1b[1;32muart:~$ \x1b[m"
# Zephyr's shell erases the prompt before each log line, then prints the prompt again.
_ERASE = b"\x1b[8D\x1b[J"
_YELLOW, _RED, _RESET = "\x1b[1;33m", "\x1b[1;31m", "\x1b[0m"


def _zephyr(ms: int, tag: str, module: str, message: str, colour: str, level: str) -> Printed:
    stamp = f"[00:00:{ms // 1000:02d}.{ms % 1000:03d},000]"
    text = f"{stamp} {colour}<{tag}> {module}: {message}{_RESET}\r\n"
    # Played at 1.5 times device time, so host and device spacing differ.
    return Printed(
        0.05 + 1.5 * ms / 1000,
        _ERASE + text.encode() + _PROMPT,
        Row(level, module, f"{module}: {message}"),
    )


def status(i: int) -> str:
    """The `i`th status line the Zephyr console prints, every 50 ms of device time."""
    return f"rail={13.80 - i / 100:.2f}V in=4.30A limit=4.5A temp={41.0 + i / 10:.1f}C"


STATUS_LINES = 8
_ZEPHYR_LOG = sorted(
    [_zephyr(50 * i, "inf", "dcdc", status(i), _RESET, "info") for i in range(STATUS_LINES)]
    + [
        _zephyr(175, "wrn", "bms", "cell imbalance 42mV", _YELLOW, "warn"),
        _zephyr(275, "err", "i2c", "transfer failed", _RED, "error"),
    ],
    key=lambda p: p.at_s,
)
BANNER = "*** Booting Zephyr OS build v4.1.0 ***"
ZEPHYR = Console(
    [
        Printed(0.0, b"\x1b[m" + _PROMPT),
        Printed(0.01, _ERASE + f"{BANNER}\r\n".encode() + _PROMPT, Row("info", "", BANNER)),
        *_ZEPHYR_LOG,
    ],
    prompts=2 + len(_ZEPHYR_LOG),
)


def _lines(rows: list[tuple[bytes, Row | None]], gap_s: float) -> list[Printed]:
    """One print every `gap_s`, so each is its own read."""
    return [Printed(i * gap_s, data, row) for i, (data, row) in enumerate(rows)]


def _esp(level: str, ms: int, tag: str, message: str) -> tuple[bytes, Row]:
    letter, colour = {"info": ("I", "32"), "warn": ("W", "33"), "error": ("E", "31")}[level]
    data = f"\x1b[0;{colour}m{letter} ({ms}) {tag}: {message}\x1b[0m\n".encode()
    return data, Row(level, tag, f"{tag}: {message}")


def _plain(text: str, ending: str) -> tuple[bytes, Row | None]:
    return f"{text}{ending}".encode(), Row("info", "", text) if text else None


_SIZE_WARNING = (
    "Detected size(4096k) larger than the size in the binary image header(2048k). "
    "Using the size in the binary image header."
)
ESP_IDF = Console(
    _lines(
        [
            *(
                _plain(text, "\n")
                for text in (
                    "ets Jun  8 2016 00:22:57",
                    "",
                    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
                    "configsip: 0, SPIWP:0xee",
                    "clk_drv:0x00,q_drv:0x00,d_drv:0x00,cs0_drv:0x00,hd_drv:0x00,wp_drv:0x00",
                    "mode:DIO, clock div:2",
                    "load:0x3fff0030,len:7104",
                    "entry 0x400805f0",
                )
            ),
            _esp("info", 29, "boot", "ESP-IDF v5.1.2 2nd stage bootloader"),
            _esp("info", 56, "boot", "chip revision: v3.0"),
            _esp("warn", 310, "spi_flash", _SIZE_WARNING),
            _esp("error", 512, "wifi", "sta connect failed, reason 201"),
            _esp("info", 600, "app", "temp=24.5C vbat=3.71V"),
            _esp("info", 700, "app", "temp=24.6C vbat=3.70V"),
        ],
        gap_s=0.01,
    ),
    prompts=0,
)


def _kernel(seconds: float, message: str) -> tuple[bytes, Row]:
    return f"[{seconds:12.6f}] {message}\r\n".encode(), Row("info", "kernel", message)


_LINUX_VERSION = (
    "Linux version 6.1.55 (builder@buildhost) (arm-buildroot-linux-gnueabihf-gcc 12.3.0) "
    "#1 SMP PREEMPT"
)
LINUX = Console(
    _lines(
        [
            _plain("Uncompressing Linux... done, booting the kernel.", "\r\n"),
            _kernel(0.0, "Booting Linux on physical CPU 0x0"),
            _kernel(0.0, _LINUX_VERSION),
            _kernel(0.12, "Memory: 499312K/524288K available"),
            _kernel(0.35, "Run /sbin/init as init process"),
            _plain("Starting syslogd: OK", "\r\n"),
            _plain("Starting network: OK", "\r\n"),
            _plain("cpu_temp=48.2C load=0.31", "\r\n"),
            # Printed late: its stamp maps behind the three lines before it.
            _kernel(0.41, "random: crng init done"),
            _plain("cpu_temp=48.4C load=0.28", "\r\n"),
            _plain("", "\r\n"),
            _plain("Welcome to Buildroot", "\r\n"),
            (b"buildroot login: ", None),
        ],
        gap_s=0.1,
    ),
    prompts=1,
)
