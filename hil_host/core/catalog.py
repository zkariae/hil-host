"""Command catalogue of the TUI: loading, merge with the command list, argument checks.

The firmware lists its command names only; the catalogue of the project (the
catalog key of its configuration) tells how to ask for each command. This
module is the only place that knows the argument types, so ui/ holds no
command name (C1, C5). examples/commands.yaml describes the format. The
project writes its catalogue from its protocol specification, never from the
firmware.

The protocol class gives its naming rules: command_name for the commands and
reserved_args for the argument names a command may not use. firmware_names()
reads the names the firmware declares (the command_names key of the
configuration), for the test of a project that compares them with its
catalogue.

Checks between arguments (an address plus a length) stay with the firmware.
The free form, used to send a deliberately invalid value, does not go through
Catalog.check.

No Textual or pyserial import.

Version: 1.1
Date: 25/11/2026
Author: Z. BEN-AKKA
History:
  1.0  09/11/2026  Initial version.
  1.1  25/11/2026  Moved from stm32f7508-dk-baremetal tests/host/tui/core/catalog.py, version 1.0.
"""

import enum
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hil_host.config import CommandNames

SCHEMA_VERSION = 1

_COMMAND_FIELDS = {"group", "summary", "spec", "effect", "requires", "args"}
_COMMAND_REQUIRED = {"group", "summary", "spec", "args"}
_EFFECTS = ("destructive", "reboots")
_ARG_COMMON = {"type", "default", "required"}

# type: (required parameters, optional parameters)
_TYPES = {
    "enum": ({"values"}, set()),
    "int": ({"min", "max"}, set()),
    "hex": ({"digits", "min", "max"}, {"multiple_of"}),
    "hexdata": ({"bytes"}, set()),
    "letter": ({"min", "max"}, set()),
    "color": (set(), set()),
}

_ARG_NAME = re.compile(r"[a-z][a-z0-9_]*")
_DECIMAL = re.compile(r"0|[1-9][0-9]*")  # No leading zeros
_HEX = re.compile(r"0x([0-9A-Fa-f]+)")  # Lowercase prefix, either digit case
_HEXDATA = re.compile(r"[0-9A-Fa-f]+")
_COLOR = re.compile(r"0x[0-9A-Fa-f]{1,4}")
_LETTER = re.compile(r"[A-Z]")


class CatalogError(Exception):
    """The catalogue file is missing, unreadable or breaks the schema."""


class ArgumentError(Exception):
    """A value typed in a form breaks the rules of its argument."""

    def __init__(self, arg: str, message: str):
        super().__init__(f"{arg}: {message}")
        self.arg = arg
        self.message = message


class EntryStatus(enum.Enum):
    """How a command is shown, from the command list of the firmware and the catalogue."""

    TYPED = "typed"  # In both: typed form
    FREE = "free"  # In the command list only: free key=value form
    ABSENT = "absent"  # In the catalogue only: grey, not in this firmware


@dataclass(frozen=True)
class Arg:
    """One argument of a command, already checked against the schema."""

    name: str
    type: str
    required: bool
    default: object
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Command:
    """One catalogue entry, already checked against the schema."""

    name: str
    group: str
    summary: str
    spec: str
    effect: str | None
    requires: str | None
    args: tuple

    @property
    def needs_confirmation(self) -> bool:
        """Destructive or rebooting commands ask before sending."""
        return self.effect is not None


@dataclass(frozen=True)
class Entry:
    """A command as the interface shows it."""

    name: str
    status: EntryStatus
    command: Command | None  # None for FREE


def default_text(arg: Arg) -> str:
    """The default value as typed in a form, or "" without default."""
    value = arg.default
    if value is None:
        return ""
    if arg.type == "hex":
        return f"0x{value:0{arg.params['digits'][1]}X}"
    if arg.type == "color":
        return f"0x{value:04X}"
    if arg.type == "hexdata":
        return str(value).upper()
    return str(value)


def check_value(arg: Arg, text: str) -> str:
    """The value to send, or ArgumentError if it breaks the argument rules."""
    params = arg.params
    if arg.type == "enum":
        if text not in params["values"]:
            raise ArgumentError(arg.name, f"one of {', '.join(params['values'])}")
        return text
    if arg.type == "int":
        if not _DECIMAL.fullmatch(text):
            raise ArgumentError(arg.name, "unsigned decimal without leading zeros")
        _check_range(arg, int(text), str(params["min"]), str(params["max"]))
        return text
    if arg.type == "hex":
        match = _HEX.fullmatch(text)
        low, high = params["digits"]
        if not match or not low <= len(match.group(1)) <= high:
            raise ArgumentError(arg.name, f"0x followed by {low} to {high} hexadecimal digits")
        value = int(match.group(1), 16)
        _check_range(arg, value, f"0x{params['min']:0{high}X}", f"0x{params['max']:0{high}X}")
        step = params.get("multiple_of")
        if step and value % step:
            raise ArgumentError(arg.name, f"a multiple of {step}")
        return text
    if arg.type == "hexdata":
        low, high = params["bytes"]
        if not _HEXDATA.fullmatch(text) or len(text) % 2:
            raise ArgumentError(arg.name, "an even number of hexadecimal digits")
        if not low <= len(text) // 2 <= high:
            raise ArgumentError(arg.name, f"{low} to {high} bytes")
        return text
    if arg.type == "letter":
        if not _LETTER.fullmatch(text) or not params["min"] <= text <= params["max"]:
            raise ArgumentError(arg.name, f"one uppercase letter from {params['min']} to {params['max']}")
        return text
    if arg.type == "color":
        if not _COLOR.fullmatch(text):
            raise ArgumentError(arg.name, "RGB565 colour: 0x followed by 1 to 4 hexadecimal digits")
        return text
    raise ArgumentError(arg.name, f"unknown type {arg.type}")


def _check_range(arg: Arg, value: int, low: str, high: str) -> None:
    if not arg.params["min"] <= value <= arg.params["max"]:
        raise ArgumentError(arg.name, f"from {low} to {high}")


class Catalog:
    """The checked catalogue, in file order."""

    def __init__(self, commands: dict):
        self.commands = commands

    def names(self) -> list:
        return list(self.commands)

    def merge(self, supported: list) -> list:
        """Entries in the order of the firmware's list, then the catalogue commands missing from it."""
        entries = []
        for name in supported:
            command = self.commands.get(name)
            status = EntryStatus.TYPED if command else EntryStatus.FREE
            entries.append(Entry(name, status, command))
        for name, command in self.commands.items():
            if name not in supported:
                entries.append(Entry(name, EntryStatus.ABSENT, command))
        return entries

    def check(self, name: str, values: dict) -> dict:
        """The arguments to send, in catalogue order, from the texts typed in a form.

        An empty optional argument is left out. Raises ArgumentError for a
        missing, unknown or invalid argument, and KeyError for a command that
        is not in the catalogue.
        """
        command = self.commands[name]
        known = {arg.name for arg in command.args}
        for key in values:
            if key not in known:
                raise ArgumentError(key, f"not an argument of {name}")
        result = {}
        for arg in command.args:
            text = (values.get(arg.name) or "").strip()
            if not text:
                if arg.required:
                    raise ArgumentError(arg.name, "required")
                continue
            result[arg.name] = check_value(arg, text)
        return result


def load(path: Path | str, protocol: type) -> Catalog:
    """Read and check the catalogue with the naming rules of the protocol class.

    Raises CatalogError naming the faulty field.
    """
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise CatalogError(f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: not a mapping")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(f"{path}: schema_version must be {SCHEMA_VERSION}")
    unknown = set(data) - {"schema_version", "commands"}
    if unknown:
        raise CatalogError(f"{path}: unknown keys {_sorted(unknown)}")
    raw = data.get("commands")
    if not isinstance(raw, dict) or not raw:
        raise CatalogError(f"{path}: commands must be a non-empty mapping")
    commands = {}
    for name, entry in raw.items():
        commands[name] = _command(name, entry, protocol)
    for command in commands.values():
        if command.requires is not None and command.requires not in commands:
            raise CatalogError(f"{command.name}.requires: {command.requires} is not in the catalogue")
    return Catalog(commands)


def firmware_names(command_names: CommandNames) -> list:
    """The command names the firmware declares, in file order."""
    return command_names.pattern.findall(command_names.file.read_text(encoding="utf-8"))


def _sorted(keys) -> list:
    return sorted(map(str, keys))  # YAML keys may be of several types


def _command(name, entry, protocol: type) -> Command:
    if not isinstance(name, str) or not protocol.command_name.fullmatch(name):
        raise CatalogError(f"{name}: invalid command name")
    if not isinstance(entry, dict):
        raise CatalogError(f"{name}: not a mapping")
    unknown = set(entry) - _COMMAND_FIELDS
    if unknown:
        raise CatalogError(f"{name}: unknown fields {_sorted(unknown)}")
    missing = _COMMAND_REQUIRED - set(entry)
    if missing:
        raise CatalogError(f"{name}: missing fields {sorted(missing)}")
    for key in ("group", "summary", "spec"):
        if not isinstance(entry[key], str) or not entry[key]:
            raise CatalogError(f"{name}.{key}: must be a non-empty string")
    effect = entry.get("effect")
    if effect is not None and effect not in _EFFECTS:
        raise CatalogError(f"{name}.effect: must be one of {', '.join(_EFFECTS)}")
    requires = entry.get("requires")
    if requires is not None and not isinstance(requires, str):
        raise CatalogError(f"{name}.requires: must be a command name")
    raw_args = entry["args"]
    if not isinstance(raw_args, dict):
        raise CatalogError(f"{name}.args: must be a mapping")
    args = tuple(_arg(name, arg_name, spec, protocol) for arg_name, spec in raw_args.items())
    return Command(name, entry["group"], entry["summary"], entry["spec"], effect, requires, args)


def _arg(command: str, name, spec, protocol: type) -> Arg:
    where = f"{command}.{name}"
    if not isinstance(name, str) or not _ARG_NAME.fullmatch(name) or name in protocol.reserved_args:
        raise CatalogError(f"{where}: invalid argument name")
    if not isinstance(spec, dict):
        raise CatalogError(f"{where}: not a mapping")
    kind = spec.get("type")
    if not isinstance(kind, str) or kind not in _TYPES:
        raise CatalogError(f"{where}.type: must be one of {', '.join(_TYPES)}")
    needed, optional = _TYPES[kind]
    params = {key: value for key, value in spec.items() if key not in _ARG_COMMON}
    unknown = set(params) - needed - optional
    if unknown:
        raise CatalogError(f"{where}: unknown fields {_sorted(unknown)} for type {kind}")
    missing = needed - set(params)
    if missing:
        raise CatalogError(f"{where}: missing fields {sorted(missing)} for type {kind}")
    _check_params(where, kind, params)
    required = spec.get("required", True)
    if not isinstance(required, bool):
        raise CatalogError(f"{where}.required: must be true or false")
    arg = Arg(name, kind, required, spec.get("default"), params)
    _check_default(where, arg)
    return arg


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_pair(where: str, value) -> None:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(_is_int(item) for item in value)
        or not 1 <= value[0] <= value[1]
    ):
        raise CatalogError(f"{where}: must be [min, max] with 1 <= min <= max")


def _check_params(where: str, kind: str, params: dict) -> None:
    if kind == "enum":
        values = params["values"]
        # A bare on or off would be a boolean in YAML 1.1
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
            raise CatalogError(f"{where}.values: must be a non-empty list of quoted strings")
        if len(set(values)) != len(values):
            raise CatalogError(f"{where}.values: duplicate value")
    if kind in ("int", "hex"):
        low, high = params["min"], params["max"]
        if not _is_int(low) or not _is_int(high) or not 0 <= low <= high:
            raise CatalogError(f"{where}: min and max must be integers with 0 <= min <= max")
    if kind == "hex":
        _check_pair(f"{where}.digits", params["digits"])
        if params["max"] >= 16 ** params["digits"][1]:
            raise CatalogError(f"{where}.max: does not fit in {params['digits'][1]} digits")
        step = params.get("multiple_of")
        if step is not None and (not _is_int(step) or step < 1):
            raise CatalogError(f"{where}.multiple_of: must be a positive integer")
    if kind == "hexdata":
        _check_pair(f"{where}.bytes", params["bytes"])
    if kind == "letter":
        low, high = params["min"], params["max"]
        for value in (low, high):
            if not isinstance(value, str) or not _LETTER.fullmatch(value):
                raise CatalogError(f"{where}: min and max must be uppercase letters")
        if low > high:
            raise CatalogError(f"{where}: min is after max")


def _check_default(where: str, arg: Arg) -> None:
    if arg.default is None:
        return
    numeric = arg.type in ("int", "hex", "color")
    if numeric != _is_int(arg.default) or (not numeric and not isinstance(arg.default, str)):
        raise CatalogError(f"{where}.default: wrong value type for {arg.type}")
    try:
        check_value(arg, default_text(arg))
    except (ArgumentError, ValueError) as error:
        raise CatalogError(f"{where}.default: {error}") from error
