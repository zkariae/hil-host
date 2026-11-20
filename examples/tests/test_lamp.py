"""Example pytest family: the lamp of the fake bench of examples/hil_host.yaml, without a board.

The pytest family of the example configuration collects this folder; the
Tests tab of the TUI lists and runs these tests like any project test. The
session is opened here on the fake firmware of hil_host.fake, which lists
the commands of examples/commands.yaml: LAMP_SET with a valid state is
answered OK with the state echoed, and the value bad is refused with
INVALID_ARGUMENT.

Not tested here: a real bench, the state of the lamp (the fake firmware
echoes the arguments it receives but keeps no state, so LAMP_GET cannot be
checked) and the fixtures of the board session, which a project test gets
from the hil_host.plugins.board_fixtures plugin instead of opening the
session itself.

Version: 1.0
Date: 20/11/2026
Author: Z. BEN-AKKA
History:
  1.0  20/11/2026  Initial version.
"""

from pathlib import Path

import pytest
import yaml

from hil_host.bench.link import Link
from hil_host.fake.hil1 import FakeFirmware, FakePort, FakeProgrammer
from hil_host.protocol.hil1 import Hil1

CATALOGUE = Path(__file__).resolve().parents[1] / "commands.yaml"
STARTUP = 1.0  # Seconds; the fake answers at once
TIMEOUT = 0.3


@pytest.fixture(scope="module")
def bench() -> Hil1:
    """A ready session on the fake firmware, which lists the commands of the example catalogue."""
    names = list(yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))["commands"])
    firmware = FakeFirmware(caps=names)
    port = FakePort(firmware.respond)
    proto = Hil1(TIMEOUT)
    proto.open(Link(port, TIMEOUT, Hil1.max_pending))
    proto.wait_ready(FakeProgrammer(port, firmware).reset, STARTUP, 0.0)
    yield proto
    proto.link.close()


@pytest.mark.parametrize("state", ["on", "off"], ids=["on", "off"])
def test_lamp_set_answers_ok(bench, state):
    response = bench.request("LAMP_SET", {"state": state})
    assert response.ok
    assert response.fields["state"] == state


def test_lamp_set_rejects_a_bad_state(bench):
    response = bench.request("LAMP_SET", {"state": "bad"})
    assert (response.status, response.code) == ("ERR", "INVALID_ARGUMENT")
