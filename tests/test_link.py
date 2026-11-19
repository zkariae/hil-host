"""Tests of hil_host/bench/link.py on a pty, without a board.

The link opens a real pty with pyserial, as it opens the board's port: the
lines written on the master side are what the firmware would send, the bytes
read there are what the firmware would receive. The tests check line reading
(CRLF or LF alone, partial lines, an unterminated line past max_pending, the
deadline, flush_input), sending (send_line, send_raw, send_at_baud), the
TX and RX lines of the hil.serial logger, the exclusive port lock with its
holder named, and port_holder on fake /proc trees. They read /proc and use
ptys, so they run on Linux only.

Not tested here: the record syntax, the transactions and the request IDs
(tests/test_protocol_hil1.py), and a real USB serial port.

Version: 1.1
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  27/10/2026  Initial version.
  1.1  19/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/unit/test_serial_adapter.py 1.0 (line tests) and tests/host/tui/tests/test_session.py 1.0 (port lock tests); run on a pty instead of a fake port.
"""

import logging
import os
import subprocess
import sys
import time

import pytest

from hil_host.bench.link import HOLDER_MAX_LEN, Link, PortBusy, port_holder, show

LINUX = sys.platform.startswith("linux")
pytestmark = pytest.mark.skipif(not LINUX, reason="ptys and /proc/locks")

SERIAL = {"port": "unused", "baudrate": 115200, "bytesize": 8, "parity": "none", "stopbits": 1,
          "flow_control": "none"}
TIMEOUT = 0.5
MAX_PENDING = 512
SETTLE = 0.05  # Seconds for bytes written on one side of the pty to reach the other


def soon(seconds: float = 1.0) -> float:
    return time.monotonic() + seconds


def received(master: int) -> bytes:
    """Bytes the link has sent, as the firmware would read them."""
    time.sleep(SETTLE)
    return os.read(master, 4096)


@pytest.fixture
def pty_path():
    master, slave = os.openpty()
    yield master, os.ttyname(slave)
    os.close(slave)
    os.close(master)


@pytest.fixture
def link(pty_path):
    """(master fd, open Link): pyserial puts the pty in raw mode, as a real port."""
    master, path = pty_path
    with Link.open(SERIAL, TIMEOUT, MAX_PENDING, path) as opened:
        yield master, opened


# Reading

def test_read_line_crlf_and_lf_alone(link):
    master, opened = link
    os.write(master, b"ONE\r\nTWO\n")
    assert opened.read_line(soon()) == "ONE"
    assert opened.read_line(soon()) == "TWO"


def test_read_line_partial_then_complete(link):
    master, opened = link
    os.write(master, b"REA")
    assert opened.read_line(soon(0.1)) is None
    os.write(master, b"DY x=1\r\n")
    assert opened.read_line(soon()) == "READY x=1"


def test_read_line_unterminated_past_max_pending(link):
    master, opened = link
    os.write(master, b"X" * (MAX_PENDING + 88))
    assert opened.read_line(soon()) == "X" * (MAX_PENDING + 88)  # Returned as is: the protocol rejects it


def test_read_line_none_at_deadline(link):
    _, opened = link
    started = time.monotonic()
    assert opened.read_line(started + 0.2) is None
    assert time.monotonic() - started < 0.2 + TIMEOUT


def test_flush_input_drops_received_bytes(link):
    master, opened = link
    os.write(master, b"stale line\r\nhalf")
    time.sleep(SETTLE)
    opened.flush_input()
    os.write(master, b"FRESH\r\n")
    assert opened.read_line(soon()) == "FRESH"


# Sending

def test_send_line_crlf_by_default_or_lf_alone(link):
    master, opened = link
    opened.send_line("CMD id=1")
    opened.send_line("CMD id=2", eol=b"\n")
    assert received(master) == b"CMD id=1\r\nCMD id=2\n"


def test_send_raw_writes_bytes_unchanged(link):
    master, opened = link
    opened.send_raw(b"CMD id=1 command=HE\x7fLLO\r\n")
    assert received(master) == b"CMD id=1 command=HE\x7fLLO\r\n"


def test_send_at_baud_restores_the_baud_rate(link):
    master, opened = link
    os.write(master, b"glitch")
    opened.send_at_baud(b"\x00", 9600, SETTLE)
    assert received(master) == b"\x00"
    assert opened._port.baudrate == SERIAL["baudrate"]
    assert opened.read_line(soon(0.1)) is None  # The input was dropped


# Log

def test_show():
    assert show(b"RESULT id=1 status=OK") == "RESULT id=1 status=OK"
    assert show(b"A\x7fB") == "b'A\\x7fB'"


def test_lines_logged_on_hil_serial(link, caplog):
    master, opened = link
    caplog.set_level(logging.INFO, logger="hil.serial")
    opened.send_line("CMD id=1")
    opened.send_line("CMD id=2", eol=b"\n")
    opened.send_raw(b"\x01\r\n")
    os.write(master, b"RX line\r\nC\x01\n")
    opened.read_line(soon())
    opened.read_line(soon())
    assert [(record.name, record.getMessage()) for record in caplog.records] == [
        ("hil.serial", "TX  CMD id=1"),
        ("hil.serial", "TX  b'CMD id=2\\n'"),
        ("hil.serial", "TX  b'\\x01\\r\\n'"),
        ("hil.serial", "RX  RX line"),
        ("hil.serial", "RX  b'C\\x01'"),
    ]


# Exclusive lock on a real pty

@pytest.fixture
def held_pty(pty_path):
    """A pty locked by a child process, as miniterm would hold the port."""
    _, path = pty_path
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import serial, sys, time; s = serial.Serial(sys.argv[1], exclusive=True); print('held', flush=True); "
         "time.sleep(30)", path],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        yield path, child.pid
    finally:
        child.kill()
        child.wait()


def _open(path):
    return Link.open(SERIAL, TIMEOUT, MAX_PENDING, path)


def test_second_open_in_this_process(pty_path):
    _, path = pty_path
    with _open(path):
        with pytest.raises(PortBusy) as error:
            _open(path)
    assert error.value.port == path
    assert error.value.holder.endswith(f"(PID {os.getpid()})")
    with _open(path):  # The lock went with the close
        pass


def test_port_held_by_another_process(held_pty):
    path, pid = held_pty
    with pytest.raises(PortBusy) as error:
        _open(path)
    assert error.value.holder.startswith(sys.executable)
    assert error.value.holder.endswith(f"(PID {pid})")
    assert str(error.value) == f"{path} is busy: held by {error.value.holder}"


def test_unlocked_port_has_no_holder(pty_path):
    _, path = pty_path
    assert port_holder(path) is None


# port_holder on fake /proc trees

@pytest.fixture
def proc_tree(tmp_path):
    """A locked file, its /proc/locks node, and an empty /proc."""
    target = tmp_path / "ttyFAKE"
    target.write_text("")
    info = os.stat(target)
    node = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    proc = tmp_path / "proc"
    proc.mkdir()
    return target, node, proc


def _holder(tree, lines, cmdlines=None):
    target, node, proc = tree
    locks = proc / "locks"
    locks.write_text("\n".join(line.format(node=node) for line in lines) + "\n")
    for pid, cmdline in (cmdlines or {}).items():
        (proc / pid).mkdir()
        (proc / pid / "cmdline").write_bytes(cmdline)
    return port_holder(str(target), str(locks), str(proc))


def test_holder_found(proc_tree):
    lines = ["1: FLOCK  ADVISORY  WRITE 111 00:00:1 0 EOF",  # Another file
             "2: POSIX  ADVISORY  WRITE 222 {node} 0 EOF",  # Not a flock
             "3: -> FLOCK  ADVISORY  WRITE 444 {node} 0 EOF",  # A waiter
             "3: FLOCK  ADVISORY  WRITE 333 {node} 0 EOF"]
    holder = _holder(proc_tree, lines, {"333": b"python3\0-m\0serial.tools.miniterm\0"})
    assert holder == "python3 -m serial.tools.miniterm (PID 333)"


def test_holder_long_command_is_cut(proc_tree):
    holder = _holder(proc_tree, ["1: FLOCK  ADVISORY  WRITE 333 {node} 0 EOF"], {"333": b"x" * 300})
    command = holder.removesuffix(" (PID 333)")
    assert len(command) == HOLDER_MAX_LEN and command.endswith("...")


def test_holder_without_cmdline(proc_tree):
    assert _holder(proc_tree, ["1: FLOCK  ADVISORY  WRITE 333 {node} 0 EOF"]) == "PID 333"


def test_holder_not_found(proc_tree):
    assert _holder(proc_tree, ["1: FLOCK  ADVISORY  WRITE 111 00:00:1 0 EOF"]) is None


def test_holder_without_locks_file(tmp_path):
    assert port_holder(str(tmp_path / "ttyFAKE"), str(tmp_path / "locks")) is None
