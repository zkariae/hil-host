"""Sessions with the board: startup, identity, command list, recovery.

The board is seen through the protocol interface alone (hil_host/protocol):
no rule of a given protocol is written here.
- start_session(): reset the board and wait for a valid readiness signal
  within the startup timeout; the protocol drops stale input first and
  checks the version;
- request(): one request; the protocol may start a new session first, when
  its rules ask for one;
- identify(): the identity request, which the protocol checks against the
  readiness signal, then the build check against the expected build;
- capabilities(): the commands the firmware lists, read once per session;
- require(): UnsupportedCommand for a command the firmware does not list;
- recover(): a new session after a lost one.

banner is the identity of the current session, None without one; it follows
a session the protocol starts by itself. adapter is the protocol object, for
the conformance tests of a project.

Version: 1.1
Date: 23/11/2026
Author: Z. BEN-AKKA
History:
  1.0  26/10/2026  Initial version.
  1.1  23/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/adapters/board.py, version 1.0; every protocol rule is behind BenchProtocol.
"""

import logging

from hil_host import LOGGER
from hil_host.bench.outcomes import BuildMismatch, UnsupportedCommand
from hil_host.protocol import BenchProtocol, Identity, Response

log = logging.getLogger(f"{LOGGER}.board")


class Board:
    """The board seen through the protocol: one session at a time."""

    def __init__(self, protocol: BenchProtocol, programmer, startup_timeout: float, settle: float,
                 expected_build: str | None = None, accept_build_mismatch: bool = False):
        self.adapter = protocol
        self.programmer = programmer
        self.startup_timeout = startup_timeout
        self.settle = settle  # Time given to stale input to arrive before the reset
        self.expected_build = expected_build  # None: no build check
        self.accept_build_mismatch = accept_build_mismatch
        self._names = None  # Command list, with the session it was read in
        self._names_session = None

    @property
    def banner(self) -> Identity | None:
        """Identity of the current session, None without one."""
        return self.adapter.identity

    def start_session(self) -> Identity:
        """Reset the board and wait for its readiness signal."""
        return self.adapter.wait_ready(self._reset, self.startup_timeout, self.settle)

    def recover(self) -> Identity:
        """A new session after a lost one; StartupFailure means the run must stop."""
        log.warning("recovery: flush, reset and wait for a new banner")
        return self.start_session()

    def request(self, command: str, /, **args) -> Response:
        """Send one request in the current session."""
        self._check_session()
        return self.adapter.request(command, args)

    def identify(self) -> Response:
        """The identity of the firmware: same values as the readiness signal, and the expected build."""
        self._check_session()
        response = self.adapter.identify()
        build = self.banner.build  # The protocol has checked the answer against it
        if self.expected_build is not None and build != self.expected_build:
            mismatch = BuildMismatch(f"firmware build={build}, expected {self.expected_build}")
            if not self.accept_build_mismatch:
                raise mismatch
            log.warning("%s (accepted by the bench file)", mismatch)
        return response

    def capabilities(self) -> list:
        """Every command the firmware lists, read again at each call."""
        self._check_session()
        names = self.adapter.commands()
        self._names, self._names_session = names, self.banner
        return names

    def require(self, command: str) -> None:
        """Raise UnsupportedCommand if the firmware does not list command."""
        self._check_session()
        if self._names is None or self._names_session is not self.banner:
            self.capabilities()
        if command not in self._names:
            raise UnsupportedCommand(f"{command} is not in the command list {self._names}")

    def _reset(self) -> None:
        # Looked up at each reset: the protocol keeps this function for the sessions it starts itself,
        # and a test may replace programmer.reset for a time
        self.programmer.reset()

    def _check_session(self) -> None:
        if self.banner is None:
            raise RuntimeError("no session: call start_session() first")
