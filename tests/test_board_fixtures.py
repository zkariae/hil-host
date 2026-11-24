"""Tests of hil_host/plugins/board_fixtures.py in a small project, with the fake firmware on a pty.

Each test writes a project in tmp_path (configuration, bench file, tests)
and runs pytest on it in a subprocess, as a consuming project does. The
fake firmware answers on a pty. The image commands only write their name in
a file that the firmware reads: a reset makes it send its banner, always
before the answer to a later request, as a board does.
  - configuration: the ini option, --hil-config over it and relative to the
    current folder, an unusable file, no configuration at all;
  - flash once and before the reset, --no-flash, a failed flash, --port;
  - the log file, in results_dir or where --log-file says;
  - a session lost by a test is recovered by fresh_board for the next one:
    the firmware answers nothing until it is reset;
  - the build check, with the build of the image (auto) or none;
  - the protocol is the one the configuration names.

Not tested here: a real board, and the rules of the session itself
(tests/test_board.py).

Version: 1.0
Date: 24/11/2026
Author: Z. BEN-AKKA
History:
  1.0  24/11/2026  Initial version.
"""

import os
import select
import shutil
import subprocess
import sys
import threading
import tty
from pathlib import Path

import pytest
import yaml

from hil_host.fake.hil1 import PIN_FLAGS, SW_FLAGS, FakeFirmware

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
BUILD = "abc1234"  # Build of the fake firmware
OTHER_BUILD = "fffffff"
RUN_TIMEOUT = 60  # Seconds, for a whole pytest run

CONFTEST = 'pytest_plugins = ["hil_host.plugins.board_fixtures"]\n'

TESTS = """
def test_board(board, quiet, bench_config, hil_config):
    assert board.banner.name == "fake-bench"
    assert (quiet, bench_config.extra_byte_wait, hil_config.protocol) == (0.2, 0.2, "hil-1")
    assert board.request("LAMP_SET", state="on").fields == {"state": "on"}
"""

# The firmware hangs during the first test, until the next reset
TESTS_LOST_SESSION = """
def test_lost(fresh_board):
    with open("calls.txt", "a") as calls:
        calls.write("hang\\n")

def test_recovered(fresh_board):
    assert fresh_board.request("LAMP_GET").ok
"""

TESTS_FREE = """
def test_free():
    pass

def test_board(board):
    pass
"""

# Answers in memory: only the port is opened
CONFTEST_TOY = CONFTEST + """
from hil_host.protocol import BenchProtocol, Identity, Response, register

@register
class Toy(BenchProtocol):
    name = "toy-fixtures"

    def wait_ready(self, reset, timeout, settle):
        reset()
        self.identity = Identity("toy", "toy", "0.1", "9.0", "abc1234", "button", 0)
        return self.identity

    def identify(self):
        return Response(1, "OK", None, {})

    def commands(self):
        return ["blink"]

    def request(self, command, args, timeout=None):
        return Response(1, "OK", None, args)

    def watch(self, line):
        return None
"""

TESTS_TOY = """
def test_toy(fresh_board):
    assert fresh_board.banner.name == "toy"
    fresh_board.require("blink")
"""


class Firmware:
    """The fake firmware on a pty, driven by the lines of the calls file."""

    def __init__(self, calls: Path):
        self.calls = calls
        self.done = 0  # Lines of the calls file already seen
        self.fake = FakeFirmware(build=BUILD, caps=list(
            yaml.safe_load((EXAMPLES / "commands.yaml").read_text(encoding="utf-8"))["commands"]))
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        pending = b""
        while not self.stopping.is_set():
            self._follow_calls()
            if select.select([self.master], [], [], 0.01)[0]:
                pending += os.read(self.master, 4096)
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    self._follow_calls()  # A reset firmware sends its banner before any answer
                    os.write(self.master, self.fake.respond(line.decode("ascii").strip()))

    def _follow_calls(self) -> None:
        text = self.calls.read_text(encoding="utf-8")
        lines = text[:text.rfind("\n") + 1].splitlines()  # Whole lines only
        new, self.done = lines[self.done:], len(lines)
        for call in new:
            if call == "hang":
                self.fake.silent = True
            elif call in ("reset", "reset_sw"):
                self.fake.silent = False
                cause, flags = ("PIN", PIN_FLAGS) if call == "reset" else ("SW", SW_FLAGS)
                os.write(self.master, f"{self.fake.banner(cause, flags)}\r\n".encode("ascii"))

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()
        os.close(self.master)
        os.close(self.slave)


class Project:
    """A project in a folder, whose bench is the fake firmware."""

    def __init__(self, folder: Path, port: str):
        self.folder = folder
        self.calls_file = folder / "calls.txt"
        self.results = folder / "results"
        shutil.copy(EXAMPLES / "commands.yaml", folder / "commands.yaml")
        self.bench = yaml.safe_load((EXAMPLES / "bench.yaml").read_text(encoding="utf-8"))
        self.bench["serial"]["port"] = port
        self.bench["timeouts"] = {"startup": 5.0, "response": 1.0}
        self.config = yaml.safe_load((EXAMPLES / "hil_host.yaml").read_text(encoding="utf-8"))
        self.config.update(root=".", bench="bench.yaml", catalog="commands.yaml", results_dir="results", families=[],
                           command_names=None)
        self.config["image"].update({action: self._call(action) for action in ("flash", "reset", "reset_sw")})
        self.config["image"]["build_id"] = {"file": "build.txt", "pattern": r"build=(\w+)"}
        self.build(BUILD)
        self.tests(TESTS)
        self.ini("../hil_host.yaml")

    def _call(self, action: str) -> list:
        return ["{python}", "-c", f"open('calls.txt', 'a').write('{action}\\n')"]

    def build(self, build: str) -> None:
        (self.folder / "build.txt").write_text(f"build={build}\n", encoding="utf-8")

    def tests(self, text: str, conftest: str = CONFTEST) -> None:
        tests = self.folder / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "conftest.py").write_text(conftest, encoding="utf-8")
        (tests / "test_project.py").write_text(text, encoding="utf-8")

    def ini(self, hil_config: str | None) -> None:
        """The pytest.ini of the tests folder, with or without the configuration."""
        lines = ["[pytest]", f"pythonpath = {ROOT}", "log_file_level = DEBUG"]
        if hil_config:
            lines.append(f"hil_config = {hil_config}")
        (self.folder / "tests" / "pytest.ini").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self, *args: str, cwd: Path | None = None) -> tuple:
        """(exit status, output) of a pytest run; cwd is the project folder by default."""
        for name, data in (("bench.yaml", self.bench), ("hil_host.yaml", self.config)):
            (self.folder / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(self.folder / "tests"), *args],
            cwd=cwd or self.folder, capture_output=True, text=True, timeout=RUN_TIMEOUT)
        return done.returncode, done.stdout + done.stderr

    def calls(self) -> list:
        return self.calls_file.read_text(encoding="utf-8").split()


@pytest.fixture
def project(tmp_path):
    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    firmware = Firmware(calls)
    yield Project(tmp_path, firmware.port)
    firmware.stop()


# Configuration

def test_configuration_of_the_ini_option(project):
    status, output = project.run()
    assert status == 0, output
    assert "1 passed" in output


def test_option_wins_over_the_ini(project):
    project.ini("../absent.yaml")
    status, output = project.run("--hil-config", str(project.folder / "hil_host.yaml"))
    assert status == 0, output


def test_option_is_relative_to_the_current_folder(project):
    project.ini(None)
    elsewhere = project.folder / "elsewhere" / "deeper"
    elsewhere.mkdir(parents=True)
    status, output = project.run("--hil-config", "../../hil_host.yaml", cwd=elsewhere)
    assert status == 0, output


def test_unusable_configuration_is_a_usage_error(project):
    project.config["image"]["timeout"] = 0
    status, output = project.run()
    assert status == pytest.ExitCode.USAGE_ERROR, output
    assert "image.timeout must be a positive number" in output
    assert project.calls() == []


def test_no_configuration_fails_only_the_bench_fixtures(project):
    project.ini(None)
    project.tests(TESTS_FREE)
    status, output = project.run()
    assert status == pytest.ExitCode.TESTS_FAILED, output
    assert "1 passed, 1 error" in output
    assert "no project configuration: give --hil-config" in output


# Flash and port

def test_flash_once_before_the_reset(project):
    status, output = project.run()
    assert status == 0, output
    assert project.calls() == ["flash", "reset"]


def test_no_flash(project):
    status, output = project.run("--no-flash")
    assert status == 0, output
    assert project.calls() == ["reset"]


def test_failed_flash_stops_before_the_reset(project):
    project.config["image"]["flash"] = ["{python}", "-c", "import sys; sys.exit('no probe found')"]
    status, output = project.run()
    assert status == pytest.ExitCode.TESTS_FAILED, output
    assert "PROGRAMMING_FAILURE" in output and "no probe found" in output
    assert project.calls() == []


def test_port_option_wins_over_the_bench_file(project):
    port, project.bench["serial"]["port"] = project.bench["serial"]["port"], str(project.folder / "absent-port")
    status, output = project.run("--port", port)
    assert status == 0, output


# Log file

def test_log_file_in_the_results_folder(project):
    status, output = project.run()
    assert status == 0, output
    assert "session started: " in (project.results / "pytest.log").read_text(encoding="utf-8")


def test_log_file_option(project):
    log_file = project.folder / "other.log"
    status, output = project.run("--log-file", str(log_file))
    assert status == 0, output
    assert "session started: " in log_file.read_text(encoding="utf-8")
    assert not (project.results / "pytest.log").exists()


# Lost session

def test_fresh_board_recovers_a_lost_session(project):
    project.tests(TESTS_LOST_SESSION)
    status, output = project.run()
    assert status == 0, output
    assert "2 passed" in output
    assert project.calls() == ["flash", "reset", "hang", "reset"]
    log = (project.results / "pytest.log").read_text(encoding="utf-8")
    assert "session lost after the previous test: PROTOCOL_FAILURE" in log


# Build check

def test_other_build_than_the_image(project):
    project.build(OTHER_BUILD)
    status, output = project.run()
    assert status == pytest.ExitCode.TESTS_FAILED, output
    assert f"BUILD_MISMATCH: firmware build={BUILD}, expected {OTHER_BUILD}" in output


def test_other_build_accepted(project):
    project.build(OTHER_BUILD)
    project.bench["firmware"]["accept_build_mismatch"] = True
    status, output = project.run()
    assert status == 0, output
    assert "accepted by the bench file" in (project.results / "pytest.log").read_text(encoding="utf-8")


def test_no_build_of_the_image_is_a_warning(project):
    (project.folder / "build.txt").unlink()
    status, output = project.run()
    assert status == 0, output
    assert "build not checked" in (project.results / "pytest.log").read_text(encoding="utf-8")


def test_expected_build_null_is_no_check(project):
    project.build(OTHER_BUILD)
    project.bench["firmware"]["expected_build"] = None
    status, output = project.run()
    assert status == 0, output
    assert "build not checked" not in (project.results / "pytest.log").read_text(encoding="utf-8")


# Protocol

def test_protocol_of_the_configuration(project):
    project.config["protocol"] = "toy-fixtures"
    project.tests(TESTS_TOY, CONFTEST_TOY)
    status, output = project.run()
    assert status == 0, output
    assert "TX " not in (project.results / "pytest.log").read_text(encoding="utf-8")
