"""A device that prints numbered lines; run apart from the app so the two never share a GIL.

    writer.py pty FD RATE SIZE SECONDS OUT   RATE lines/s of SIZE bytes to the pty controller FD
    writer.py tcp SIZE SECONDS OUT           one byte per send(), as fast as it goes, to the
                                             one client of a port it prints on stdout

OUT gets each line's send time in ns, by sequence number, as a native int64 array. The TCP
connection then stays open until stdin closes, so the app reads every byte before it ends.
"""

import os
import socket
import sys
import time
from array import array
from pathlib import Path

BANNER = b"perf start\n"


def numbered(seq: int, size: int) -> bytes:
    """A line of `size` bytes, LF included, that carries `seq`."""
    return f"seq={seq} ".ljust(size - 1, "x").encode() + b"\n"


def save(sent: array, out: str) -> None:
    """Write `out` in one step, so a reader never sees half of it."""
    part = Path(f"{out}.part")
    part.write_bytes(sent.tobytes())
    part.replace(out)


def paced(fd: int, rate: int, size: int, seconds: float, out: str) -> None:
    sent = array("q")

    def write(data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]

    write(BANNER)
    start = time.monotonic()
    while (elapsed := time.monotonic() - start) < seconds:
        due = min(int(elapsed * rate), int(seconds * rate))
        if due == len(sent):
            time.sleep(0.001)
            continue
        lines = b"".join(numbered(seq, size) for seq in range(len(sent), due))
        sent.extend([time.time_ns()] * (due - len(sent)))
        write(lines)
    save(sent, out)


def bytewise(size: int, seconds: float, out: str) -> None:
    sent = array("q")
    with socket.create_server(("127.0.0.1", 0)) as listener:
        print(listener.getsockname()[1], flush=True)
        listener.settimeout(10)
        peer, _ = listener.accept()
    with peer:
        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        send = peer.send
        for i in range(len(BANNER)):
            send(BANNER[i : i + 1])
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            line = numbered(len(sent), size)
            for i in range(len(line)):
                send(line[i : i + 1])
            sent.append(time.time_ns())
        save(sent, out)
        sys.stdin.read()


def main(argv: list[str]) -> None:
    if argv[0] == "pty":
        fd, rate, size, seconds, out = argv[1:]
        paced(int(fd), int(rate), int(size), float(seconds), out)
    else:
        size, seconds, out = argv[1:]
        bytewise(int(size), float(seconds), out)


if __name__ == "__main__":
    main(sys.argv[1:])
