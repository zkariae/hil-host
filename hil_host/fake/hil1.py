"""Fake serial port, fake firmware and fake programmer of protocol hil-1, for tests without a board.

FakePort has the pyserial methods the line link uses (hil_host/bench/link.py).
Each write() is kept, with the baud rate it was sent at, and handed without
its terminator to a responder: a function that plays the firmware and
returns the bytes of its answer, which read_until() then gives back.

FakeFirmware.respond is the responder of a conforming firmware:
  - HELLO answers its name, protocol, firmware version and build;
  - CAPS lists its commands one page at a time: whole names while the line,
    next included, stays within 128 bytes, at most 8 names and at least one;
  - a command it lists answers status=OK with its arguments echoed, or
    status=ERR code=INVALID_ARGUMENT when one value is "bad";
  - a command it does not list answers status=ERR code=UNKNOWN_COMMAND;
  - silent=True makes it answer nothing, as a firmware that hangs.
A test passes its own responder to FakePort to make the firmware misbehave.
FakeProgrammer resets this firmware: reset() and reset_sw() queue its
banner (reset=PIN or reset=SW) in the port, as the board sends it after a
reset; a failure set beforehand is raised by the next reset instead.

Version: 1.1
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  27/10/2026  Initial version.
  1.1  19/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/unit/hil_fake.py 1.0, with the paged CAPS, echo and silent firmware of tests/host/tui/tests/test_ui_commands.py 1.2.
"""

import time

READ_WAIT_MAX = 0.05  # Seconds read_until() waits, like a timeout, when no line is complete
LINE_MAX_LEN = 128  # Bytes of a line without its terminator
CAPS_PAGE = 8  # Names per CAPS page at most; the line length decides first
PIN_FLAGS = 0x04000000  # Reset flags of a reset by the reset pin
SW_FLAGS = 0x14000000  # Software reset: the pin flag is set too


def banner(name: str = "fake-bench", fw: str = "1.0.0", proto: str = "1.0", build: str = "abc1234",
           reset: str = "PIN", flags: int = PIN_FLAGS) -> str:
    """A valid READY line, without its terminator."""
    return f"READY name={name} fw={fw} proto={proto} build={build} reset={reset} reset_flags=0x{flags:08X}"


class FakePort:
    """pyserial-like port: write() hands each line to a responder, which queues reply bytes."""

    def __init__(self, responder=None):
        self.responder = responder or (lambda line: b"")
        self.incoming = bytearray()  # Bytes the firmware sent, not read yet
        self.written = []  # Each write, decoded, without its terminator
        self.writes = []  # (baudrate, bytes) of each write
        self.timeout = 1.0
        self.baudrate = 115200
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append((self.baudrate, bytes(data)))
        line = bytes(data).decode("latin-1").rstrip("\r\n")
        self.written.append(line)
        self.incoming += self.responder(line)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self.incoming.clear()

    def read_until(self, expected: bytes = b"\n") -> bytes:
        end = self.incoming.find(expected)
        if end < 0:
            time.sleep(min(self.timeout or 0, READ_WAIT_MAX))  # Nothing complete yet: wait like a timeout
            data, self.incoming[:] = bytes(self.incoming), b""
            return data
        data = bytes(self.incoming[: end + 1])
        del self.incoming[: end + 1]
        return data

    def close(self) -> None:
        self.closed = True


class FakeFirmware:
    """A conforming firmware: HELLO, paged CAPS, echo of the listed commands."""

    def __init__(self, name: str = "fake-bench", fw: str = "1.0.0", proto: str = "1.0", build: str = "abc1234",
                 caps: list | None = None):
        self.name, self.fw, self.proto, self.build = name, fw, proto, build
        self.caps = list(caps or ["HELLO", "CAPS"])
        self.silent = False  # The firmware stops answering

    def banner(self, reset: str = "PIN", flags: int = PIN_FLAGS) -> str:
        return banner(self.name, self.fw, self.proto, self.build, reset, flags)

    def respond(self, line: str) -> bytes:
        if self.silent:
            return b""
        try:
            tokens = dict(token.split("=", 1) for token in line.split()[1:])
            request_id, command = tokens.pop("id"), tokens.pop("command")
        except (ValueError, KeyError):
            return b""  # Not a request this fake understands: a test of the link, not of the firmware
        if command == "HELLO":
            body = f"status=OK name={self.name} proto={self.proto} fw={self.fw} build={self.build}"
        elif command == "CAPS":
            body = self.caps_page(request_id, int(tokens.get("start", "0")))
        elif command not in self.caps:
            body = "status=ERR code=UNKNOWN_COMMAND"
        elif "bad" in tokens.values():
            body = "status=ERR code=INVALID_ARGUMENT"
        else:
            body = " ".join(["status=OK"] + [f"{key}={value}" for key, value in tokens.items()])
        return f"RESULT id={request_id} {body}\r\nEND id={request_id}\r\n".encode()

    def caps_page(self, request_id: str, start: int) -> str:
        """Whole names while the line, next included, stays within LINE_MAX_LEN."""
        total = len(self.caps)
        head = f"RESULT id={request_id} status=OK total={total} start={start} names="
        end = start + 1  # One name at least
        while end < total and end - start < CAPS_PAGE:
            tail = f" next={end + 1}" if end + 1 < total else ""
            if len(head + ",".join(self.caps[start:end + 1]) + tail) > LINE_MAX_LEN:
                break
            end += 1
        body = f"status=OK total={total} start={start} names={','.join(self.caps[start:end])}"
        return body + (f" next={end}" if end < total else "")


class FakeProgrammer:
    """Resets the fake firmware: its banner arrives in the port, as after a board reset."""

    def __init__(self, port: FakePort, firmware: FakeFirmware):
        self.port = port
        self.firmware = firmware
        self.resets = 0
        self.flashes = 0
        self.failure = None  # Exception raised by the next reset instead of the banner

    def flash(self) -> None:
        self.flashes += 1

    def reset(self) -> None:
        self._reset("PIN", PIN_FLAGS)

    def reset_sw(self) -> None:
        self._reset("SW", SW_FLAGS)

    def build_id(self) -> str | None:
        return self.firmware.build

    def _reset(self, cause: str, flags: int) -> None:
        self.resets += 1
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        self.port.incoming += (self.firmware.banner(cause, flags) + "\r\n").encode()
