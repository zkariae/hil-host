"""pytest plugin: project configuration, log file and the board fixtures.

Loaded by the conftest.py of a project:

    pytest_plugins = ["hil_host.plugins.board_fixtures"]

Options:
  --hil-config FILE  project configuration, relative to the current folder
                     (default: the ini option hil_config, relative to the
                     ini file)
  --port PORT        serial port (default: serial.port in the bench file)
  --no-flash         test the image already on the board
An unusable configuration is a usage error. Without any, the tests that need
no board still run and the fixtures of the bench fail. The pytest log file
goes to <results_dir>/pytest.log unless --log-file is given.

The board fixture is shared by the whole session: it flashes the image,
opens the port, waits for the readiness signal, then checks the identity and
the command list, so every test starts from an identified board. The
protocol is the one the configuration names. The fresh_board fixture gives
each test that same board with a live session: the identity is asked before
the test, and the session recovered if that fails, since a previous test may
have reset the board or left a line half sent. quiet is the silence after
which no more record is expected. hil_config and bench_config give the
configuration of the run.

Version: 1.2
Date: 23/11/2026
Author: Z. BEN-AKKA
History:
  1.0  26/10/2026  Initial version.
  1.1  26/10/2026  Add fresh_board and quiet; require the reset-sw target and RX check delays.
  1.2  23/11/2026  Moved from stm32f7508-dk-baremetal tests/host/hil/fixtures/board_fixtures.py, version 1.1; the project configuration replaces --bench-config.
"""

import logging
import time
from pathlib import Path

import pytest

from hil_host import LOGGER
from hil_host.bench.board import Board
from hil_host.bench.link import Link
from hil_host.bench.outcomes import HilOutcome
from hil_host.bench.programmer import Programmer
from hil_host.config import Bench, Config, ConfigError, load
from hil_host.protocol import get

AUTO = "auto"  # expected_build: the build of the flashed image

CONFIG_KEY = pytest.StashKey[Config]()

log = logging.getLogger(f"{LOGGER}.fixtures")


def pytest_addoption(parser):
    group = parser.getgroup("hil", "HIL bench")
    group.addoption("--hil-config", default=None, metavar="FILE",
                    help="project configuration file (default: the ini option hil_config)")
    group.addoption("--port", default=None, help="serial port (default: serial.port in the bench file)")
    group.addoption("--no-flash", action="store_true", help="test the image already on the board")
    parser.addini("hil_config", "project configuration file, relative to the ini file", default="")


def _config_path(config) -> Path | None:
    """The configuration file of this run, None if neither the option nor the ini names one."""
    option = config.getoption("hil_config")
    if option:
        return config.invocation_params.dir / option
    ini = config.getini("hil_config")
    if ini:
        return (config.inipath.parent if config.inipath else config.rootpath) / ini
    return None


def pytest_configure(config):
    path = _config_path(config)
    if path is None:
        return
    try:
        hil_config = load(path)
    except ConfigError as error:
        raise pytest.UsageError(f"project configuration {path}: {error}") from None
    config.stash[CONFIG_KEY] = hil_config
    if not config.getoption("log_file", None):
        hil_config.results_dir.mkdir(parents=True, exist_ok=True)
        config.option.log_file = str(hil_config.results_dir / "pytest.log")  # Read by the logging plugin, configured last


@pytest.fixture(scope="session")
def hil_config(pytestconfig) -> Config:
    """The project configuration of this run."""
    if CONFIG_KEY not in pytestconfig.stash:
        pytest.fail("no project configuration: give --hil-config or the ini option hil_config", pytrace=False)
    return pytestconfig.stash[CONFIG_KEY]


@pytest.fixture(scope="session")
def bench_config(hil_config) -> Bench:
    """The bench file of this run."""
    return hil_config.bench


def _expected_build(bench: Bench, programmer: Programmer) -> str | None:
    """Build the firmware must report: the flashed image's for auto, else the configured value."""
    if bench.expected_build != AUTO:
        return bench.expected_build
    build = programmer.build_id()
    if build is None:
        log.warning("expected_build auto: no build of the image found, build not checked")
    return build


@pytest.fixture(scope="session")
def board(pytestconfig, hil_config, bench_config) -> Board:
    """An identified board: flashed (unless --no-flash), ready, identity and command list checked."""
    programmer = Programmer(hil_config.image, hil_config.root)
    if not pytestconfig.getoption("no_flash"):
        programmer.flash()

    protocol = get(hil_config.protocol)(bench_config.response)
    with Link.open(bench_config.serial, bench_config.response, protocol.max_pending,
                   pytestconfig.getoption("port")) as link:
        protocol.open(link)
        time.sleep(bench_config.port_open_settle)  # Let the serial adapter settle after opening
        hil_board = Board(protocol, programmer, bench_config.startup, bench_config.port_open_settle,
                          _expected_build(bench_config, programmer), bench_config.accept_build_mismatch)
        hil_board.start_session()
        hil_board.identify()
        hil_board.capabilities()
        yield hil_board


@pytest.fixture
def fresh_board(board) -> Board:
    """The session board with a live session before the test, and no input left after it."""
    alive = False
    if board.banner is not None:
        try:
            # Reads what is pending: a readiness signal is UNEXPECTED_RESET. The build is not checked again
            alive = board.adapter.identify().ok
        except HilOutcome as error:
            log.warning("session lost after the previous test: %s", error)
    if not alive:
        board.recover()
    yield board
    board.adapter.link.flush_input()


@pytest.fixture(scope="session")
def quiet(bench_config) -> float:
    """Seconds without a received line after which no more record is expected."""
    return bench_config.extra_byte_wait
