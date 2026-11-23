"""Tests of hil_host/bench/board.py on the fake firmware and on a toy protocol, without a board.

The board session talks to the protocol interface alone. Most tests run it
with hil-1 on the fake firmware, as on a bench; the last ones with a toy
protocol that has no serial link, so nothing of hil-1 can be needed:
  - no request without a session;
  - the session: readiness signal, arguments of a request, failed reset,
    missing readiness signal and recovery;
  - a session the protocol starts by itself, after the last request ID of
    hil-1: the banner follows it, and a reset replaced for a time is not
    used again;
  - the build check: same, different, accepted, not checked;
  - the command list: read once per session, and a missing command.

Not tested here: the rules of hil-1 (tests/test_protocol_hil1.py), the
choice of the protocol by the configuration (tests/test_config.py) and a
real board.

Version: 1.0
Date: 23/11/2026
Author: Z. BEN-AKKA
History:
  1.0  23/11/2026  Initial version.
"""

import logging
from types import SimpleNamespace

import pytest

from hil_host.bench.board import Board
from hil_host.bench.link import Link
from hil_host.bench.outcomes import BuildMismatch, ProgrammingFailure, StartupFailure, UnsupportedCommand
from hil_host.fake.hil1 import FakeFirmware, FakePort, FakeProgrammer
from hil_host.protocol import BenchProtocol, Identity, Response
from hil_host.protocol.hil1 import ID_MAX, Hil1

NAMES = ["HELLO", "CAPS", "PING"]
BUILD = "abc1234"  # Build of the fake firmware and of the toy protocol
OTHER_BUILD = "fffffff"
STARTUP = 0.3  # Seconds; the fake answers at once
TIMEOUT = 0.3


@pytest.fixture
def bench():
    """Board on the fake firmware, without a session; sent holds every line it sends."""
    firmware = FakeFirmware(build=BUILD, caps=NAMES)
    sent = []

    def respond(line: str) -> bytes:
        sent.append(line)
        return firmware.respond(line)

    port = FakePort(respond)
    programmer = FakeProgrammer(port, firmware)
    protocol = Hil1(TIMEOUT)
    protocol.open(Link(port, TIMEOUT, Hil1.max_pending))
    return SimpleNamespace(board=Board(protocol, programmer, STARTUP, 0.0), programmer=programmer, sent=sent)


@pytest.fixture
def session(bench):
    """The same bench with a live session, and no line sent yet."""
    bench.board.start_session()
    bench.sent.clear()
    return bench


def warnings(caplog) -> list:
    return [record.getMessage() for record in caplog.records
            if record.name == "hil.board" and record.levelno == logging.WARNING]


# Without a session

@pytest.mark.parametrize("call", [
    lambda board: board.request("PING"),
    lambda board: board.identify(),
    lambda board: board.capabilities(),
    lambda board: board.require("PING"),
], ids=["request", "identify", "capabilities", "require"])
def test_needs_a_session(bench, call):
    with pytest.raises(RuntimeError, match=r"call start_session\(\) first"):  # Not the error of the protocol
        call(bench.board)
    assert bench.sent == []


# Session

def test_start_session_gives_the_banner(bench):
    assert bench.board.banner is None
    banner = bench.board.start_session()
    assert bench.board.banner is banner
    assert (banner.name, banner.build, banner.reset) == ("fake-bench", BUILD, "PIN")
    assert bench.programmer.resets == 1


def test_request_passes_the_arguments(session):
    response = session.board.request("PING", level=3, mode="slow")
    assert response.ok
    assert response.fields == {"level": "3", "mode": "slow"}


def test_failed_reset_leaves_no_session(session):
    session.programmer.failure = ProgrammingFailure("no probe")
    with pytest.raises(ProgrammingFailure, match="no probe"):
        session.board.start_session()
    assert session.board.banner is None


def test_missing_banner_then_recovery(session, monkeypatch, caplog):
    with monkeypatch.context() as patch:
        patch.setattr(session.programmer, "reset", lambda: None)
        with pytest.raises(StartupFailure):
            session.board.start_session()
    assert session.board.banner is None
    banner = session.board.recover()
    assert session.board.banner is banner
    assert any(text.startswith("recovery") for text in warnings(caplog))
    assert session.board.request("PING").ok


# A session the protocol starts by itself

def test_banner_follows_the_new_session_after_the_last_id(session):
    first = session.board.banner
    session.board.adapter.next_id = ID_MAX
    assert session.board.request("PING").id == ID_MAX
    assert session.board.banner is first
    session.board.request("PING")
    assert session.board.banner is not None
    assert session.board.banner is not first
    assert session.programmer.resets == 2


def test_failed_restart_leaves_no_session(session):
    session.board.adapter.next_id = ID_MAX + 1
    session.programmer.failure = ProgrammingFailure("no probe")
    with pytest.raises(ProgrammingFailure):
        session.board.request("PING")
    assert session.board.banner is None


def test_replaced_reset_is_not_used_again(bench, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(bench.programmer, "reset", bench.programmer.reset_sw)
        assert bench.board.start_session().reset == "SW"
    bench.board.adapter.next_id = ID_MAX + 1
    bench.board.request("PING")
    assert bench.board.banner.reset == "PIN"


# Build check

def test_expected_build(session, caplog):
    session.board.expected_build = BUILD
    assert session.board.identify().ok
    assert warnings(caplog) == []


def test_other_build_is_refused(session):
    session.board.expected_build = OTHER_BUILD
    with pytest.raises(BuildMismatch, match=f"build={BUILD}, expected {OTHER_BUILD}"):
        session.board.identify()


def test_other_build_accepted_is_a_warning(session, caplog):
    session.board.expected_build = OTHER_BUILD
    session.board.accept_build_mismatch = True
    assert session.board.identify().ok
    assert len(warnings(caplog)) == 1
    assert f"expected {OTHER_BUILD}" in warnings(caplog)[0]


def test_no_expected_build_is_no_check(session, caplog):
    assert session.board.expected_build is None
    assert session.board.identify().ok
    assert warnings(caplog) == []


# Command list

def test_capabilities_gives_the_whole_list(session):
    assert session.board.capabilities() == NAMES


def test_require_reads_the_list_once_per_session(session):
    session.board.require("PING")
    read = len(session.sent)
    assert read > 0
    session.board.require("HELLO")
    assert len(session.sent) == read


def test_require_a_missing_command(session):
    with pytest.raises(UnsupportedCommand, match="ABSENT is not in the command list"):
        session.board.require("ABSENT")


def test_require_reads_the_list_again_in_a_new_session(session):
    session.board.require("PING")
    session.board.start_session()
    session.sent.clear()
    session.board.require("PING")
    assert len(session.sent) > 0


# Toy protocol: no serial link, nothing of hil-1

class ToyProtocol(BenchProtocol):
    """Answers in memory; starts a new session by itself before the request named renew."""

    name = "toy-board"

    def __init__(self):
        super().__init__(TIMEOUT)
        self.started = []  # (timeout, settle) of each session
        self.restart = None

    def wait_ready(self, reset, timeout, settle):
        self.identity = None
        self.restart = (reset, timeout, settle)
        reset()
        self.started.append((timeout, settle))
        number = len(self.started)
        self.identity = Identity(f"toy {number}", "toy", "0.1", "9.0", BUILD, "button", 0)
        return self.identity

    def identify(self):
        return Response(1, "OK", None, {})  # No build field: only the identity holds it

    def commands(self):
        return ["blink", "renew"]

    def request(self, command, args, timeout=None):
        if command == "renew":
            self.wait_ready(*self.restart)
        return Response(1, "OK", None, dict(args))

    def watch(self, line):
        return None


@pytest.fixture
def toy():
    programmer = SimpleNamespace(resets=0)

    def reset():
        programmer.resets += 1

    programmer.reset = reset
    board = Board(ToyProtocol(), programmer, 1.5, 0.25)
    board.start_session()
    return board


def test_toy_protocol_session(toy):
    assert toy.adapter.started == [(1.5, 0.25)]
    assert toy.programmer.resets == 1
    assert toy.banner.proto == "9.0"
    assert toy.request("blink", times=2).fields == {"times": 2}
    toy.require("blink")
    with pytest.raises(UnsupportedCommand):
        toy.require("absent")


def test_toy_protocol_build_check(toy):
    toy.expected_build = BUILD
    assert toy.identify().ok
    toy.expected_build = OTHER_BUILD
    with pytest.raises(BuildMismatch):
        toy.identify()


def test_toy_protocol_starts_a_session_by_itself(toy):
    toy.request("renew")
    assert toy.banner.text == "toy 2"
    assert toy.programmer.resets == 2
