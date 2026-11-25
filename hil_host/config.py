"""Project configuration of hil-host: the configuration file and the bench file it names, checked.

A project describes its bench in one YAML file (examples/hil_host.yaml),
passed as --config to the TUI and as the ini option hil_config to pytest.
load() reads it and the bench file it names, checks them and returns a
frozen Config whose paths are absolute, resolved from root:

  - schema_version 1; root a folder; the bench file and the catalogue exist;
  - protocol a name of the protocol registry;
  - results_dir a path, not created here;
  - image: name, the flash, reset and reset_sw commands, their timeout,
    build_id (file and pattern, optional) and abnormal_resets;
  - command_names: file and pattern, optional;
  - run_options: the file of the run options, optional, exists;
  - families, in order, with unique ids: pytest (path), script (plan,
    before, command, error_prefixes, leaves_image) or scenario (path).

A key this module does not know is refused in the configuration file, so a
misspelt key is not ignored. load_bench() reads only the keys hil-host uses
in the bench file (serial, timeouts.startup and response, delays,
firmware) and ignores the others, which belong to the project.

A command is a list of strings, one argument per item, run from root;
expand() replaces {python} with the running interpreter and {ids} with the
selected ids, one argument each. A pattern has exactly one group, the value
it reads, and is compiled with re.MULTILINE; its file may not exist yet (a
build file before the first build).

ConfigError names the file and the key. This module imports neither pytest
nor Textual: the plugins and the TUI turn ConfigError into their own error.

Not checked here: the folders and plans of the families (suites.py reports
them per family), the content of the catalogue (catalog.py) and of the run
options file (run_options.py), and the serial port (link.py).

Version: 1.1
Date: 25/11/2026
Author: Z. BEN-AKKA
History:
  1.0  20/11/2026  Initial version, after load_bench_config of tests/host/hil/fixtures/board_fixtures.py 1.1 and the families of tests/host/tui/core/suites.py 1.0 of stm32f7508-dk-baremetal.
  1.1  25/11/2026  run_options key, optional; files that are not UTF-8, unknown keys of several types and a list as a kind raise ConfigError.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from hil_host.protocol import UnknownProtocol, get

SCHEMA_VERSION = 1
PARITIES = ("none", "even", "odd")
FLOW_CONTROLS = ("none", "xonxoff", "rtscts")
SERIAL_KEYS = ("port", "baudrate", "bytesize", "parity", "stopbits", "flow_control")
TOP_KEYS = {"schema_version", "root", "bench", "catalog", "protocol", "results_dir", "image", "command_names",
            "run_options", "families"}
OPTIONAL_KEYS = {"command_names", "run_options"}
IMAGE_KEYS = {"name", "flash", "reset", "reset_sw", "timeout", "build_id", "abnormal_resets"}
SOURCE_KEYS = {"file", "pattern"}
FAMILY_KEYS = {
    "pytest": {"path"},
    "script": {"plan", "before", "command", "error_prefixes", "leaves_image"},
    "scenario": {"path"},
}
PLAN_KEYS = {"file", "key"}
PYTHON, IDS = "{python}", "{ids}"


class ConfigError(ValueError):
    """A configuration or bench file that cannot be used; the message names the file and the key."""


@dataclass(frozen=True)
class Bench:
    """The keys of the bench file that hil-host reads. Durations in seconds."""

    serial: dict  # port, baudrate, bytesize, parity, stopbits, flow_control
    startup: float
    response: float
    port_open_settle: float
    baud_switch_settle: float
    extra_byte_wait: float
    expected_build: str | None  # "auto", a build, or None to skip the check
    accept_build_mismatch: bool


@dataclass(frozen=True)
class Image:
    """The bench image: its commands, their timeout, where its build is read, the abnormal reset causes."""

    name: str
    flash: tuple
    reset: tuple
    reset_sw: tuple
    timeout: float
    build_file: Path | None
    build_pattern: re.Pattern | None
    abnormal_resets: tuple


@dataclass(frozen=True)
class CommandNames:
    """Where the firmware declares its command names."""

    file: Path
    pattern: re.Pattern


@dataclass(frozen=True)
class Family:
    """One test family of the Tests tab; the fields of the other kinds stay empty."""

    id: str
    kind: str  # pytest, script or scenario
    title: str
    path: Path | None = None  # pytest and scenario
    plan_file: Path | None = None  # script
    plan_key: str = ""
    before: tuple = ()  # Commands run first, in order
    command: tuple = ()
    error_prefixes: tuple = ()
    leaves_image: bool = False


@dataclass(frozen=True)
class Config:
    """The checked configuration of a project."""

    path: Path
    root: Path
    bench_file: Path
    bench: Bench
    catalog: Path
    protocol: str
    results_dir: Path
    image: Image
    command_names: CommandNames | None
    run_options: Path | None
    families: tuple


def load(path: Path | str) -> Config:
    """Read and check the configuration file and its bench file."""
    path = Path(path).resolve()
    data = _read(path)
    check = _Checker(path.name)
    check.keys("", data, TOP_KEYS, TOP_KEYS - OPTIONAL_KEYS)
    if data["schema_version"] != SCHEMA_VERSION:
        raise check.error("schema_version", f"must be {SCHEMA_VERSION}")
    root = (path.parent / check.text("root", data["root"])).resolve()
    if not root.is_dir():
        raise check.error("root", f"{root} is not a folder")
    bench_file = check.file(root, "bench", data["bench"])
    catalog = check.file(root, "catalog", data["catalog"])
    protocol = check.text("protocol", data["protocol"])
    try:
        get(protocol)
    except UnknownProtocol as error:
        raise ConfigError(f"{path.name}: {error}") from None  # It names the protocol key and the known names
    results_dir = root / check.text("results_dir", data["results_dir"])
    image = _image(check, root, data["image"])
    names = data.get("command_names")
    command_names = None if names is None else CommandNames(*check.source(root, "command_names", names))
    options = data.get("run_options")
    run_options = None if options is None else check.file(root, "run_options", options)
    families = _families(check, root, data["families"])
    return Config(path, root, bench_file, load_bench(bench_file), catalog, protocol, results_dir, image,
                  command_names, run_options, families)


def load_bench(path: Path | str) -> Bench:
    """Read and check the keys hil-host uses in a bench file; the other keys are ignored."""
    path = Path(path).resolve()
    data = _read(path)
    check = _Checker(path.name)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise check.error("schema_version", f"must be {SCHEMA_VERSION}")
    serial = check.section(data, "serial", SERIAL_KEYS)
    check.text("serial.port", serial["port"])
    for key in ("baudrate", "bytesize", "stopbits"):
        if isinstance(serial[key], bool) or not isinstance(serial[key], int) or serial[key] <= 0:
            raise check.error(f"serial.{key}", "must be a positive integer")
    check.choice("serial.parity", serial["parity"], PARITIES)
    check.choice("serial.flow_control", serial["flow_control"], FLOW_CONTROLS)
    timeouts = check.section(data, "timeouts", ("startup", "response"))
    delays = check.section(data, "delays", ("port_open_settle", "baud_switch_settle", "extra_byte_wait"))
    firmware = check.section(data, "firmware", ("expected_build", "accept_build_mismatch"))
    expected = firmware["expected_build"]
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        raise check.error("firmware.expected_build", "must be auto, a build or null")
    if not isinstance(firmware["accept_build_mismatch"], bool):
        raise check.error("firmware.accept_build_mismatch", "must be true or false")
    return Bench(
        serial={key: serial[key] for key in SERIAL_KEYS},
        startup=check.number("timeouts.startup", timeouts["startup"]),
        response=check.number("timeouts.response", timeouts["response"]),
        port_open_settle=check.number("delays.port_open_settle", delays["port_open_settle"], zero=True),
        baud_switch_settle=check.number("delays.baud_switch_settle", delays["baud_switch_settle"], zero=True),
        extra_byte_wait=check.number("delays.extra_byte_wait", delays["extra_byte_wait"], zero=True),
        expected_build=expected,
        accept_build_mismatch=firmware["accept_build_mismatch"],
    )


def expand(argv, ids=()) -> list:
    """A command with {python} replaced by the running interpreter and {ids} by the ids, one argument each."""
    command = []
    for arg in argv:
        if arg == IDS:
            command.extend(str(test_id) for test_id in ids)
        else:
            command.append(sys.executable if arg == PYTHON else arg)
    return command


# Checks

def _read(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigError(f"{path}: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: not a mapping")
    return data


def _image(check, root: Path, data) -> Image:
    check.keys("image.", data, IMAGE_KEYS, IMAGE_KEYS - {"build_id"})
    build = data.get("build_id")
    build_file, build_pattern = (None, None) if build is None else check.source(root, "image.build_id", build)
    resets = data["abnormal_resets"]
    if not isinstance(resets, list) or not all(isinstance(cause, str) and cause for cause in resets):
        raise check.error("image.abnormal_resets", "must be a list of reset causes")
    return Image(
        name=check.text("image.name", data["name"]),
        flash=check.command("image.flash", data["flash"]),
        reset=check.command("image.reset", data["reset"]),
        reset_sw=check.command("image.reset_sw", data["reset_sw"]),
        timeout=check.number("image.timeout", data["timeout"]),
        build_file=build_file,
        build_pattern=build_pattern,
        abnormal_resets=tuple(resets),
    )


def _families(check, root: Path, data) -> tuple:
    if not isinstance(data, list):
        raise check.error("families", "must be a list")
    families, seen = [], set()
    for index, entry in enumerate(data):
        where = f"families[{index}]"
        if not isinstance(entry, dict):
            raise check.error(where, "must be a mapping")
        family_id = check.text(f"{where}.id", entry.get("id"))
        where = f"families.{family_id}"
        if family_id in seen:
            raise check.error(where, "id used twice")
        seen.add(family_id)
        kind = entry.get("kind")
        if not isinstance(kind, str) or kind not in FAMILY_KEYS:
            raise check.error(f"{where}.kind", f"must be one of {', '.join(FAMILY_KEYS)}")
        keys = {"id", "kind", "title"} | FAMILY_KEYS[kind]
        check.keys(f"{where}.", entry, keys, keys)
        title = check.text(f"{where}.title", entry["title"])
        if kind != "script":
            families.append(Family(family_id, kind, title, path=root / check.text(f"{where}.path", entry["path"])))
            continue
        plan = entry["plan"]
        check.keys(f"{where}.plan.", plan, PLAN_KEYS, PLAN_KEYS)
        before = entry["before"]
        if not isinstance(before, list):
            raise check.error(f"{where}.before", "must be a list of commands")
        prefixes = entry["error_prefixes"]
        if not isinstance(prefixes, list) or not all(isinstance(prefix, str) and prefix for prefix in prefixes):
            raise check.error(f"{where}.error_prefixes", "must be a list of texts")
        if not isinstance(entry["leaves_image"], bool):
            raise check.error(f"{where}.leaves_image", "must be true or false")
        families.append(Family(
            family_id, kind, title,
            plan_file=root / check.text(f"{where}.plan.file", plan["file"]),
            plan_key=check.text(f"{where}.plan.key", plan["key"]),
            before=tuple(check.command(f"{where}.before[{number}]", command) for number, command in enumerate(before)),
            command=check.command(f"{where}.command", entry["command"]),
            error_prefixes=tuple(prefixes),
            leaves_image=entry["leaves_image"],
        ))
    return tuple(families)


class _Checker:
    """Checks of one file; each error names the file and the key."""

    def __init__(self, name: str):
        self.name = name

    def error(self, key: str, message: str) -> ConfigError:
        return ConfigError(f"{self.name}: {key} {message}")

    def keys(self, prefix: str, data, allowed: set, required: set) -> None:
        if not isinstance(data, dict):
            raise self.error(prefix.rstrip(".") or "file", "must be a mapping")
        unknown = sorted(set(data) - allowed, key=str)  # YAML keys may be of several types
        if unknown:
            raise self.error(f"{prefix}{unknown[0]}", "is not a known key")
        missing = sorted(required - set(data))
        if missing:
            raise self.error(f"{prefix}{missing[0]}", "is missing")

    def section(self, data: dict, name: str, keys) -> dict:
        section = data.get(name)
        if not isinstance(section, dict):
            raise self.error(name, "is missing or not a mapping")
        missing = [key for key in keys if key not in section]
        if missing:
            raise self.error(f"{name}.{missing[0]}", "is missing")
        return section

    def text(self, key: str, value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.error(key, "must be a non-empty text")
        return value

    def number(self, key: str, value, zero: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or (value == 0 and not zero):
            raise self.error(key, "must be a positive number" if not zero else "must be a number, 0 or more")
        return float(value)

    def choice(self, key: str, value, choices) -> None:
        if value not in choices:
            raise self.error(key, f"must be one of {' | '.join(choices)}")

    def file(self, root: Path, key: str, value) -> Path:
        path = root / self.text(key, value)
        if not path.is_file():
            raise self.error(key, f"{path} is not a file")
        return path

    def command(self, key: str, value) -> tuple:
        if not isinstance(value, list) or not value or not all(isinstance(arg, str) and arg for arg in value):
            raise self.error(key, "must be a non-empty list of texts, one argument each")
        return tuple(value)

    def source(self, root: Path, key: str, value) -> tuple:
        """(file, compiled pattern) of a file read with a one-group pattern."""
        self.keys(f"{key}.", value, SOURCE_KEYS, SOURCE_KEYS)
        path = root / self.text(f"{key}.file", value["file"])
        try:
            pattern = re.compile(self.text(f"{key}.pattern", value["pattern"]), re.MULTILINE)
        except re.error as error:
            raise self.error(f"{key}.pattern", f"does not compile: {error}") from None
        if pattern.groups != 1:
            raise self.error(f"{key}.pattern", "must have exactly one group")
        return path, pattern
