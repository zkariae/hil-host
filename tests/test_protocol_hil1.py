"""Tests of hil_host/protocol/hil1.py on the fake firmware of hil_host/fake/hil1.py, without a board.

The protocol runs on a Link over a FakePort, as it runs on the board's port.
The fake firmware answers right, or a test's own responder answers wrong on
purpose. Each test checks that a conforming exchange is accepted and that
each violation raises the outcome of section 7.3 of the protocol
specification:
  - records and transactions (sections 2, 4.3 and 5): parse_record,
    format_request, RESULT and END pairing, ERROR records, READY during a
    request, IDs up to 65535, and the lower-level calls of the conformance
    tests (raw bytes, records until silence, take_id and read_response);
  - the session (sections 3.4, 4.1, 5.1, 6.2 and 6.3): the readiness
    banner, its protocol major, stale lines before it, HELLO against the
    banner, every CAPS page and its checks, the new session after ID 65535,
    and an idle READY seen by watch().

Not tested here: the line link itself (tests/test_link.py), the build check,
require() and recover() of the board session (tests/test_board.py), and a
real board.

Version: 1.1
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  27/10/2026  Initial version.
  1.1  19/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/unit/test_serial_adapter.py 1.0 (records and transactions), on the fake firmware of hil_host.fake; tests of the banner, HELLO, CAPS and the new session added.
"""

import logging
import time

import pytest

from hil_host.bench.link import Link
from hil_host.bench.outcomes import (FunctionalError, IncompatibleProtocol, ProtocolFailure, StartupFailure,
                                     UnexpectedReset)
from hil_host.fake.hil1 import FakeFirmware, FakePort, FakeProgrammer, banner
from hil_host.protocol import BenchProtocol, Identity, get
from hil_host.protocol.hil1 import (ID_MAX, ID_MIN, Hil1, RecordError, SessionExhausted, format_request,
                                    parse_banner, parse_record)

BANNER = banner()
STARTUP = 1.0  # Seconds; the fake answers at once
TIMEOUT = 0.3


def protocol(responder=None, timeout: float = TIMEOUT) -> Hil1:
    """Hil1 on a fake port, without a session: for the record and transaction tests."""
    proto = Hil1(timeout)
    proto.open(Link(FakePort(responder), timeout, Hil1.max_pending))
    return proto


def port_of(proto: Hil1) -> FakePort:
    return proto.link._port


def reply(*lines):
    """Responder that answers any request with these lines."""
    return lambda line: "".join(f"{text}\r\n" for text in lines).encode()


def soon(seconds: float = 0.5) -> float:
    return time.monotonic() + seconds


@pytest.fixture
def bench():
    """(protocol, firmware, programmer) of a fake bench, session not started."""
    firmware = FakeFirmware()
    port = FakePort(firmware.respond)
    proto = Hil1(TIMEOUT)
    proto.open(Link(port, TIMEOUT, Hil1.max_pending))
    return proto, firmware, FakeProgrammer(port, firmware)


@pytest.fixture
def session(bench):
    """The fake bench with its session started."""
    proto, firmware, programmer = bench
    proto.wait_ready(programmer.reset, STARTUP, 0.0)
    return bench


# Registry

def test_registered_as_hil_1():
    assert get("hil-1") is Hil1
    assert issubclass(Hil1, BenchProtocol)
    assert Hil1.default_timeouts == {"startup": 5.0, "response": 2.0}
    assert Hil1.reserved_args == ("id", "command")
    assert Hil1.command_name.fullmatch("A" * 32) and not Hil1.command_name.fullmatch("A" * 33)
    assert Hil1.max_pending == 512


# parse_record and format_request: sections 2.2, 2.3 and 5.1

def test_parse_ok():
    record = parse_record("RESULT id=1 status=OK name=fake-bench")
    assert record.type == "RESULT"
    assert record.keys() == ["id", "status", "name"]


def test_parse_multiple_spaces_accepted():
    assert parse_record("  END   id=3 ").fields == (("id", "3"),)


def test_parse_line_of_128_bytes_accepted():
    line = "RESULT id=1 status=OK data=" + "A" * (128 - len("RESULT id=1 status=OK data="))
    assert len(line) == 128 and parse_record(line).get("data") == "A" * 101


@pytest.mark.parametrize("line", [
    "A" * 129, "END id=1\x7f", "end id=1", "END id", "END id=", "END =1", "END id=a=b",
    "END id=1 id=2", "END Id=1", "",
], ids=["too_long", "del_0x7f", "lowercase_type", "no_equal", "empty_value", "empty_key", "equal_in_value",
        "duplicate_key", "uppercase_key", "empty_line"])
def test_parse_rejects(line):
    with pytest.raises(RecordError):
        parse_record(line)


def test_format_request():
    assert format_request(7, "CAPS", {"start": 1}) == "CMD id=7 command=CAPS start=1"


# Transactions: sections 4.3, 5.1 and 5.2

def test_hello_ok_and_ids_increment():
    proto = protocol(FakeFirmware().respond)
    first = proto.transaction("HELLO")
    second = proto.transaction("CAPS")
    assert first.ok and first.id == 1 and first.fields["build"] == "abc1234"
    assert second.id == 2 and second.fields["names"] == "HELLO,CAPS"
    assert port_of(proto).written == ["CMD id=1 command=HELLO", "CMD id=2 command=CAPS"]


def test_err_response():
    response = protocol(reply("RESULT id=1 status=ERR code=UNKNOWN_COMMAND", "END id=1")).transaction("FOO")
    assert not response.ok
    assert response.code == "UNKNOWN_COMMAND"
    assert response.fields == {}


def test_blank_lines_ignored():
    assert protocol(reply("", "RESULT id=1 status=OK", "   ", "END id=1")).transaction("HELLO").ok


@pytest.mark.parametrize("lines, message", [
    (("RESULT id=2 status=OK", "END id=2"), "id differs"),
    (("END id=1",), "END before RESULT"),
    (("RESULT id=1 status=OK", "RESULT id=1 status=OK", "END id=1"), "second RESULT"),
    (("ERROR code=BAD_FRAME",), "ERROR"),
    (("RESULT id=1 status=OK", "END id=2"), "END is not"),
    (("RESULT id=1 status=OK", "END id=1 x=1"), "END is not"),
    (("RESULT status=OK id=1", "END id=1"), "does not start"),
    (("RESULT id=1 status=ERR", "END id=1"), "without code"),
    (("RESULT id=1 status=ERR x=1 code=A", "END id=1"), "without code"),
    (("RESULT id=1 status=OK code=A", "END id=1"), "reserved"),
    (("RESULT id=1 status=MAYBE", "END id=1"), "neither"),
    (("RESULT id=1 status=OK",), "no END"),
    ((), "no END"),
    (("HELLO x=1",), "unexpected HELLO"),
    (("RESULT id=1 status=OK bad",), "invalid record"),
])
def test_protocol_failures(lines, message):
    with pytest.raises(ProtocolFailure, match=message):
        protocol(reply(*lines), timeout=0.2).transaction("HELLO")


def test_ready_is_unexpected_reset():
    with pytest.raises(UnexpectedReset) as reset:
        protocol(reply(BANNER)).transaction("HELLO")
    assert reset.value.banner_line == BANNER


def test_session_exhausted_after_65535():
    proto = protocol(FakeFirmware().respond)
    proto.next_id = 65535
    assert proto.transaction("HELLO").id == 65535
    with pytest.raises(SessionExhausted):
        proto.transaction("HELLO")


# Lower-level calls of the conformance tests

def test_link_calls_reach_the_port():
    proto = protocol()
    proto.send_raw(b"CMD id=1 command=HE\x7fLLO\r\n")
    proto.send_line("CMD id=2 command=HELLO", eol=b"\n")
    proto.send_at_baud(b"\x00", 9600, 0)
    assert port_of(proto).writes == [(115200, b"CMD id=1 command=HE\x7fLLO\r\n"),
                                     (115200, b"CMD id=2 command=HELLO\n"), (9600, b"\x00")]
    assert port_of(proto).baudrate == 115200
    port_of(proto).incoming += b"LINE\r\n"
    assert proto.read_line(soon()) == "LINE"
    port_of(proto).incoming += b"STALE\r\n"
    proto.flush_input()
    assert proto.read_line(soon(0.1)) is None


def test_read_records_until_silence():
    proto = protocol()
    port_of(proto).incoming += b"ERROR code=BAD_FRAME\r\n\r\nERROR code=LINE_TOO_LONG\n"
    assert [record.text for record in proto.read_records(0.1)] == ["ERROR code=BAD_FRAME",
                                                                   "ERROR code=LINE_TOO_LONG"]
    assert proto.read_records(0.1) == []


def test_read_records_invalid_line():
    proto = protocol()
    port_of(proto).incoming += b"garbage line\r\n"
    with pytest.raises(ProtocolFailure, match="invalid record"):
        proto.read_records(0.1)


def test_transaction_lf_alone():
    proto = protocol(FakeFirmware().respond)
    assert proto.transaction("HELLO", eol=b"\n").ok
    assert port_of(proto).writes == [(115200, b"CMD id=1 command=HELLO\n")]


def test_take_id_and_read_response():
    proto = protocol(FakeFirmware().respond)
    request_id = proto.take_id()
    assert (request_id, proto.next_id) == (1, 2)
    proto.send_raw(f"  CMD   id={request_id}  command=HELLO  \r\n".encode())
    assert proto.read_response(request_id, "HELLO").ok


def test_take_id_after_65535():
    proto = protocol()
    proto.next_id = 65536
    with pytest.raises(SessionExhausted):
        proto.take_id()


# Readiness banner: section 4.1

def test_parse_banner_valid():
    identity = parse_banner(BANNER + " extra=1")  # An unknown field is ignored
    assert isinstance(identity, Identity)
    assert (identity.name, identity.fw, identity.proto, identity.build) == ("fake-bench", "1.0.0", "1.0", "abc1234")
    assert (identity.reset, identity.reset_flags) == ("PIN", 0x04000000)
    assert identity.text == BANNER + " extra=1"


@pytest.mark.parametrize("line", [
    BANNER.replace(" build=abc1234", ""),
    BANNER.replace("reset=PIN", "reset=HOT"),
    BANNER.replace("READY", "HELLO"),
    BANNER + " bad",
], ids=["missing_field", "value_out_of_format", "other_type", "invalid_record"])
def test_parse_banner_rejects(line):
    assert parse_banner(line) is None


# Session start: sections 3.4 and 4.1

def test_wait_ready_gives_the_identity_and_restarts_the_ids(bench):
    proto, _, programmer = bench
    proto.next_id = 10
    identity = proto.wait_ready(programmer.reset, STARTUP, 0.0)
    assert identity == parse_banner(BANNER)
    assert proto.identity == identity
    assert proto.next_id == ID_MIN
    assert programmer.resets == 1


def test_wait_ready_ignores_lines_before_the_banner(bench, caplog):
    proto, _, _ = bench
    caplog.set_level(logging.INFO, logger="hil.board")
    port = port_of(proto)
    proto.wait_ready(lambda: port.incoming.extend(f"r=PIN reset_flags=0x0\r\n{BANNER}\r\n".encode()), STARTUP, 0.0)
    messages = [record.getMessage() for record in caplog.records]
    assert "ignored during startup: 'r=PIN reset_flags=0x0'" in messages
    assert f"session started: {BANNER}" in messages


def test_wait_ready_without_banner(bench):
    proto, _, _ = bench
    with pytest.raises(StartupFailure, match="no valid READY within 0.2 s"):
        proto.wait_ready(lambda: None, 0.2, 0.0)
    assert proto.identity is None


def test_wait_ready_other_major():
    firmware = FakeFirmware(proto="2.0")
    port = FakePort(firmware.respond)
    proto = protocol()
    proto.open(Link(port, TIMEOUT, Hil1.max_pending))
    with pytest.raises(IncompatibleProtocol, match="proto=2.0"):
        proto.wait_ready(FakeProgrammer(port, firmware).reset, STARTUP, 0.0)


def test_wait_ready_logs_the_session_start(bench, caplog):
    proto, _, programmer = bench
    caplog.set_level(logging.INFO, logger="hil.board")
    proto.wait_ready(programmer.reset, STARTUP, 0.0)
    assert [(record.name, record.getMessage()) for record in caplog.records] == [
        ("hil.board", f"session started: {BANNER}")]


# HELLO: section 6.2

def test_identify_ok(session):
    proto, _, _ = session
    response = proto.identify()
    assert response.ok
    assert {key: response.fields[key] for key in ("name", "proto", "fw", "build")} == {
        "name": "fake-bench", "proto": "1.0", "fw": "1.0.0", "build": "abc1234"}


def test_identify_differs_from_the_banner(session):
    proto, firmware, _ = session
    firmware.fw = "9.9.9"  # The banner of the session said 1.0.0
    with pytest.raises(ProtocolFailure, match="HELLO fw=9.9.9 differs from the banner fw=1.0.0"):
        proto.identify()


def test_identify_err(session):
    proto, firmware, _ = session
    port_of(proto).responder = reply("RESULT id=1 status=ERR code=INTERNAL", "END id=1")
    with pytest.raises(FunctionalError, match="HELLO answered status=ERR code=INTERNAL"):
        proto.identify()


# CAPS: section 6.3

NAMES = ["HELLO", "CAPS"] + [f"COMMAND_NUMBER_{n:02d}" for n in range(14)]


def test_commands_every_page(bench):
    proto, firmware, programmer = bench
    firmware.caps = NAMES
    proto.wait_ready(programmer.reset, STARTUP, 0.0)
    assert proto.commands() == NAMES
    pages = [line for line in port_of(proto).written if "command=CAPS" in line]
    assert len(pages) > 2 and pages[0].endswith("start=0")


def caps(*pages):
    """Responder: HELLO as the fake firmware, CAPS from these bodies by start value."""
    firmware = FakeFirmware()
    bodies = dict(pages)

    def respond(line):
        tokens = dict(token.split("=", 1) for token in line.split()[1:])
        if tokens["command"] != "CAPS":
            return firmware.respond(line)
        return f"RESULT id={tokens['id']} status=OK {bodies[tokens['start']]}\r\nEND id={tokens['id']}\r\n".encode()

    return respond


@pytest.mark.parametrize("pages, message", [
    ((("0", "total=3 start=0 names=HELLO,CAPS next=2"), ("2", "total=4 start=2 names=PING")), "total changed"),
    ((("0", "total=3 start=0 names=HELLO,CAPS next=3"),), "next=3 after 2 names"),
    ((("0", "total=3 start=0 names=HELLO,CAPS next=2"), ("2", "total=3 start=2 names=CAPS")), "lists CAPS twice"),
    ((("0", "total=2 start=0 names=HELLO,PING"),), "lacks CAPS"),
    ((("0", "total=1 start=0 names=HELLO"),), "total=1 outside 2 to 255"),
    ((("0", "total=2 start=1 names=HELLO,CAPS"),), "start=1 for a request with start=0"),
    ((("0", "total=2 start=0 names=hello,CAPS"),), "'hello' is not a command name"),
    ((("0", "total=two start=0 names=HELLO,CAPS"),), "total=two is not a decimal integer"),
    ((("0", "total=3 start=0 names=HELLO,CAPS"),), "2 names for total=3"),
], ids=["total_changes", "wrong_next", "duplicate", "lacks_caps", "total_too_small", "wrong_start", "bad_name",
        "total_not_decimal", "names_missing"])
def test_commands_rejects(pages, message):
    firmware = FakeFirmware()
    port = FakePort(caps(*pages))
    proto = protocol()
    proto.open(Link(port, TIMEOUT, Hil1.max_pending))
    proto.wait_ready(FakeProgrammer(port, firmware).reset, STARTUP, 0.0)
    with pytest.raises(ProtocolFailure, match=message):
        proto.commands()


# Requests in a session: section 5.1

def test_request_needs_a_session(bench):
    proto, _, _ = bench
    with pytest.raises(RuntimeError, match="no session"):
        proto.request("HELLO", {})


def test_request_starts_a_new_session_after_65535(session):
    proto, _, programmer = session
    proto.next_id = ID_MAX
    first = proto.identity
    assert proto.request("HELLO", {}).id == ID_MAX
    assert proto.request("HELLO", {}).id == ID_MIN
    assert programmer.resets == 2
    assert proto.identity is not first  # A new session started


# Idle lines: section 4.3

def test_watch(session):
    proto, _, _ = session
    assert proto.watch(banner(reset="SW", flags=0x14000000)).reset == "SW"
    assert proto.watch("RESULT id=1 status=OK") is None
