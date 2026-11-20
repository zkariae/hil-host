"""Tests of hil_host/config.py on the example configuration of examples/, without a board.

The example configuration and bench file load as they are. Each refused case
starts from a copy of one of them, written in tmp_path with one change (root
of a configuration copy points back to this repository), and checks that
ConfigError names the file and the key:
  - the example: protocol, families in order, values of the bench and the
    image, absolute paths under root, the build read by build_id and the
    names read by command_names;
  - the optional keys: build_id null, no command_names, the project keys of
    the bench file, delays of 0, each form of expected_build;
  - the refused configuration and bench files, one key at a time;
  - a toy protocol registered by a test, and expand() of {python} and {ids}.

Not tested here: the folders and plans of the families (tests of suites.py),
the content of the catalogue (tests of catalog.py) and the serial port
(tests/test_link.py).

Version: 1.0
Date: 20/11/2026
Author: Z. BEN-AKKA
History:
  1.0  20/11/2026  Initial version.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

from hil_host import protocol
from hil_host.config import Config, ConfigError, expand, load, load_bench
from hil_host.fake.hil1 import banner
from hil_host.protocol import BenchProtocol, register

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "hil_host.yaml"
BENCH = ROOT / "examples" / "bench.yaml"


def write_config(tmp_path: Path, change=None) -> Path:
    """A copy of the example configuration, root pointing back here, with one change."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["root"] = str(ROOT)
    if change:
        change(data)
    path = tmp_path / "hil_host.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_bench(tmp_path: Path, change=None) -> Path:
    """A copy of the example bench file with one change."""
    data = yaml.safe_load(BENCH.read_text(encoding="utf-8"))
    if change:
        change(data)
    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def script_family(data: dict) -> dict:
    return next(family for family in data["families"] if family["kind"] == "script")


# The example

def test_example_loads():
    config = load(EXAMPLE)
    assert isinstance(config, Config)
    assert config.root == ROOT
    assert config.protocol == "hil-1"
    assert [(family.id, family.kind) for family in config.families] == [
        ("pytest", "pytest"), ("lamp", "script"), ("scenarios", "scenario")]
    assert (config.bench.startup, config.bench.response) == (5.0, 2.0)
    assert config.bench.serial["baudrate"] == 115200
    assert config.image.name == "fake-bench"
    assert config.image.timeout == 60.0
    assert config.image.abnormal_resets == ("IWDG", "WWDG", "BOR", "LPWR")
    script = config.families[1]
    assert (script.plan_key, script.leaves_image) == ("tests", False)
    assert script.command[-1] == "{ids}"


def test_example_paths_are_absolute_under_root():
    config = load(EXAMPLE)
    script, scenario = config.families[1], config.families[2]
    paths = [config.bench_file, config.catalog, config.results_dir, config.image.build_file,
             config.command_names.file, config.families[0].path, script.plan_file, scenario.path]
    for path in paths:
        assert path.is_absolute()
        assert path.is_relative_to(ROOT)
    assert config.bench_file == ROOT / "examples" / "bench.yaml"
    assert config.catalog == ROOT / "examples" / "commands.yaml"


def test_example_build_id_reads_the_build_of_the_fake_firmware():
    image = load(EXAMPLE).image
    build = image.build_pattern.search(image.build_file.read_text(encoding="utf-8")).group(1)
    assert build == re.search(r"build=(\S+)", banner()).group(1)


def test_example_command_names_read_the_catalogue_in_order():
    config = load(EXAMPLE)
    names = config.command_names.pattern.findall(config.command_names.file.read_text(encoding="utf-8"))
    assert names == list(yaml.safe_load(config.catalog.read_text(encoding="utf-8"))["commands"])


# Optional keys

def test_build_id_null_and_no_command_names(tmp_path):
    def change(data):
        data["image"]["build_id"] = None
        del data["command_names"]

    config = load(write_config(tmp_path, change))
    assert (config.image.build_file, config.image.build_pattern) == (None, None)
    assert config.command_names is None


def test_project_keys_of_the_bench_file_are_ignored(tmp_path):
    bench = load_bench(write_bench(tmp_path, lambda data: data.update(hardware={"board": "any"}, results_dir="x")))
    assert bench == load_bench(BENCH)


def test_delays_of_zero_are_accepted(tmp_path):
    def change(data):
        data["delays"] = {"port_open_settle": 0, "baud_switch_settle": 0, "extra_byte_wait": 0}

    bench = load_bench(write_bench(tmp_path, change))
    assert (bench.port_open_settle, bench.baud_switch_settle, bench.extra_byte_wait) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("value", ["auto", "abc1234", None], ids=["auto", "a-build", "null"])
def test_expected_build_forms(tmp_path, value):
    bench = load_bench(write_bench(tmp_path, lambda data: data["firmware"].update(expected_build=value)))
    assert bench.expected_build == value


# Refused configuration files

CONFIG_CASES = [
    ("schema-version", lambda d: d.update(schema_version=2), "schema_version"),
    ("unknown-key", lambda d: d.update(colour="red"), "colour"),
    ("unknown-image-key", lambda d: d["image"].update(flash_speed=4), "image.flash_speed"),
    ("unknown-family-key", lambda d: d["families"][0].update(plan={"file": "x", "key": "y"}), "families.pytest.plan"),
    ("missing-protocol", lambda d: d.pop("protocol"), "protocol"),
    ("missing-reset-sw", lambda d: d["image"].pop("reset_sw"), "image.reset_sw"),
    ("root-not-a-folder", lambda d: d.update(root=str(ROOT / "README-absent")), "root"),
    ("bench-absent", lambda d: d.update(bench="examples/absent.yaml"), "bench"),
    ("catalog-absent", lambda d: d.update(catalog="examples/absent.yaml"), "catalog"),
    ("protocol-unknown", lambda d: d.update(protocol="toy"), "protocol 'toy'"),
    ("timeout-zero", lambda d: d["image"].update(timeout=0), "image.timeout"),
    ("timeout-bool", lambda d: d["image"].update(timeout=True), "image.timeout"),
    ("command-not-a-list", lambda d: d["image"].update(flash="flash the board"), "image.flash"),
    ("command-empty", lambda d: d["image"].update(reset=[]), "image.reset"),
    ("command-with-a-number", lambda d: d["image"].update(reset_sw=["{python}", 3]), "image.reset_sw"),
    ("pattern-without-group", lambda d: d["image"]["build_id"].update(pattern="build"), "image.build_id.pattern"),
    ("pattern-not-compiling", lambda d: d["command_names"].update(pattern="(["), "command_names.pattern"),
    ("same-family-id", lambda d: d["families"].append(dict(d["families"][0])), "families.pytest"),
    ("unknown-kind", lambda d: d["families"][0].update(kind="robot"), "families.pytest.kind"),
    ("before-not-a-list", lambda d: script_family(d).update(before="flash"), "families.lamp.before"),
    ("leaves-image-not-bool", lambda d: script_family(d).update(leaves_image="no"), "families.lamp.leaves_image"),
]


@pytest.mark.parametrize("change, key", [case[1:] for case in CONFIG_CASES], ids=[case[0] for case in CONFIG_CASES])
def test_config_refused(tmp_path, change, key):
    with pytest.raises(ConfigError) as caught:
        load(write_config(tmp_path, change))
    assert str(caught.value).startswith("hil_host.yaml: ")
    assert key in str(caught.value)


# Refused bench files

BENCH_CASES = [
    ("schema-version", lambda d: d.update(schema_version=2), "schema_version"),
    ("serial-missing", lambda d: d.pop("serial"), "serial"),
    ("port-empty", lambda d: d["serial"].update(port=""), "serial.port"),
    ("baudrate-zero", lambda d: d["serial"].update(baudrate=0), "serial.baudrate"),
    ("baudrate-bool", lambda d: d["serial"].update(baudrate=True), "serial.baudrate"),
    ("parity-mark", lambda d: d["serial"].update(parity="mark"), "serial.parity"),
    ("flow-control-hardware", lambda d: d["serial"].update(flow_control="hardware"), "serial.flow_control"),
    ("startup-zero", lambda d: d["timeouts"].update(startup=0), "timeouts.startup"),
    ("delay-negative", lambda d: d["delays"].update(extra_byte_wait=-0.1), "delays.extra_byte_wait"),
    ("expected-build-number", lambda d: d["firmware"].update(expected_build=1234), "firmware.expected_build"),
    ("accept-mismatch-text", lambda d: d["firmware"].update(accept_build_mismatch="yes"),
     "firmware.accept_build_mismatch"),
]


@pytest.mark.parametrize("change, key", [case[1:] for case in BENCH_CASES], ids=[case[0] for case in BENCH_CASES])
def test_bench_refused(tmp_path, change, key):
    with pytest.raises(ConfigError) as caught:
        load_bench(write_bench(tmp_path, change))
    assert str(caught.value).startswith("bench.yaml: ")
    assert key in str(caught.value)


# Other cases

class ToyProtocol(BenchProtocol):
    """A protocol with no behaviour: only its name matters to the configuration."""

    name = "toy"

    def wait_ready(self, reset, timeout, settle):
        raise NotImplementedError

    def identify(self):
        raise NotImplementedError

    def commands(self):
        return []

    def request(self, command, args, timeout=None):
        raise NotImplementedError

    def watch(self, line):
        return None


def test_registered_protocol_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(protocol, "_REGISTRY", dict(protocol._REGISTRY))  # The registry is restored after the test
    register(ToyProtocol)
    assert load(write_config(tmp_path, lambda data: data.update(protocol="toy"))).protocol == "toy"


def test_expand_fills_python_and_ids():
    assert expand(("{python}", "check.py", "--only", "{ids}", "--trace"), ["L1", "L2"]) == [
        sys.executable, "check.py", "--only", "L1", "L2", "--trace"]


def test_expand_without_ids_drops_the_ids_placeholder():
    assert expand(["{python}", "check.py", "{ids}"]) == [sys.executable, "check.py"]


def test_unreadable_file(tmp_path):
    with pytest.raises(ConfigError, match="absent.yaml"):
        load(tmp_path / "absent.yaml")


def test_file_not_a_mapping(tmp_path):
    path = tmp_path / "hil_host.yaml"
    path.write_text("- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a mapping"):
        load(path)
