# @file
#
# Craft, modify or dump variable list (.vl) data using a config schema XML.
#   modify: create or update a .vl file by changing specific fields (knobs, struct members or array elements)
#   dump:   print the fields of the schema with the values of the schema defaults, a .vl file or this system
#
# Copyright (c), Microsoft Corporation
# SPDX-License-Identifier: BSD-2-Clause-Patent

import io
import os
import sys
import csv
import json
import uuid
import shutil
import fnmatch
import logging
import argparse
import tempfile
import subprocess
from collections import OrderedDict

from VariableList import (
    Schema,
    UEFIVariable,
    ParseError,
    InvalidRangeError,
    create_vlist_buffer,
    read_vlist,
)

READ_TOOL_NAME = "ReadUefiVarsToConfVarList"
WRITE_TOOL_NAME = "WriteConfVarListToUefiVars"

# Attributes used when a variable has to be created: NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS
DEFAULT_ATTRIBUTES = 7

# Exit code reported by ReadUefiVarsToConfVarList when it did not find any variable (-1)
READ_TOOL_NO_VARIABLES_CODES = (-1, 255)


class ModifyError(Exception):
    pass


#
# Helpers around the schema
#
def parse_assignment(text):
    """Split '[GUID:]FIELD_PATH=VALUE' into (guid or None, field path, value string)."""
    if "=" not in text:
        raise ModifyError(f"Invalid change '{text}', expected [GUID:]FIELD=VALUE")

    target, value = text.split("=", 1)
    target = target.strip()
    guid = None
    if ":" in target:
        guid_text, target = target.split(":", 1)
        try:
            guid = uuid.UUID(guid_text.strip().strip("{}"))
        except ValueError:
            raise ModifyError(f"Invalid namespace GUID '{guid_text}' in change '{text}'")
        target = target.strip()

    if not target:
        raise ModifyError(f"Invalid change '{text}', the field name is empty")

    return guid, target, value.strip()


def find_subknob(schema, guid, path):
    """Find the one field (SubKnob) in the schema matching the path and the optional namespace."""
    matches = [
        subknob
        for subknob in schema.subknobs
        if subknob.name == path and (guid is None or uuid.UUID(subknob.knob.namespace) == guid)
    ]

    if not matches:
        raise ModifyError(f"Field '{path}' is not defined in the schema" + (f" (namespace {guid})" if guid else ""))

    if len(matches) > 1:
        namespaces = ", ".join(str(m.knob.namespace) for m in matches)
        raise ModifyError(
            f"Field '{path}' is defined in more than one namespace ({namespaces}), "
            f"use the GUID:{path}=VALUE form to select one"
        )

    return matches[0]


def knob_key(knob):
    return (uuid.UUID(knob.namespace), knob.name)


#
# Dump: print the fields of the schema with the values of a source
#
DUMP_COLUMNS = ["Namespace", "Field", "Type", "Value", "Default", "State"]


def matches_filters(namespace, path, filters):
    if not filters:
        return True
    candidates = (path.lower(), f"{namespace}:{path}".lower())
    for pattern in filters:
        # '[' is part of the field names (array index), it is not a character class here
        pattern = pattern.lower().replace("[", "[[]")
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            return True
    return False


def dump_rows(schema, variables=None, knobs_only=False, changed_only=False, filters=None):
    """Build the rows describing the schema fields.

    Args:
        schema: Loaded Schema.
        variables: Variables of a .vl or of the system (list of UEFIVariable), None to show the schema defaults.
        knobs_only: One row per knob instead of one row per field.
        changed_only: Only the rows whose value differs from the schema default.
        filters: Wildcard patterns, a row is kept when its field matches one of them.

    State is 'default' when the value is the schema default, 'modified' when it differs, and
    'not-set' when a source was given but does not contain the variable (the default is shown).
    """
    input_vars = {}
    for variable in variables or []:
        input_vars.setdefault((variable.guid, variable.name), variable)

    rows = []
    for knob in schema.knobs:
        variable = input_vars.get(knob_key(knob))
        if variables is not None and variable is None:
            knob.value = knob.default
            not_set = True
        else:
            load_knob_value(knob, variable)
            not_set = False

        subknobs = knob.subknobs[:1] if knobs_only else [s for s in knob.subknobs if s.leaf]
        for subknob in subknobs:
            if not matches_filters(knob.namespace, subknob.name, filters):
                continue
            modified = subknob.value != subknob.default
            if changed_only and not modified:
                continue
            rows.append(OrderedDict(zip(DUMP_COLUMNS, [
                str(knob.namespace).upper(),
                subknob.name,
                subknob.format.c_type,
                subknob.format.object_to_string(subknob.value),
                subknob.format.object_to_string(subknob.default),
                "not-set" if not_set else ("modified" if modified else "default"),
            ])))

    return rows


def format_rows(rows, output_format):
    if output_format == "json":
        return json.dumps(rows, indent=2)

    if output_format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=DUMP_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().rstrip("\n")

    widths = [max([len(column)] + [len(row[column]) for row in rows]) for column in DUMP_COLUMNS]
    lines = ["  ".join(column.ljust(width) for column, width in zip(DUMP_COLUMNS, widths)).rstrip()]
    for row in rows:
        lines.append("  ".join(row[column].ljust(width) for column, width in zip(DUMP_COLUMNS, widths)).rstrip())
    return "\n".join(lines)


#
# Core: apply the changes and build the list of variables to write
#
def load_knob_value(knob, variable):
    """Initialize the knob value from the variable data, or from the schema default if there is none."""
    if variable is None:
        knob.value = knob.default
        return

    expected = knob.format.size_in_bytes()
    if len(variable.data) != expected:
        raise ModifyError(
            f"Variable {knob.name} in the input has {len(variable.data)} bytes but the schema "
            f"expects {expected} bytes, the input does not match this schema"
        )
    knob.value = knob.format.binary_to_object(variable.data)


def modify_variables(schema, changes, variables=None, drop_unrelated=False):
    """Apply the changes to the schema knobs and return the resulting list of UEFIVariable.

    Args:
        schema: Loaded Schema.
        changes: list of '[GUID:]FIELD=VALUE' strings, applied in order.
        variables: Variables of an input .vl (list of UEFIVariable) or None to start from the schema defaults.
        drop_unrelated: Only return the variables that are modified by changes.
    """
    resolved = []
    touched = OrderedDict()
    for change in changes:
        guid, path, value = parse_assignment(change)
        subknob = find_subknob(schema, guid, path)
        resolved.append((subknob, value))
        touched.setdefault(knob_key(subknob.knob), subknob.knob)

    input_vars = {}
    for variable in variables or []:
        input_vars.setdefault((variable.guid, variable.name), variable)

    for key, knob in touched.items():
        load_knob_value(knob, input_vars.get(key))

    for subknob, value in resolved:
        try:
            old = subknob.format.object_to_string(subknob.value)
            subknob.value = subknob.format.string_to_object(value)
            new = subknob.format.object_to_string(subknob.value)
        except (ParseError, InvalidRangeError, ValueError, TypeError, KeyError, IndexError) as e:
            raise ModifyError(f"Cannot set {subknob.name} to '{value}': {e}")
        logging.info(f"{subknob.knob.namespace}:{subknob.name}: {old} -> {new}")

    def build(knob, original=None):
        data = knob.format.object_to_binary(knob.value)
        if original is None:
            return UEFIVariable(knob.name, knob.namespace, data, DEFAULT_ATTRIBUTES)
        return UEFIVariable(original.name, original.guid, data, original.attributes)

    result = []
    if variables is None:
        # No input, start from the schema defaults
        for knob in schema.knobs:
            if knob_key(knob) in touched:
                result.append(build(knob))
            elif not drop_unrelated:
                result.append(UEFIVariable(
                    knob.name, knob.namespace, knob.format.object_to_binary(knob.default), DEFAULT_ATTRIBUTES))
        return result

    written = set()
    for variable in variables:
        key = (variable.guid, variable.name)
        if key in touched:
            result.append(build(touched[key], variable))
            written.add(key)
        elif not drop_unrelated:
            result.append(variable)

    for key, knob in touched.items():
        if key not in written:
            result.append(build(knob))

    return result


def vlist_buffer(variables):
    return b"".join(create_vlist_buffer(variable) for variable in variables)


def write_file_atomic(path, data):
    # The output may be the input file, so never truncate it until the new content is complete
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


#
# Optional integration with ReadUefiVarsToConfVarList and WriteConfVarListToUefiVars
#
def find_tool(base_name, override=None):
    """Find a command to run a tool. Returns the command as a list.

    Order: explicit override, compiled executable next to this script, compiled executable
    in PATH, then the python script next to this script.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))

    def as_command(path):
        if path.lower().endswith(".py"):
            return [sys.executable, path]
        return [path]

    if override:
        if not os.path.isfile(override):
            raise ModifyError(f"Tool '{override}' does not exist")
        return as_command(override)

    exe_name = base_name + (".exe" if os.name == "nt" else "")
    local_exe = os.path.join(script_dir, exe_name)
    if os.path.isfile(local_exe):
        return [local_exe]

    path_exe = shutil.which(exe_name)
    if path_exe:
        return [path_exe]

    script = os.path.join(script_dir, base_name + ".py")
    if os.path.isfile(script):
        return as_command(script)

    raise ModifyError(f"Cannot find {base_name} (neither {exe_name} nor {base_name}.py)")


def hash_check_args(arguments):
    args = []
    if arguments.skip_fw_xml_hash_check:
        args.append("--skip-fw-xml-hash-check")
    if arguments.ignore_fw_xml_hash_mismatch:
        args.append("--ignore-fw-xml-hash-mismatch")
    return args


def run_tool(command):
    logging.info("Running: " + " ".join(command))
    return subprocess.run(command, check=False).returncode


def read_system_variables(arguments, schema_path):
    """Dump the config variables of the running system using ReadUefiVarsToConfVarList."""
    tool = find_tool(READ_TOOL_NAME, arguments.read_tool)
    fd, temp_path = tempfile.mkstemp(suffix=".vl")
    os.close(fd)
    try:
        rc = run_tool(tool + ["-x", schema_path, "-o", temp_path] + hash_check_args(arguments))
        if rc in READ_TOOL_NO_VARIABLES_CODES:
            logging.warning("The system has no variables for this schema")
            return []
        if rc != 0:
            raise ModifyError(f"{READ_TOOL_NAME} failed with exit code {rc}")
        return read_vlist(temp_path)
    finally:
        os.remove(temp_path)


def apply_to_system(arguments, schema_path, vl_path):
    tool = find_tool(WRITE_TOOL_NAME, arguments.write_tool)
    rc = run_tool(tool + ["-l", vl_path, "-x", schema_path] + hash_check_args(arguments))
    if rc != 0:
        raise ModifyError(f"{WRITE_TOOL_NAME} failed with exit code {rc}")


#
# CLI
#
def add_common_arguments(parser):
    parser.add_argument("-x", "--xml", dest="schema_file", required=True, type=str,
                        help="Config schema XML file")
    parser.add_argument("--read-tool", type=str,
                        help=f"Path of {READ_TOOL_NAME} (.exe or .py), detected automatically by default")
    parser.add_argument("--skip-fw-xml-hash-check", action="store_true",
                        help="Passed to the Read/Write tools: skip validating the XML hash against system FW")
    parser.add_argument("--ignore-fw-xml-hash-mismatch", action="store_true",
                        help="Passed to the Read/Write tools: do not fail when the XML hash mismatches FW")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print more details")

    source = parser.add_mutually_exclusive_group()
    source.add_argument("-i", "--input", dest="input_file", type=str,
                        help="Optional .vl file to read the variables from. Without it, the schema defaults are used")
    source.add_argument("--from-system", action="store_true",
                        help=f"Read the variables from this system, using {READ_TOOL_NAME}")
    return source


def option_parser(argv=None):
    parser = argparse.ArgumentParser(
        description="Craft, modify or dump variable list (.vl) data using a config schema XML.",
        epilog="""\
Examples:
  Build the default .vl from the schema:
    ModifyConfVarList.py modify -x schema.xml -o out.vl
  Change one field, starting from the defaults, and only output the changed variable:
    ModifyConfVarList.py modify -x schema.xml -o out.vl -s INTEGER_KNOB=0x20 --drop-unrelated
  Make more changes on the same .vl (the file can be updated in place):
    ModifyConfVarList.py modify -x schema.xml -i out.vl -o out.vl -s COMPLEX_KNOB1a.mode=SECOND -s INTEGER_KNOB=5
  Print the schema defaults, the content of a .vl, or the values of this system:
    ModifyConfVarList.py dump -x schema.xml
    ModifyConfVarList.py dump -x schema.xml -i out.vl --changed-only
    ModifyConfVarList.py dump -x schema.xml --from-system --filter "COMPLEX_KNOB1a*" --format csv
  Print the current configuration: system values, schema defaults where the system has none:
    ModifyConfVarList.py dump -x schema.xml --current
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{modify,dump}")

    modify = subparsers.add_parser(
        "modify", help="Create or update a .vl file by changing fields of the schema",
        description="Create or update a .vl file by changing fields of the schema.")
    add_common_arguments(modify)
    modify.add_argument("-o", "--output", dest="output_file", required=True, type=str,
                        help="Output .vl file (may be the same as the input file)")
    modify.add_argument("-s", "--set", dest="changes", action="append", default=[], metavar="[GUID:]FIELD=VALUE",
                        help="Field to change, can be repeated. FIELD is a knob name or a path inside a knob such as "
                             "KNOB.member or KNOB.array[2]. Use the GUID prefix when the knob name exists in "
                             "more than one namespace. VALUE uses the same syntax as the schema defaults.")
    modify.add_argument("--drop-unrelated", action="store_true",
                        help="Only output the variables changed by this run, drop every other variable")
    modify.add_argument("--apply", action="store_true",
                        help=f"Write the output .vl to the variables of this system with {WRITE_TOOL_NAME}")
    modify.add_argument("--write-tool", type=str,
                        help=f"Path of {WRITE_TOOL_NAME} (.exe or .py), detected automatically by default")

    dump = subparsers.add_parser(
        "dump", help="Print the fields of the schema with their values",
        description="Print the fields of the schema with their values, from the schema defaults (no source), "
                    "a .vl file (-i), this system (--from-system) or the current configuration (--current). State "
                    "is 'default', 'modified' (differs from the schema default) or 'not-set' (the source has no "
                    "such variable, the default is shown). The result is printed to stdout unless -o is given.")
    group = add_common_arguments(dump)
    group.add_argument("--current", action="store_true",
                       help="Read the current configuration: use the variables of this system, and the schema "
                            "default for what is not there. Unlike --from-system, it does not fail when the "
                            "system cannot be read (no privilege, no UEFI variable support, tool not found)")
    dump.add_argument("-o", "--output", dest="output_file", type=str,
                      help="Optional, write the result to this file. Without it the result is printed to stdout")
    dump.add_argument("--format", dest="output_format", choices=["text", "csv", "json"], default="text",
                      help="Output format (default: text)")
    dump.add_argument("--knobs", dest="knobs_only", action="store_true",
                      help="One line per knob (the whole struct value) instead of one line per field")
    dump.add_argument("--changed-only", action="store_true",
                      help="Only print the fields that differ from the schema default")
    dump.add_argument("-f", "--filter", dest="filters", action="append", default=[], metavar="PATTERN",
                      help="Only print the fields matching this case-insensitive wildcard pattern, can be repeated. "
                           "It matches the field (KNOB.member[2]) or NAMESPACE_GUID:FIELD")

    arguments = parser.parse_args(argv)

    if not os.path.isfile(arguments.schema_file):
        parser.error(f"Invalid schema file: {arguments.schema_file}")
    if arguments.input_file is not None and not os.path.isfile(arguments.input_file):
        parser.error(f"Invalid input file: {arguments.input_file}")
    if arguments.command == "modify" and arguments.drop_unrelated and not arguments.changes:
        parser.error("--drop-unrelated needs at least one -s/--set change")

    return arguments


def read_current_variables(arguments):
    """Read the variables of this system, an unreadable system is the same as a system with no variables."""
    try:
        return read_system_variables(arguments, arguments.schema_file)
    except Exception as e:
        logging.warning(f"Cannot read the system variables, using the schema defaults: {e}")
        return []


def load_source_variables(arguments):
    if arguments.input_file is not None:
        return read_vlist(arguments.input_file)
    if arguments.from_system:
        return read_system_variables(arguments, arguments.schema_file)
    if getattr(arguments, "current", False):
        return read_current_variables(arguments)
    return None


def run_modify(arguments, schema):
    variables = load_source_variables(arguments)
    if arguments.from_system and not variables:
        # Nothing on the system: start from the schema defaults
        variables = None

    result = modify_variables(schema, arguments.changes, variables, arguments.drop_unrelated)
    write_file_atomic(arguments.output_file, vlist_buffer(result))
    print(f"{len(result)} variable(s) written to {arguments.output_file}")

    if arguments.apply:
        apply_to_system(arguments, arguments.schema_file, arguments.output_file)


def run_dump(arguments, schema):
    rows = dump_rows(schema, load_source_variables(arguments), arguments.knobs_only,
                     arguments.changed_only, arguments.filters)
    text = format_rows(rows, arguments.output_format)
    if arguments.output_file:
        with open(arguments.output_file, "w", encoding="utf-8", newline="") as file:
            file.write(text + "\n")
        print(f"{len(rows)} row(s) written to {arguments.output_file}")
    else:
        print(text)


def main(argv=None):
    arguments = option_parser(argv)
    logging.basicConfig(level=logging.INFO if arguments.verbose else logging.WARNING, format="%(message)s")

    try:
        schema = Schema.load(arguments.schema_file)
        if arguments.command == "modify":
            run_modify(arguments, schema)
        else:
            run_dump(arguments, schema)
    except ModifyError as e:
        print(f"ERROR: {e}")
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
