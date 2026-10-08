"""The extension's life cycle: one PortWorker per configured port until SIGTERM or SIGINT.

Given a `source`, run() writes there and never connects to the agent; given a `stop` event, it
installs no signal handlers.
"""

import dataclasses
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable

import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from . import ACTION_PREFIX, actions, config
from .config import ConfigError, PortConfig, Settings
from .emitter import Emitter
from .naming import unique_name
from .transport import Transport, open_transport
from .worker import PortWorker

logger = logging.getLogger(__name__)


def run(
    source: zelos_sdk.TraceSource | None = None,
    *,
    settings: Settings | None = None,
    open: Callable[[PortConfig], Transport] = open_transport,
    stop: threading.Event | None = None,
) -> None:
    """Run the extension until `stop` is set, or until SIGTERM or SIGINT."""
    if settings is None:
        try:
            settings = config.load()
        except ConfigError as e:
            # The supervisor does not restart the extension: these lines are the user's feedback.
            for problem in e.problems:
                logger.error("%s", problem)
            sys.exit(1)
    if source is None:
        # Name the global source before init() does; init_global_source is idempotent.
        source = zelos_sdk.init_global_source(settings.prefix)
        zelos_sdk.init(name=ACTION_PREFIX, actions=True)
    if stop is None:
        stop = _stop_on_signals()

    handler = TraceLoggingHandler(source)
    handler.setLevel(settings.log_level)
    # The SDK logs every failed action, which the caller already gets as the action's error.
    handler.addFilter(lambda record: not record.name.startswith("zelos_sdk.actions"))
    root = logging.getLogger()
    root.addHandler(handler)
    logging.getLogger(__package__).setLevel(settings.log_level)

    emitter = Emitter(source)
    actions.workers.clear()
    try:
        for cfg in _with_default_names(settings.ports):
            emitter.add_port(cfg.name)
            worker = PortWorker(cfg, emitter, settings, open=open)
            worker.start()
            actions.workers[cfg.name] = worker
        # A timed wait: an untimed Event.wait() may not wake for Ctrl-C on Windows.
        while not stop.wait(0.5):
            pass
    finally:
        workers = list(actions.workers.values())
        for worker in workers:
            worker.stop()
        # One deadline for all: the agent's grace period also covers the flush.
        deadline = time.monotonic() + 1.0
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        source.flush()
        root.removeHandler(handler)


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    return stop


def _with_default_names(ports: tuple[PortConfig, ...]) -> list[PortConfig]:
    used = {cfg.name for cfg in ports if cfg.name}
    named: list[PortConfig] = []
    for cfg in ports:
        if not cfg.name:
            cfg = dataclasses.replace(cfg, name=unique_name(_default_name(cfg), used))
        named.append(cfg)
    return named


def _default_name(cfg: PortConfig) -> str:
    # The config alone: which devices are plugged in at start must not change where data lands.
    if cfg.port is not None:
        return cfg.port
    if cfg.host is not None:
        return f"{cfg.host}:{cfg.tcp_port}"
    return "demo"
