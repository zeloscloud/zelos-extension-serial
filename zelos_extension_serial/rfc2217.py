"""serialx 1.11's RFC 2217 client with two fixes."""

import time
from collections.abc import Callable
from typing import TypeVar

from serialx import SerialException
from serialx.platforms.serial_rfc2217 import RFC2217Serial
from serialx.platforms.serial_rfc2217.types import (
    Rfc2217Command,
    SetLinestateMaskCmd,
    SetModemstateMaskCmd,
    TelnetCommand,
)

_T = TypeVar("_T")


class NoRfc2217(Exception):
    """The server accepted the connection but does not negotiate RFC 2217."""


class FixedRFC2217Serial(RFC2217Serial):
    """serialx's sync RFC 2217 client, fixed to work with ser2net 4.3.4 to 4.6.7."""

    # A raw TCP console or a plain telnet server accepts the connection, then never agrees to
    # RFC 2217: serialx raises SerialException on a refusal, and the reply wait times out otherwise.
    def _negotiate(self) -> None:
        try:
            super()._negotiate()
        except (SerialException, TimeoutError) as e:
            raise NoRfc2217("the server does not answer RFC 2217") from e

    # The handshake's WILL/DO waits restart serialx's timer too while ser2net 4.3.4 and 4.6.0
    # stream device data. `timeout` is unused: serialx only ever passes the connect timeout.
    def _send_command(
        self,
        cmd: TelnetCommand | Rfc2217Command,
        responses: list[TelnetCommand] | None = None,
        timeout: float | None = None,
    ) -> TelnetCommand | None:
        super()._send_command(cmd)
        if responses is None:
            return None
        return self._await_reply(lambda: self._engine.pop_matching_telnet(responses))

    def _send_and_wait(self, cmd: Rfc2217Command) -> Rfc2217Command:
        self._send_command(cmd)
        # ser2net 4.3.4 and 4.6.0 never ack SET-MODEMSTATE-MASK or SET-LINESTATE-MASK. The masks
        # only filter notifications, which those servers send anyway.
        if isinstance(cmd, SetModemstateMaskCmd | SetLinestateMaskCmd):
            return cmd
        return self._await_reply(lambda: self._engine.pop_pending_rfc2217(cmd.CMD_ID))

    def _await_reply(self, pop: Callable[[], _T | None]) -> _T:
        """Process server bytes until `pop` finds the reply, within one connect timeout."""
        # serialx times each recv, so a device that keeps printing restarts the timer for ever
        # while the server never replies.
        assert self._connect_timeout is not None
        deadline = time.monotonic() + self._connect_timeout
        while (reply := pop()) is None:
            remaining = deadline - time.monotonic()
            # Checked first: a socket timeout of 0 means non-blocking, not expired.
            if remaining <= 0:
                raise TimeoutError("the RFC 2217 server did not reply")
            with self._socket_timeout(remaining):
                self._recv_and_process()
        return reply
