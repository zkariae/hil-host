"""Tests of hil_host/core/catalog.py on the example catalogue of examples/, without a board.

  - the example: its commands, the marked and the dependent ones, the names
    read by command_names, equal to the catalogue;
  - the merge with the command list of the firmware: typed, free and absent;
  - the form checks: every argument type, both ends of each range, the
    defaults, the optional arguments;
  - the naming rules come from the protocol class: those of hil-1, then
    those of a toy protocol;
  - broken catalogues written in tmp_path: CatalogError names the faulty
    field, also for a file that is not UTF-8, keys of several types and a
    list as an argument type.

Not tested here: the checks between arguments, which stay with the firmware,
the comparison of a catalogue with the names of a real firmware and sending
its commands to a board, which are tests of the project.

Version: 1.3
Date: 25/11/2026
Author: Z. BEN-AKKA
History:
  1.0  09/11/2026  Initial version.
  1.1  20/11/2026  LCD_CIRCLE also requires LCD_INIT (protocol 1.4).
  1.2  20/11/2026  LCD_LINE also requires LCD_INIT (protocol 1.4).
  1.3  25/11/2026  Moved from stm32f7508-dk-baremetal tests/host/tui/tests/test_catalog.py, version 1.2.
"""

import re
from pathlib import Path

import pytest

from hil_host import config
from hil_host.core.catalog import ArgumentError, CatalogError, EntryStatus, default_text, firmware_names, load
from hil_host.protocol import BenchProtocol, get

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CONFIG = config.load(EXAMPLES / "hil_host.yaml")
HIL1 = get("hil-1")
CATALOGUE = load(CONFIG.catalog, HIL1)

COMMANDS = ["HELLO", "CAPS", "REBOOT", "LAMP_SET", "LAMP_GET", "MEM_READ", "MEM_ERASE", "MEM_WRITE", "PIN_READ",
            "SCREEN_INIT", "SCREEN_FILL"]


class Toy:
    """Naming rules other than those of hil-1."""

    command_name = re.compile(r"[a-z]{1,4}")
    reserved_args = ("seq",)


# The example

def test_example_commands_in_file_order():
    assert CATALOGUE.names() == COMMANDS


def test_marked_commands_ask_for_confirmation():
    marked = {name: command.effect for name, command in CATALOGUE.commands.items() if command.needs_confirmation}
    assert marked == {"REBOOT": "reboots", "MEM_ERASE": "destructive", "MEM_WRITE": "destructive"}


def test_command_that_requires_another():
    requiring = {name: command.requires for name, command in CATALOGUE.commands.items() if command.requires}
    assert requiring == {"SCREEN_FILL": "SCREEN_INIT"}


def test_command_fields():
    command = CATALOGUE.commands["MEM_READ"]
    assert (command.name, command.group, command.spec, command.effect) == ("MEM_READ", "Memory", "example", None)
    assert command.summary == "Read 1 to 32 bytes of the memory"
    assert [(arg.name, arg.type, arg.required, arg.default) for arg in command.args] == [
        ("addr", "hex", True, 0x1000), ("len", "int", True, 4)]
    assert command.args[1].params == {"min": 1, "max": 32}
    assert not CATALOGUE.commands["CAPS"].args[0].required


# Names declared by the firmware: C1

def test_firmware_names_of_the_example():
    assert firmware_names(CONFIG.command_names) == COMMANDS


def test_firmware_names_reads_the_group_of_the_pattern(tmp_path):
    header = tmp_path / "commands.h"
    header.write_text(
        '#define BENCH_CMD_LAMP "LAMP_SET"\n'
        '#  define BENCH_CMD_PIN   "PIN_READ"\n'
        "#define BENCH_CMD_NAME_MAX_LEN 32u\n"
        '#define BENCH_KEY_ID "id"\n')
    pattern = re.compile(r'^\s*#\s*define\s+BENCH_CMD_\w+\s+"([^"]*)"', re.MULTILINE)
    assert firmware_names(config.CommandNames(header, pattern)) == ["LAMP_SET", "PIN_READ"]


# Merge with the command list

def test_merge_of_the_same_commands_gives_typed_forms():
    entries = CATALOGUE.merge(COMMANDS)
    assert [entry.name for entry in entries] == COMMANDS
    assert all(entry.status is EntryStatus.TYPED for entry in entries)
    assert all(entry.command is CATALOGUE.commands[entry.name] for entry in entries)


def test_merge_free_and_absent():
    supported = ["NEW_CMD"] + COMMANDS[:0:-1]  # Its own order, without HELLO
    entries = CATALOGUE.merge(supported)
    assert [entry.name for entry in entries] == supported + ["HELLO"]
    assert (entries[0].status, entries[0].command) == (EntryStatus.FREE, None)
    assert all(entry.status is EntryStatus.TYPED for entry in entries[1:-1])
    assert (entries[-1].status, entries[-1].command) == (EntryStatus.ABSENT, CATALOGUE.commands["HELLO"])


def test_merge_without_command_list():
    assert [(entry.name, entry.status) for entry in CATALOGUE.merge([])] == [
        (name, EntryStatus.ABSENT) for name in COMMANDS]


# Form checks

@pytest.mark.parametrize("name", COMMANDS)
def test_defaults_are_accepted(name):
    command = CATALOGUE.commands[name]
    values = {arg.name: default_text(arg) for arg in command.args}
    assert all(values.values())  # Every argument of the example has a default
    assert CATALOGUE.check(name, values) == values


@pytest.mark.parametrize("name, values", [
    ("LAMP_SET", {"state": "off"}),
    ("MEM_READ", {"addr": "0x0", "len": "1"}),
    ("MEM_READ", {"addr": "0x0000ffff", "len": "32"}),
    ("MEM_READ", {"addr": "0x0000FfFf", "len": "10"}),
    ("MEM_ERASE", {"addr": "0x00001F00"}),
    ("MEM_ERASE", {"addr": "0x1000"}),
    ("MEM_WRITE", {"addr": "0x1FFF", "data": "ab"}),
    ("MEM_WRITE", {"addr": "0x1000", "data": "Ab" * 32}),
    ("PIN_READ", {"port": "D", "pin": "15"}),
    ("PIN_READ", {"port": "A", "pin": "0"}),
    ("SCREEN_FILL", {"color": "0xffff"}),
    ("SCREEN_FILL", {"color": "0x0"}),
], ids=["enum", "one_digit", "lowercase_hex", "mixed_case_hex", "last_block", "first_block", "last_byte",
        "max_data", "last_port_pin", "first_port_pin", "max_color", "short_color"])
def test_check_accepts(name, values):
    assert CATALOGUE.check(name, values) == values  # Sent as typed


def test_check_gives_the_catalogue_order():
    assert list(CATALOGUE.check("MEM_READ", {"len": "4", "addr": "0x0"})) == ["addr", "len"]


def test_empty_optional_argument_is_left_out():
    assert CATALOGUE.check("CAPS", {"start": "  "}) == {}
    assert CATALOGUE.check("CAPS", {"start": None}) == {}
    assert CATALOGUE.check("CAPS", {}) == {}


def test_values_are_stripped():
    assert CATALOGUE.check("CAPS", {"start": " 8 "}) == {"start": "8"}


@pytest.mark.parametrize("name, values, arg, message", [
    ("LAMP_SET", {"state": "ON"}, "state", "one of on, off, toggle"),
    ("LAMP_SET", {}, "state", "required"),
    ("LAMP_SET", {"state": " "}, "state", "required"),
    ("LAMP_SET", {"state": "on", "level": "1"}, "level", "not an argument of LAMP_SET"),
    ("MEM_READ", {"addr": "0x0", "len": "04"}, "len", "unsigned decimal without leading zeros"),
    ("MEM_READ", {"addr": "0x0", "len": "-1"}, "len", "unsigned decimal without leading zeros"),
    ("MEM_READ", {"addr": "0x0", "len": "0"}, "len", "from 1 to 32"),
    ("MEM_READ", {"addr": "0x0", "len": "33"}, "len", "from 1 to 32"),
    ("MEM_READ", {"addr": "1000", "len": "4"}, "addr", "0x followed by 1 to 8 hexadecimal digits"),
    ("MEM_READ", {"addr": "0X1000", "len": "4"}, "addr", "0x followed by 1 to 8 hexadecimal digits"),
    ("MEM_READ", {"addr": "0x", "len": "4"}, "addr", "0x followed by 1 to 8 hexadecimal digits"),
    ("MEM_READ", {"addr": "0x000001000", "len": "4"}, "addr", "0x followed by 1 to 8 hexadecimal digits"),
    ("MEM_READ", {"addr": "0x00010000", "len": "4"}, "addr", "from 0x00000000 to 0x0000FFFF"),
    ("MEM_ERASE", {"addr": "0x00001010"}, "addr", "a multiple of 256"),
    ("MEM_ERASE", {"addr": "0x00000F00"}, "addr", "from 0x00001000 to 0x00001F00"),
    ("MEM_WRITE", {"addr": "0x1000", "data": "ABC"}, "data", "an even number of hexadecimal digits"),
    ("MEM_WRITE", {"addr": "0x1000", "data": "0G"}, "data", "an even number of hexadecimal digits"),
    ("MEM_WRITE", {"addr": "0x1000", "data": "00" * 33}, "data", "1 to 32 bytes"),
    ("PIN_READ", {"port": "E", "pin": "1"}, "port", "one uppercase letter from A to D"),
    ("PIN_READ", {"port": "a", "pin": "1"}, "port", "one uppercase letter from A to D"),
    ("PIN_READ", {"port": "AB", "pin": "1"}, "port", "one uppercase letter from A to D"),
    ("PIN_READ", {"port": "A", "pin": "16"}, "pin", "from 0 to 15"),
    ("SCREEN_FILL", {"color": "0x10000"}, "color", "RGB565 colour: 0x followed by 1 to 4 hexadecimal digits"),
    ("SCREEN_FILL", {"color": "F800"}, "color", "RGB565 colour: 0x followed by 1 to 4 hexadecimal digits"),
], ids=["uppercase_enum", "missing", "blank", "unknown_argument", "leading_zero", "negative", "len_zero",
        "len_too_big", "no_prefix", "uppercase_prefix", "no_digit", "nine_digits", "beyond_memory", "not_aligned",
        "below_scratch", "odd_digits", "bad_digit", "too_many_bytes", "port_after_d", "lowercase_port",
        "two_letters", "pin_too_big", "color_too_big", "color_no_prefix"])
def test_check_rejects(name, values, arg, message):
    with pytest.raises(ArgumentError) as error:
        CATALOGUE.check(name, values)
    assert (error.value.arg, error.value.message) == (arg, message)
    assert str(error.value) == f"{arg}: {message}"


def test_check_unknown_command():
    with pytest.raises(KeyError):
        CATALOGUE.check("NO_SUCH_COMMAND", {})


# Schema: the loader names the faulty field

BASE_ENTRY = """\
  LAMP_SET:
    group: Lamp
    summary: Switch the lamp
    spec: "6.4"
    args:
      state: {type: enum, values: ["on", "off"]}
"""
VALUES = 'values: ["on", "off"]'


def write_catalogue(tmp_path, entries=BASE_ENTRY, version=1):
    path = tmp_path / "commands.yaml"
    path.write_text(f"schema_version: {version}\ncommands:\n{entries}", encoding="utf-8")
    return path


def entry_with(arg: str, name: str = "CMD", arg_name: str = "n") -> str:
    return f'  {name}:\n    group: G\n    summary: s\n    spec: "6"\n    args:\n      {arg_name}: {arg}\n'


def refused(path, protocol=HIL1) -> str:
    """The message of the CatalogError."""
    with pytest.raises(CatalogError) as error:
        load(path, protocol)
    return str(error.value)


def test_minimal_catalogue_loads(tmp_path):
    assert load(write_catalogue(tmp_path), HIL1).names() == ["LAMP_SET"]


@pytest.mark.parametrize("arg, text", [
    ("{type: int, min: 0, max: 9, default: 5}", "5"),
    ("{type: hex, digits: [1, 4], min: 0, max: 0xFF, default: 0xab}", "0x00AB"),
    ("{type: hexdata, bytes: [1, 4], default: \"abcd\"}", "ABCD"),
    ("{type: letter, min: A, max: K, default: C}", "C"),
    ("{type: color, default: 0x1f}", "0x001F"),
    ("{type: color}", ""),
], ids=["int", "hex", "hexdata", "letter", "color", "none"])
def test_default_text(tmp_path, arg, text):
    catalogue = load(write_catalogue(tmp_path, entry_with(arg)), HIL1)
    assert default_text(catalogue.commands["CMD"].args[0]) == text


@pytest.mark.parametrize("old, new, message", [
    (VALUES, "values: [on, off]", "LAMP_SET.state.values: must be a non-empty list of quoted strings"),
    (VALUES, "values: []", "LAMP_SET.state.values: must be a non-empty list of quoted strings"),
    (VALUES, 'values: ["on", "on"]', "LAMP_SET.state.values: duplicate value"),
    ("group: Lamp", "group: Lamp\n    colour: red", "LAMP_SET: unknown fields ['colour']"),
    ("    group: Lamp\n", "", "LAMP_SET: missing fields ['group']"),
    ("summary: Switch the lamp", "summary: 5", "LAMP_SET.summary: must be a non-empty string"),
    ('spec: "6.4"', 'spec: ""', "LAMP_SET.spec: must be a non-empty string"),
    ("type: enum", "type: float", "LAMP_SET.state.type: must be one of enum, int"),
    (VALUES, "default: x", "LAMP_SET.state: missing fields ['values'] for type enum"),
    (VALUES, f"{VALUES}, size: 1", "LAMP_SET.state: unknown fields ['size'] for type enum"),
    (VALUES, f"{VALUES}, default: toggle", "LAMP_SET.state.default: state: one of on, off"),
    ('{type: enum, values: ["on", "off"]}', "enum", "LAMP_SET.state: not a mapping"),
    ("group: Lamp", "group: Lamp\n    effect: dangerous", "LAMP_SET.effect: must be one of destructive, reboots"),
    ("group: Lamp", "group: Lamp\n    requires: LAMP_ON", "LAMP_SET.requires: LAMP_ON is not in the catalogue"),
    ("group: Lamp", "group: Lamp\n    requires: 5", "LAMP_SET.requires: must be a command name"),
    ("state:", "State:", "LAMP_SET.State: invalid argument name"),
], ids=["bare_on_off", "no_value", "duplicate_value", "unknown_field", "missing_field", "summary_not_text",
        "empty_spec", "unknown_type", "missing_parameter", "unknown_parameter", "default_not_in_values",
        "argument_not_mapping", "bad_effect", "unknown_requires", "requires_not_text", "uppercase_argument"])
def test_load_rejects(tmp_path, old, new, message):
    assert old in BASE_ENTRY
    assert refused(write_catalogue(tmp_path, BASE_ENTRY.replace(old, new))).startswith(message)


@pytest.mark.parametrize("arg, message", [
    ("{type: int, min: 1, max: 5, default: 6}", "CMD.n.default: n: from 1 to 5"),
    ("{type: int, min: 5, max: 1}", "CMD.n: min and max must be integers"),
    ("{type: int, min: -1, max: 1}", "CMD.n: min and max must be integers"),
    ("{type: int, min: false, max: 1}", "CMD.n: min and max must be integers"),
    ("{type: int, min: 0, max: 9, default: true}", "CMD.n.default: wrong value type for int"),
    ("{type: hex, digits: [1, 2], min: 0, max: 0x100}", "CMD.n.max: does not fit in 2 digits"),
    ("{type: hex, digits: [2, 1], min: 0, max: 0xF}", "CMD.n.digits: must be [min, max]"),
    ("{type: hex, digits: 2, min: 0, max: 0xF}", "CMD.n.digits: must be [min, max]"),
    ("{type: hex, digits: [1, 8], min: 0, max: 0xFF, multiple_of: 0}", "CMD.n.multiple_of: must be a positive"),
    ("{type: hexdata, bytes: [0, 32]}", "CMD.n.bytes: must be [min, max]"),
    ("{type: hexdata, bytes: [1, 4], default: \"ABC\"}", "CMD.n.default: n: an even number"),
    ("{type: letter, min: K, max: A}", "CMD.n: min is after max"),
    ("{type: letter, min: a, max: k}", "CMD.n: min and max must be uppercase letters"),
    ("{type: letter, min: A, max: K, default: 5}", "CMD.n.default: wrong value type for letter"),
    ("{type: color, default: 0x10000}", "CMD.n.default: n: RGB565 colour"),
    ("{type: color, default: \"0xF800\"}", "CMD.n.default: wrong value type for color"),
    ("{type: int, min: 0, max: 9, required: maybe}", "CMD.n.required: must be true or false"),
], ids=["default_out_of_range", "min_above_max", "negative_min", "min_boolean", "default_boolean",
        "max_beyond_digits", "digits_reversed", "digits_not_pair", "zero_step", "zero_bytes", "default_odd_digits",
        "letters_reversed", "lowercase_letters", "letter_default_number", "color_too_big", "default_as_text",
        "required_not_boolean"])
def test_load_rejects_argument(tmp_path, arg, message):
    assert refused(write_catalogue(tmp_path, entry_with(arg))).startswith(message)


@pytest.mark.parametrize("entries, message", [
    ("  LAMP_SET:\n", "LAMP_SET: not a mapping"),
    ("  5: {}\n", "5: invalid command name"),
    (BASE_ENTRY.split("    args")[0] + "    args: []\n", "LAMP_SET.args: must be a mapping"),
], ids=["no_entry", "name_not_text", "args_not_mapping"])
def test_load_rejects_entry(tmp_path, entries, message):
    assert refused(write_catalogue(tmp_path, entries)) == message


@pytest.mark.parametrize("text, message", [
    ("- a\n", "not a mapping"),
    ("schema_version: 2\ncommands: {}\n", "schema_version must be 1"),
    ("commands: {}\n", "schema_version must be 1"),
    ("schema_version: 1\n", "commands must be a non-empty mapping"),
    ("schema_version: 1\ncommands: {}\n", "commands must be a non-empty mapping"),
    ("schema_version: 1\ncommands: {}\nextra: 1\n", "unknown keys ['extra']"),
], ids=["list", "other_version", "no_version", "no_commands", "empty_commands", "unknown_key"])
def test_load_rejects_file(tmp_path, text, message):
    path = tmp_path / "commands.yaml"
    path.write_text(text, encoding="utf-8")
    assert refused(path) == f"{path}: {message}"


def test_load_rejects_missing_file_and_bad_yaml(tmp_path):
    path = tmp_path / "commands.yaml"
    assert refused(path).startswith(f"{path}: ")
    path.write_text("commands: [", encoding="utf-8")
    assert refused(path).startswith(f"{path}: ")


# Files that once ended in a Python error

def test_load_rejects_a_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "commands.yaml"
    path.write_bytes(b"schema_version: 1\ncommands:\n  \xff\xfe: {}\n")
    message = refused(path)
    assert message.startswith(f"{path}: ") and "utf-8" in message


def test_load_rejects_unknown_keys_of_several_types(tmp_path):
    path = write_catalogue(tmp_path, BASE_ENTRY + "1: x\nextra: y\n")
    assert refused(path) == f"{path}: unknown keys ['1', 'extra']"


def test_load_rejects_unknown_fields_of_several_types(tmp_path):
    path = write_catalogue(tmp_path, BASE_ENTRY.replace("    group", "    1: x\n    colour: red\n    group"))
    assert refused(path) == "LAMP_SET: unknown fields ['1', 'colour']"


def test_load_rejects_unknown_parameters_of_several_types(tmp_path):
    path = write_catalogue(tmp_path, entry_with("{type: color, 1: x, size: 1}"))
    assert refused(path) == "CMD.n: unknown fields ['1', 'size'] for type color"


def test_load_rejects_a_list_as_type(tmp_path):
    path = write_catalogue(tmp_path, entry_with("{type: [int], min: 0, max: 1}"))
    assert refused(path).startswith("CMD.n.type: must be one of")


# Naming rules of the protocol

@pytest.mark.parametrize("name, arg_name, message", [
    ("A" * 32, "n", None),
    ("A" * 33, "n", f"{'A' * 33}: invalid command name"),
    ("lamp", "n", "lamp: invalid command name"),
    ("LAMP", "id", "LAMP.id: invalid argument name"),
    ("LAMP", "command", "LAMP.command: invalid argument name"),
    ("LAMP", "seq", None),
], ids=["longest_name", "name_too_long", "lowercase_name", "reserved_id", "reserved_command", "free_seq"])
def test_naming_rules_of_hil1(tmp_path, name, arg_name, message):
    path = write_catalogue(tmp_path, entry_with("{type: color}", name, arg_name))
    if message is None:
        assert load(path, HIL1).names() == [name]
    else:
        assert refused(path) == message


def test_command_name_is_a_text_whatever_the_protocol(tmp_path):
    path = write_catalogue(tmp_path, entry_with("{type: color}", name="5"))
    assert refused(path, BenchProtocol) == "5: invalid command name"  # Its pattern accepts any text


@pytest.mark.parametrize("name, arg_name, message", [
    ("lamp", "n", None),
    ("lamp", "id", None),
    ("lamp", "command", None),
    ("lamps", "n", "lamps: invalid command name"),
    ("LAMP", "n", "LAMP: invalid command name"),
    ("lamp", "seq", "lamp.seq: invalid argument name"),
], ids=["name", "free_id", "free_command", "name_too_long", "uppercase_name", "reserved_seq"])
def test_naming_rules_of_another_protocol(tmp_path, name, arg_name, message):
    path = write_catalogue(tmp_path, entry_with("{type: color}", name, arg_name))
    if message is None:
        assert load(path, Toy).names() == [name]
    else:
        assert refused(path, Toy) == message
