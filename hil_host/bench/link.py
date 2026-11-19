"""Serial line link to the firmware: lines sent and received, and the exclusive port lock.

The link knows lines, not records: what a line means belongs to the protocol
(hil_host/protocol/), which uses this link.
- read_line() returns one received line without its terminator (LF, with one
  CR before it removed); a line still unterminated past max_pending bytes is
  returned as it is, so the protocol can reject it;
- send_line() sends a line with its terminator, send_raw() sends bytes as
  they are, and send_at_baud() sends them at another baud rate to cause a
  UART framing error, then restores the baud rate and drops the input.
Every line sent and received is logged at INFO on the hil.serial logger, as
text, or as escaped bytes when it holds a byte outside 0x20-0x7E; pytest
--log-cli-level=INFO shows them live in the terminal.
open() locks the port exclusively (flock), as miniterm does, so two programs
never share the received bytes; a busy port raises PortBusy naming the
program that holds it, from /proc/locks. The lock is advisory: a program that
does not take it can still open the port.

Version: 1.4
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  26/10/2026  Initial version.
  1.1  26/10/2026  Log TX and RX lines at INFO, non-printable bytes escaped.
  1.2  26/10/2026  Add raw bytes, other baud rates and reading records until silence.
  1.3  09/11/2026  Lock the port exclusively; a busy port names the program that holds it.
  1.4  19/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/adapters/serial_adapter.py, version 1.3: the line link and the port lock; the records go to hil_host/protocol/hil1.py.
"""

import errno
import logging
import os
import time
from pathlib import Path

from hil_host import LOGGER

log = logging.getLogger(f"{LOGGER}.serial")


def show(data: bytes) -> str:
    """Log form of a line: its text if printable ASCII, else the escaped bytes."""
    if all(0x20 <= byte <= 0x7E for byte in data):
        return data.decode("ascii")
    return repr(data)


class PortBusy(RuntimeError):
    """Another program holds the exclusive lock of the serial port: a bench error, not an outcome."""

    def __init__(self, port: str, holder: str | None):
        super().__init__(f"{port} is busy: held by {holder or 'an unknown program'}")
        self.port = port
        self.holder = holder


HOLDER_MAX_LEN = 100  # Characters of the command line shown in PortBusy


def port_holder(path: str, locks: str = "/proc/locks", proc: str = "/proc") -> str | None:
    """Command line and PID of the process holding a flock on path, or None if not found."""
    try:
        info = os.stat(path)
        table = Path(locks).read_text()
    except OSError:
        return None
    # /proc/locks names the file as major:minor:inode of its file system, major and minor in hex
    node = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    for line in table.splitlines():
        fields = line.split()
        # "1: FLOCK ADVISORY WRITE <pid> <node> 0 EOF"; "1: -> FLOCK ..." is a waiter
        if len(fields) < 6 or fields[1] != "FLOCK" or fields[5] != node:
            continue
        pid = fields[4]
        try:
            command = Path(proc, pid, "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            command = ""
        if len(command) > HOLDER_MAX_LEN:
            command = command[: HOLDER_MAX_LEN - 3] + "..."
        return f"{command} (PID {pid})" if command else f"PID {pid}"
    return None


class Link:
    """Line link to the firmware, on an open pyserial port."""

    def __init__(self, port, timeout: float, max_pending: int):
        self._port = port
        self.timeout = timeout  # Default read and write timeout of the port
        self.max_pending = max_pending  # A line still unterminated past this is returned as is
        self._pending = b""  # Bytes received after the last complete line

    @classmethod
    def open(cls, serial_config: dict, timeout: float, max_pending: int, port: str | None = None):
        """Open and lock the port described by the serial section of the bench file; PortBusy if held."""
        import serial  # pyserial; imported here so the parsers need no serial port

        name = port or serial_config["port"]
        parities = {"none": serial.PARITY_NONE, "even": serial.PARITY_EVEN, "odd": serial.PARITY_ODD}
        try:
            link = serial.serial_for_url(
                name,
                baudrate=serial_config["baudrate"],
                bytesize=serial_config["bytesize"],
                parity=parities[serial_config["parity"]],
                stopbits=serial_config["stopbits"],
                timeout=timeout,
                write_timeout=timeout,
                xonxoff=serial_config["flow_control"] == "xonxoff",
                rtscts=serial_config["flow_control"] == "rtscts",
                dsrdtr=False,
                exclusive=True,  # flock before any line change: a refused open leaves the holder alone
            )
        except serial.SerialException as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise PortBusy(name, port_holder(name)) from None
            raise
        return cls(link, timeout, max_pending)

    def close(self) -> None:
        self._port.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def flush_input(self) -> None:
        """Drop every received byte not read yet."""
        self._port.reset_input_buffer()
        self._pending = b""

    def send_line(self, line: str, eol: bytes = b"\r\n") -> None:
        data = line.encode("ascii") + eol
        log.info("TX  %s", line if eol == b"\r\n" else repr(data))
        self._write(data)

    def send_raw(self, data: bytes) -> None:
        """Send bytes as they are, terminator included: lines a conforming host never sends."""
        log.info("TX  %r", data)
        self._write(data)

    def send_at_baud(self, data: bytes, baudrate: int, settle: float) -> None:
        """Send data at another baud rate, then restore the link's and drop input (UART framing error)."""
        normal = self._port.baudrate
        time.sleep(settle)  # Let the bridge send what is queued at the normal rate
        log.info("TX  %r at %d baud", data, baudrate)
        self._port.baudrate = baudrate
        self._write(data)
        time.sleep(settle)  # flush() returns once the USB bridge holds the data, not once it is on the wire
        self._port.baudrate = normal
        time.sleep(settle)  # Let the bridge apply the restored baud rate
        self.flush_input()  # Drop switch glitches; the firmware reports errors at the next terminator

    def _write(self, data: bytes) -> None:
        self._port.write(data)
        self._port.flush()

    def read_line(self, deadline: float) -> str | None:
        """Return the next received line, or None if none ends before deadline (time.monotonic())."""
        while True:
            end = self._pending.find(b"\n")
            if end >= 0 or len(self._pending) > self.max_pending:
                if end < 0:
                    end = len(self._pending)  # Never terminated: let the caller reject it
                raw, self._pending = self._pending[:end], self._pending[end + 1 :]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                log.info("RX  %s", show(raw))
                return raw.decode("latin-1")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._port.timeout = remaining
            self._pending += self._port.read_until(b"\n")
