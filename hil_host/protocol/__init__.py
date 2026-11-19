"""Protocol interface of hil-host: the one place the rest of hil-host talks to about the firmware.

Every rule of the protocol spoken with the firmware sits behind BenchProtocol:
framing of the lines into messages, the readiness signal and its version
check, the identity request, the command list, the pairing of a request with
its response, and what an unsolicited line means. bench/, plugins/, core/ and
ui/ only use this interface and its two data types:

  - Identity: a valid readiness signal (name, firmware and protocol versions,
    build, reset cause and flags, and any field the protocol adds);
  - Response: one answer without the protocol's own fields. Its status is
    "OK" or "ERR": every protocol maps its statuses onto these two, so ok,
    status, code and fields mean the same whatever the protocol.

A protocol works on the line link of hil_host/bench/link.py (open()), whose
unterminated line limit it gives (max_pending). The board session decides
what to do with an identity: build check, session lost, recovery.

Implementations register under the name the project configuration uses
(protocol: in the configuration file). get() returns a class registered
with register(), such as the toy protocol of a test, or else imports the
module of hil-host that holds that name: the registry is the only place
that names the implementations.

Version: 1.1
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  19/11/2026  Initial version.
  1.1  19/11/2026  identify() returns the Response of the identity request, checked against the readiness signal.
"""

import importlib
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

STATUS_OK = "OK"
STATUS_ERR = "ERR"

_REGISTRY = {}  # Name: protocol class
_BUILTIN = {"hil-1": "hil_host.protocol.hil1"}  # Name: module of hil-host that registers it


@dataclass(frozen=True)
class Identity:
    """A valid readiness signal of the firmware."""

    text: str  # The line as received
    name: str
    fw: str
    proto: str
    build: str
    reset: str  # Reset cause, as the firmware names it
    reset_flags: int
    extra: dict = field(default_factory=dict)  # Fields the protocol adds


@dataclass(frozen=True)
class Response:
    """One answer to a request, without the protocol's own fields."""

    id: int
    status: str  # STATUS_OK or STATUS_ERR
    code: str | None  # Error code when status is STATUS_ERR
    fields: dict  # Result fields, in received order

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


class BenchProtocol(ABC):
    """The protocol spoken with the firmware, over one line link."""

    name: str = ""  # Name used by the configuration
    default_timeouts: dict = {}  # {"startup": seconds, "response": seconds}; the report shows other values
    reserved_args: tuple = ()  # Argument names a command may not use
    command_name: re.Pattern = re.compile(r".+")  # A valid command name, length included
    max_pending: int = 0  # Bytes of an unterminated line the link keeps before returning it

    def __init__(self, response_timeout: float):
        self.response_timeout = response_timeout
        self.link = None

    def open(self, link) -> None:
        """Take the line link to talk on; a new link replaces the previous one."""
        self.link = link

    @abstractmethod
    def wait_ready(self, reset: Callable[[], None], timeout: float, settle: float) -> Identity:
        """Drop stale input for settle seconds, call reset(), then wait for a valid readiness signal.

        Raises StartupFailure without one within timeout, IncompatibleProtocol
        for a version this implementation does not speak.
        """

    @abstractmethod
    def identify(self) -> Response:
        """Ask the firmware for its identity, check it against the readiness signal and return the answer."""

    @abstractmethod
    def commands(self) -> list:
        """Every command name the firmware supports, in its order."""

    @abstractmethod
    def request(self, command: str, args: dict, timeout: float | None = None) -> Response:
        """Send one request and return its response; a broken exchange raises a HilOutcome."""

    @abstractmethod
    def watch(self, line: str) -> Identity | None:
        """A line read while no request is outstanding: an Identity means the firmware has reset."""


class UnknownProtocol(ValueError):
    """No protocol of that name: a configuration error."""


def register(cls: type) -> type:
    """Class decorator: make a protocol available under its name."""
    if not (isinstance(cls, type) and issubclass(cls, BenchProtocol)) or not cls.name:
        raise TypeError("register() takes a BenchProtocol subclass with a name")
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type:
    """The protocol class of a name: registered first, then the modules of hil-host."""
    if name in _REGISTRY:
        return _REGISTRY[name]
    module = _BUILTIN.get(name)
    if module is None:
        known = sorted(set(_REGISTRY) | set(_BUILTIN))
        raise UnknownProtocol(f"unknown protocol {name!r}: one of {', '.join(known)}")
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as error:
        raise UnknownProtocol(f"protocol {name!r}: module {module} not found ({error})") from None
    if name not in _REGISTRY:
        raise UnknownProtocol(f"protocol {name!r}: module {module} does not register it")
    return _REGISTRY[name]
