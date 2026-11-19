"""Line protocol major 1 of the HIL bench firmware, behind BenchProtocol (registered as "hil-1").

Implements the host side of the protocol specification of the bench,
sections 2 to 6 and 8.3, on the line link of hil_host/bench/link.py:
- parse_record() checks a line against sections 2.2 and 2.3 and splits it
  into fields; format_request() writes a CMD record (section 5.1);
- wait_ready(): drop stale input, reset the board, wait for a valid READY
  within the startup timeout (lines that are not a valid banner are ignored,
  for example the tail of a banner sent before the port was flushed), check
  the protocol major version and restart the IDs at 1 (sections 3.4 and 4.1);
- request(): one transaction, starting a new session first once ID 65535 has
  been used (section 5.1); transaction() sends CMD with the next ID of the
  session and waits for RESULT then END within the response timeout. Any
  other record, a wrong ID, END before RESULT, a second RESULT, an ERROR
  record or the timeout raise ProtocolFailure (section 5.2.3); a READY record
  raises UnexpectedReset (section 4.3);
- identify(): HELLO, whose values must equal the banner's (section 6.2);
- commands(): every CAPS page, with the checks of section 6.3;
- watch(): a READY line read while idle is an unexpected reset (section 4.3).
For the conformance tests of a project, the object also drives the link the
way a conforming host never does: send_raw() and send_at_baud() of the link,
read_records() returns every record received until the link stays silent,
ERROR records included, and take_id() and read_response() are the two halves
of transaction(), for a raw request with a valid ID. The startup lines are
logged on hil.board, with the "session started" line the report reads.

Version: 1.4
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  26/10/2026  Initial version.
  1.1  26/10/2026  Log TX and RX lines at INFO, non-printable bytes escaped.
  1.2  26/10/2026  Add raw bytes, other baud rates and reading records until silence.
  1.3  09/11/2026  Lock the port exclusively; a busy port names the program that holds it.
  1.4  19/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/adapters/serial_adapter.py 1.3 (records, transactions) and board.py 1.0 (banner, HELLO, CAPS), behind BenchProtocol.
"""

import logging
import re
import time
from dataclasses import dataclass

from hil_host import LOGGER
from hil_host.bench.outcomes import (FunctionalError, IncompatibleProtocol, ProtocolFailure, StartupFailure,
                                     UnexpectedReset)
from hil_host.protocol import BenchProtocol, Identity, Response, register

LINE_MAX_LEN = 128  # Bytes, without the terminator (section 2.2)
ID_MIN = 1
ID_MAX = 65535
PENDING_MAX = 4 * LINE_MAX_LEN  # A line still unterminated past this is returned as is

TYPE_RE = re.compile(r"[A-Z]+")
KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
VALUE_RE = re.compile(r"[\x21-\x3c\x3e-\x7e]+")  # Printable, without space and '='
RESULT_RESERVED = ("id", "status", "code")

SUPPORTED_MAJOR = 1  # Protocol major version this host implements (section 3.4)
REQUIRED_COMMANDS = ("HELLO", "CAPS")  # Every conforming firmware has them (sections 6.2 and 6.3)
HELLO_FIELDS = ("name", "proto", "fw", "build")

# Readiness banner fields, in order, with their formats (section 4.1)
BANNER_FORMATS = {
    "name": re.compile(r"[a-z0-9-]{1,16}"),
    "fw": re.compile(r"(?=.{1,16}$)\d+\.\d+\.\d+"),
    "proto": re.compile(r"(?=.{1,7}$)\d+\.\d+"),
    "build": re.compile(r"[0-9a-f]{7,12}(-dirty)?|unknown"),
    "reset": re.compile(r"LPWR|WWDG|IWDG|SW|POR|BOR|PIN|unknown"),
    "reset_flags": re.compile(r"0x[0-9A-F]{8}"),
}
COMMAND_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")  # Section 6.1
DECIMAL_RE = re.compile(r"0|[1-9][0-9]*")

log = logging.getLogger(f"{LOGGER}.board")


class RecordError(ValueError):
    """A line that is not a valid record (sections 2.2 and 2.3)."""


class SessionExhausted(RuntimeError):
    """ID 65535 was used: the board must be reset before the next request (section 5.1)."""


@dataclass(frozen=True)
class Record:
    """One received record."""

    text: str  # The line as received
    type: str
    fields: tuple  # ((key, value), ...) in received order

    def keys(self) -> list:
        return [key for key, _ in self.fields]

    def get(self, key: str, default=None):
        return next((value for name, value in self.fields if name == key), default)


def parse_record(line: str) -> Record:
    """Check a line against sections 2.2 and 2.3 and split it into fields."""
    if len(line) > LINE_MAX_LEN:
        raise RecordError(f"{len(line)} bytes, more than {LINE_MAX_LEN}")
    bad = next((char for char in line if not 0x20 <= ord(char) <= 0x7E), None)
    if bad is not None:
        raise RecordError(f"byte 0x{ord(bad):02X} outside 0x20-0x7E")
    tokens = line.split()  # One or more spaces between tokens are accepted
    if not tokens:
        raise RecordError("empty line")
    if not TYPE_RE.fullmatch(tokens[0]):
        raise RecordError(f"type {tokens[0]!r} is not uppercase letters")
    fields = []
    for token in tokens[1:]:
        key, equal, value = token.partition("=")
        if not equal or not KEY_RE.fullmatch(key) or not VALUE_RE.fullmatch(value):
            raise RecordError(f"field {token!r} is not key=value")
        if any(key == name for name, _ in fields):
            raise RecordError(f"key {key!r} appears twice")
        fields.append((key, value))
    return Record(line, tokens[0], tuple(fields))


def format_request(request_id: int, command: str, args: dict) -> str:
    """Write a CMD record (section 5.1)."""
    tokens = ["CMD", f"id={request_id}", f"command={command}"]
    tokens += [f"{key}={value}" for key, value in args.items()]
    line = " ".join(tokens)
    parse_record(line)  # A request the host cannot send is a test bug
    return line


def parse_banner(line: str) -> Identity | None:
    """Return the identity if line is a valid READY record (section 4.1), otherwise None."""
    try:
        record = parse_record(line)
    except RecordError:
        return None
    if record.type != "READY":
        return None
    known = [key for key in record.keys() if key in BANNER_FORMATS]  # Unknown fields are ignored
    if known != list(BANNER_FORMATS):
        return None
    values = {key: record.get(key) for key in BANNER_FORMATS}
    if not all(BANNER_FORMATS[key].fullmatch(value) for key, value in values.items()):
        return None
    values["reset_flags"] = int(values["reset_flags"], 16)
    return Identity(line, **values)


def _decimal(response: Response, key: str) -> int:
    value = response.fields.get(key)
    if value is None or not DECIMAL_RE.fullmatch(value):
        raise ProtocolFailure(f"CAPS {key}={value} is not a decimal integer")
    return int(value)


@register
class Hil1(BenchProtocol):
    """Protocol major 1, on one line link; one session at a time."""

    name = "hil-1"
    default_timeouts = {"startup": 5.0, "response": 2.0}  # Section 8.1, HELLO and CAPS
    reserved_args = ("id", "command")  # Section 5.1
    command_name = COMMAND_NAME_RE
    max_pending = PENDING_MAX

    def __init__(self, response_timeout: float):
        super().__init__(response_timeout)
        self.next_id = ID_MIN  # ID of the next request in the session
        self.identity = None  # Banner of the current session
        self._restart = None  # (reset, timeout, settle) of the last wait_ready, for a new session

    # Link, for the conformance tests

    def send_line(self, line: str, eol: bytes = b"\r\n") -> None:
        self.link.send_line(line, eol)

    def send_raw(self, data: bytes) -> None:
        self.link.send_raw(data)

    def send_at_baud(self, data: bytes, baudrate: int, settle: float) -> None:
        self.link.send_at_baud(data, baudrate, settle)

    def read_line(self, deadline: float) -> str | None:
        return self.link.read_line(deadline)

    def flush_input(self) -> None:
        self.link.flush_input()

    # BenchProtocol

    def wait_ready(self, reset, timeout: float, settle: float) -> Identity:
        """Reset the board and wait for its readiness banner (sections 4.1 and 3.4)."""
        self.identity = None
        self._restart = (reset, timeout, settle)
        self._drain(settle)
        reset()

        deadline = time.monotonic() + timeout
        while True:
            line = self.link.read_line(deadline)
            if line is None:
                raise StartupFailure(f"no valid READY within {timeout} s after the reset")
            banner = parse_banner(line)
            if banner is not None:
                break
            log.info("ignored during startup: %r", line)

        major = int(banner.proto.split(".")[0])
        if major != SUPPORTED_MAJOR:
            raise IncompatibleProtocol(f"firmware proto={banner.proto}, host supports {SUPPORTED_MAJOR}.x")
        self.next_id = ID_MIN
        self.identity = banner
        log.info("session started: %s", banner.text)
        return banner

    def identify(self) -> Response:
        """HELLO: same values as the banner (section 6.2)."""
        response = self.request("HELLO", {})
        if not response.ok:
            raise FunctionalError(f"HELLO answered status=ERR code={response.code}")
        for key in HELLO_FIELDS:
            if response.fields.get(key) != getattr(self.identity, key):
                raise ProtocolFailure(
                    f"HELLO {key}={response.fields.get(key)} differs from the banner {key}={getattr(self.identity, key)}"
                )
        return response

    def commands(self) -> list:
        """Every CAPS page, checked against section 6.3."""
        names = []
        total = None
        start = 0
        while True:
            response = self.request("CAPS", {"start": start})
            if not response.ok:
                raise FunctionalError(f"CAPS start={start} answered status=ERR code={response.code}")
            page_total = _decimal(response, "total")
            if total is None:
                if not 2 <= page_total <= 255:
                    raise ProtocolFailure(f"CAPS total={page_total} outside 2 to 255")
                total = page_total
            elif page_total != total:
                raise ProtocolFailure(f"CAPS total changed from {total} to {page_total}")
            if _decimal(response, "start") != start:
                raise ProtocolFailure(f"CAPS start={response.fields.get('start')} for a request with start={start}")
            page = response.fields.get("names", "").split(",")
            for name in page:
                if not COMMAND_NAME_RE.fullmatch(name):
                    raise ProtocolFailure(f"CAPS name {name!r} is not a command name")
                if name in names:
                    raise ProtocolFailure(f"CAPS lists {name} twice")
                names.append(name)
            if "next" not in response.fields:
                break
            next_start = _decimal(response, "next")
            if next_start != start + len(page):
                raise ProtocolFailure(f"CAPS next={next_start} after {len(page)} names from start={start}")
            start = next_start

        if len(names) != total:
            raise ProtocolFailure(f"CAPS gave {len(names)} names for total={total}")
        missing = [name for name in REQUIRED_COMMANDS if name not in names]
        if missing:
            raise ProtocolFailure(f"CAPS list lacks {', '.join(missing)}")
        return names

    def request(self, command: str, args: dict, timeout: float | None = None) -> Response:
        """Send one request in the current session; a new session first once ID 65535 was used."""
        if self.identity is None:
            raise RuntimeError("no session: call wait_ready() first")
        if self.next_id > ID_MAX:
            log.info("ID %d used: new session (section 5.1)", ID_MAX)
            self.wait_ready(*self._restart)
        return self.transaction(command, args, timeout)

    def watch(self, line: str) -> Identity | None:
        """A READY line while idle is an unexpected reset (section 4.3)."""
        return parse_banner(line)

    # Records and transactions

    def read_record(self, deadline: float) -> Record | None:
        """Return the next non-blank record, or None at deadline; an invalid line is a ProtocolFailure."""
        while True:
            line = self.link.read_line(deadline)
            if line is None:
                return None
            if not line.strip(" "):
                continue  # Blank lines are ignored (section 2.2)
            try:
                return parse_record(line)
            except RecordError as error:
                raise ProtocolFailure(f"invalid record {line!r}: {error}") from None

    def read_records(self, quiet: float) -> list:
        """Every record received until no line arrives for quiet seconds; ERROR and READY included."""
        records = []
        while (record := self.read_record(time.monotonic() + quiet)) is not None:
            records.append(record)
        return records

    def take_id(self) -> int:
        """ID of the next request, marked as used (section 5.1)."""
        if self.next_id > ID_MAX:
            raise SessionExhausted("ID 65535 already used in this session")
        request_id = self.next_id
        self.next_id += 1
        return request_id

    def transaction(self, command: str, args: dict | None = None, timeout: float | None = None,
                    eol: bytes = b"\r\n") -> Response:
        """Send one request and return its response (sections 5.1 and 5.2); eol=b"\\n" sends LF alone."""
        request_id = self.take_id()
        self.link.send_line(format_request(request_id, command, args or {}), eol)
        return self.read_response(request_id, command, timeout)

    def read_response(self, request_id: int, command: str, timeout: float | None = None) -> Response:
        """Wait for RESULT then END of request_id (section 5.2.3); command only names it in failures."""
        limit = self.response_timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        result = None
        while True:
            record = self.read_record(deadline)
            if record is None:
                raise ProtocolFailure(f"no END for id={request_id} ({command}) within {limit} s")
            if record.type == "READY":
                raise UnexpectedReset(f"READY during id={request_id} ({command})", record.text)
            if record.type == "ERROR":
                raise ProtocolFailure(f"{record.text!r} while id={request_id} ({command}) was outstanding")
            if record.type == "RESULT":
                if result is not None:
                    raise ProtocolFailure(f"second RESULT for id={request_id}: {record.text!r}")
                result = self._check_result(record, request_id)
            elif record.type == "END":
                if record.fields != (("id", str(request_id)),):
                    raise ProtocolFailure(f"END is not 'END id={request_id}': {record.text!r}")
                if result is None:
                    raise ProtocolFailure(f"END before RESULT for id={request_id}")
                return result
            else:
                raise ProtocolFailure(f"unexpected {record.type} record: {record.text!r}")

    @staticmethod
    def _check_result(record: Record, request_id: int) -> Response:
        """Check the reserved fields of RESULT (section 5.2.1)."""
        keys = record.keys()
        if keys[:2] != ["id", "status"]:
            raise ProtocolFailure(f"RESULT does not start with id and status: {record.text!r}")
        if record.get("id") != str(request_id):
            raise ProtocolFailure(f"RESULT id differs from {request_id}: {record.text!r}")
        status = record.get("status")
        if status == "ERR":
            if len(keys) < 3 or keys[2] != "code":
                raise ProtocolFailure(f"RESULT status=ERR without code third: {record.text!r}")
            code, extra = record.get("code"), record.fields[3:]
        elif status == "OK":
            code, extra = None, record.fields[2:]
        else:
            raise ProtocolFailure(f"RESULT status is neither OK nor ERR: {record.text!r}")
        if any(key in RESULT_RESERVED for key, _ in extra):
            raise ProtocolFailure(f"RESULT uses a reserved key as a result field: {record.text!r}")
        return Response(request_id, status, code, dict(extra))

    def _drain(self, settle: float) -> None:
        """Drop input, then whatever stale bytes arrive within settle (old banners, partial lines)."""
        self.link.flush_input()
        deadline = time.monotonic() + settle
        while (line := self.link.read_line(deadline)) is not None:
            log.info("dropped before the reset: %r", line)
        self.link.flush_input()
