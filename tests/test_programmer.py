"""Tests of hil_host/bench/programmer.py on the image of the example configuration, without a board.

Each test starts from the image of examples/hil_host.yaml, with a command
replaced by a line of Python: nothing is built, programmed or reset.
  - commands: flash(), reset() and reset_sw() each run their own command of
    the configuration, from root;
  - failures: an exit status, a timeout or a command that cannot start is
    PROGRAMMING_FAILURE, with the last output lines, stderr included;
  - output and log: any byte is accepted, RUN <action>: <command> is logged
    at INFO and the output at DEBUG;
  - build_id(): read with the file and pattern of the configuration, at each
    call, None when there is nothing to read;
  - the fake programmer offers the same methods.

Not tested here: a real board, and the processes started by a command that
is stopped on its timeout (only the command itself is killed).

Version: 1.0
Date: 23/11/2026
Author: Z. BEN-AKKA
History:
  1.0  23/11/2026  Initial version.
"""

import logging
import re
import shlex
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hil_host.bench.outcomes import ProgrammingFailure
from hil_host.bench.programmer import OUTPUT_TAIL_LINES, Programmer
from hil_host.config import load
from hil_host.fake.hil1 import FakeProgrammer, banner

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load(ROOT / "examples" / "hil_host.yaml")
ACTIONS = ("flash", "reset", "reset_sw")


def python(code: str) -> tuple:
    """A command of the configuration that runs a line of Python."""
    return ("{python}", "-c", code)


def programmer(root: Path = ROOT, **changes) -> Programmer:
    """A programmer on the example image, with some of its fields changed."""
    return Programmer(replace(CONFIG.image, **changes), root)


@pytest.fixture
def records(caplog):
    """(level, text) of the records of the programmer logger, read when called."""
    caplog.set_level(logging.DEBUG, logger="hil.programmer")
    return lambda: [(record.levelname, record.getMessage()) for record in caplog.records
                    if record.name == "hil.programmer"]


# Commands

@pytest.mark.parametrize("action", ACTIONS)
def test_each_action_runs_its_own_command(records, action):
    commands = {name: python(f"print('this is {name}')") for name in ACTIONS}
    getattr(programmer(**commands), action)()
    assert [text for level, text in records() if level == "DEBUG"] == [f"{action}: this is {action}"]


def test_command_runs_from_root(tmp_path):
    programmer(tmp_path, flash=python("open('here.txt', 'w').close()")).flash()
    assert (tmp_path / "here.txt").is_file()


# Failures

def test_exit_status_keeps_the_last_output_lines():
    with pytest.raises(ProgrammingFailure) as caught:
        programmer(flash=python("import sys\nfor i in range(20): print('line', i)\nsys.exit(3)")).flash()
    first, *tail = str(caught.value).splitlines()
    assert first == "PROGRAMMING_FAILURE: flash failed (exit 3):"
    assert tail == [f"line {number}" for number in range(20 - OUTPUT_TAIL_LINES, 20)]


def test_failure_message_holds_stderr():
    with pytest.raises(ProgrammingFailure, match="no probe found"):
        programmer(reset=python("import sys; sys.exit('no probe found')")).reset()


def test_timeout():
    started = time.monotonic()
    with pytest.raises(ProgrammingFailure, match=r"reset_sw did not finish within 0\.5 s"):
        programmer(reset_sw=python("import time; time.sleep(5)"), timeout=0.5).reset_sw()
    assert time.monotonic() - started < 4.0


def test_command_that_cannot_start(tmp_path):
    with pytest.raises(ProgrammingFailure, match="flash could not start"):
        programmer(flash=(str(tmp_path / "absent"),)).flash()


# Output and log

def test_output_that_is_not_utf8_is_accepted(records):
    programmer(flash=python(r"import sys; sys.stdout.buffer.write(b'ab\xff')")).flash()
    assert ("DEBUG", "flash: ab�") in records()


def test_command_logged_at_info_and_output_at_debug(records):
    code = "print('one'); print('two')"
    programmer(reset=python(code)).reset()
    assert records() == [
        ("INFO", f"RUN reset: {shlex.join([sys.executable, '-c', code])}"),
        ("DEBUG", "reset: one"),
        ("DEBUG", "reset: two"),
    ]


# build_id()

def test_build_id_of_the_example_is_the_build_of_the_fake_firmware():
    assert programmer().build_id() == re.search(r"build=(\S+)", banner()).group(1)


@pytest.mark.parametrize("case", ["no-source", "no-file", "no-match"])
def test_build_id_without_a_build(tmp_path, case):
    empty = tmp_path / "build.txt"
    empty.write_text("nothing here\n", encoding="utf-8")
    changes = {
        "no-source": {"build_file": None, "build_pattern": None},
        "no-file": {"build_file": tmp_path / "absent.txt"},
        "no-match": {"build_file": empty},
    }[case]
    assert programmer(**changes).build_id() is None


def test_build_id_is_read_at_each_call(tmp_path):
    build = tmp_path / "build.txt"
    bench = programmer(build_file=build, build_pattern=re.compile(r"build (\w+)"))
    build.write_text("build aaa1111\n", encoding="utf-8")
    first = bench.build_id()
    build.write_text("build bbb2222\n", encoding="utf-8")
    assert (first, bench.build_id()) == ("aaa1111", "bbb2222")


# Interface

def test_fake_programmer_has_the_same_methods():
    def public(cls):
        return {name for name in vars(cls) if not name.startswith("_")}

    assert public(FakeProgrammer) == public(Programmer)
