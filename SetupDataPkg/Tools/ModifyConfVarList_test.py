# @file
#
# Unit tests for ModifyConfVarList.py
#
# Copyright (c), Microsoft Corporation
# SPDX-License-Identifier: BSD-2-Clause-Patent

import io
import os
import sys
import csv
import json
import uuid
import logging
import tempfile
import unittest
import contextlib
from unittest import mock

# Allow running from any directory, e.g. "python -m unittest SetupDataPkg.Tools.ModifyConfVarList_test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ModifyConfVarList as mcv                                     # noqa: E402
from VariableList import Schema, UEFIVariable, ParseError, read_vlist, create_vlist_buffer      # noqa: E402

NS1 = "FE3ED49F-B173-41ED-9076-356661D46A42"
NS2 = "8CF777E5-0D01-479D-9255-19B1F07328D4"


def sample_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "sampleschema.xml")


def as_dict(variables):
    return {(str(v.guid).upper(), v.name): v for v in variables}


class QuietTestCase(unittest.TestCase):
    """Hide the messages the tool prints for the error cases that the tests trigger on purpose."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)


class ModifyConfVarListTests(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.schema = Schema.load(sample_path())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def decode(self, variable):
        knob = self.schema.get_knob(variable.guid, variable.name).knob
        return knob.format.binary_to_object(variable.data)

    def test_parse_assignment(self):
        self.assertEqual(mcv.parse_assignment("A.b[1]=0x10"), (None, "A.b[1]", "0x10"))
        guid, path, value = mcv.parse_assignment(f"{{{NS1}}}:K = {{1,2}}")
        self.assertEqual((guid, path, value), (uuid.UUID(NS1), "K", "{1,2}"))
        for bad in ("NOVALUE", "=1", "zz:K=1"):
            with self.assertRaises(mcv.ModifyError):
                mcv.parse_assignment(bad)

    def test_defaults_without_input(self):
        result = mcv.modify_variables(self.schema, [])
        self.assertEqual(len(result), len(self.schema.knobs))
        for variable in result:
            knob = self.schema.get_knob(variable.guid, variable.name).knob
            self.assertEqual(variable.data, knob.format.object_to_binary(knob.default))

    def test_modify_scalar_without_input(self):
        result = as_dict(mcv.modify_variables(self.schema, ["INTEGER_KNOB=0x20"]))
        self.assertEqual(len(result), len(self.schema.knobs))
        self.assertEqual(self.decode(result[(NS1, "INTEGER_KNOB")]), 0x20)
        self.assertEqual(self.decode(result[(NS2, "INTEGER_KNOB2")]), 89)

    def test_drop_unrelated_without_input(self):
        result = mcv.modify_variables(self.schema, ["BOOLEAN_KNOB=false"], drop_unrelated=True)
        self.assertEqual([v.name for v in result], ["BOOLEAN_KNOB"])
        self.assertEqual(self.decode(result[0]), False)

    def test_struct_member_enum_and_array_element(self):
        result = mcv.modify_variables(
            self.schema, ["COMPLEX_KNOB1a.mode=THIRD", "COMPLEX_KNOB1a.data[2]=9"], drop_unrelated=True)
        self.assertEqual(len(result), 1)
        value = self.decode(result[0])
        self.assertEqual(value["data"], [1, 2, 9, 4, 5])
        self.assertEqual(value["mode"], 2)

    def test_nested_struct_path(self):
        result = mcv.modify_variables(
            self.schema, ["COMPLEX_KNOB2.children[1].data[0]=42"], drop_unrelated=True)
        value = self.decode(result[0])
        self.assertEqual(value["counter"], 2)
        self.assertEqual(value["children"][1]["data"][0], 42)
        self.assertEqual(value["children"][0]["data"][0], 1)

    def test_ambiguous_name_needs_guid(self):
        with self.assertRaises(mcv.ModifyError):
            mcv.modify_variables(self.schema, ["COMPLEX_KNOB1b.mode=THIRD"])
        result = mcv.modify_variables(
            self.schema, [f"{NS2}:COMPLEX_KNOB1b.mode=THIRD"], drop_unrelated=True)
        self.assertEqual(str(result[0].guid).upper(), NS2)
        self.assertEqual(self.decode(result[0])["mode"], 2)

    def test_invalid_changes(self):
        for change in ("NO_SUCH_KNOB=1", "INTEGER_KNOB=abc", "COMPLEX_KNOB1a.mode=BAD",
                       "COMPLEX_KNOB1a.data[2]=300", "COMPLEX_KNOB1a.data[9]=1"):
            with self.assertRaises(mcv.ModifyError, msg=change):
                mcv.modify_variables(self.schema, [change])

    def input_variables(self):
        unrelated = UEFIVariable("OtherVar", uuid.UUID(NS1), b"\x01\x02\x03", 3)
        knob_data = self.schema.get_knob(uuid.UUID(NS1), "INTEGER_KNOB").knob.format.object_to_binary(7)
        existing = UEFIVariable("INTEGER_KNOB", uuid.UUID(NS1), knob_data, 6)
        other_knob = UEFIVariable("INTEGER_KNOB2", uuid.UUID(NS2), (5).to_bytes(4, "little"), 7)
        return [unrelated, existing, other_knob]

    def test_input_is_preserved(self):
        original = self.input_variables()
        result = mcv.modify_variables(self.schema, ["INTEGER_KNOB=8", "BOOLEAN_KNOB=false"], original)
        names = [v.name for v in result]
        self.assertEqual(names, ["OtherVar", "INTEGER_KNOB", "INTEGER_KNOB2", "BOOLEAN_KNOB"])
        by_name = {v.name: v for v in result}
        self.assertEqual(by_name["OtherVar"].data, b"\x01\x02\x03")
        self.assertEqual(self.decode(by_name["INTEGER_KNOB"]), 8)
        self.assertEqual(by_name["INTEGER_KNOB"].attributes, 6)
        self.assertEqual(self.decode(by_name["INTEGER_KNOB2"]), 5)
        self.assertEqual(self.decode(by_name["BOOLEAN_KNOB"]), False)
        self.assertEqual(by_name["BOOLEAN_KNOB"].attributes, mcv.DEFAULT_ATTRIBUTES)

    def test_input_drop_unrelated(self):
        result = mcv.modify_variables(self.schema, ["INTEGER_KNOB=8"], self.input_variables(), True)
        self.assertEqual([v.name for v in result], ["INTEGER_KNOB"])

    def test_input_uses_existing_value_as_base(self):
        data = self.schema.get_knob(uuid.UUID(NS1), "COMPLEX_KNOB1a").knob.format.object_to_binary(
            {"data": [9, 9, 9, 9, 9], "mode": 1})
        variables = [UEFIVariable("COMPLEX_KNOB1a", uuid.UUID(NS1), data)]
        result = mcv.modify_variables(self.schema, ["COMPLEX_KNOB1a.data[0]=1"], variables)
        self.assertEqual(self.decode(result[0]), {"data": [1, 9, 9, 9, 9], "mode": 1})

    def test_input_size_mismatch(self):
        variables = [UEFIVariable("INTEGER_KNOB", uuid.UUID(NS1), b"\x01")]
        with self.assertRaises(mcv.ModifyError):
            mcv.modify_variables(self.schema, ["INTEGER_KNOB=1"], variables)

    def test_cli_multiple_calls_same_file(self):
        out = self.path("out.vl")
        schema = sample_path()
        self.assertEqual(mcv.main(["modify", "-x", schema, "-o", out, "-s", "INTEGER_KNOB=1"]), 0)
        self.assertEqual(mcv.main(["modify", "-x", schema, "-i", out, "-o", out, "-s", "FLOAT_KNOB=2.5"]), 0)
        self.assertEqual(mcv.main(["modify", "-x", schema, "-i", out, "-o", out, "-s", "INTEGER_KNOB=3"]), 0)
        variables = as_dict(read_vlist(out))
        self.assertEqual(len(variables), len(self.schema.knobs))
        self.assertEqual(self.decode(variables[(NS1, "INTEGER_KNOB")]), 3)
        self.assertEqual(self.decode(variables[(NS1, "FLOAT_KNOB")]), 2.5)

        dropped = self.path("dropped.vl")
        self.assertEqual(mcv.main(["modify", "-x", schema, "-i", out, "-o", dropped, "-s", "INTEGER_KNOB=4",
                                   "--drop-unrelated"]), 0)
        self.assertEqual([v.name for v in read_vlist(dropped)], ["INTEGER_KNOB"])

    def test_cli_errors(self):
        out = self.path("out.vl")
        self.assertEqual(mcv.main(["modify", "-x", sample_path(), "-o", out, "-s", "NOPE=1"]), 1)
        self.assertFalse(os.path.exists(out))
        with open(out, "wb") as f:
            f.write(b"\x01\x02")
        self.assertEqual(mcv.main(["modify", "-x", sample_path(), "-i", out, "-o", out, "-s", "INTEGER_KNOB=1"]), 1)
        self.assertEqual(mcv.main(["dump", "-x", sample_path(), "-i", out]), 1)
        with self.assertRaises(SystemExit):
            mcv.main(["-x", sample_path()])
        with self.assertRaises(SystemExit):
            mcv.main(["modify", "-x", sample_path()])
        with self.assertRaises(SystemExit):
            mcv.main(["modify", "-x", sample_path(), "-o", out, "--drop-unrelated"])
        with self.assertRaises(SystemExit):
            mcv.main(["modify", "-x", sample_path(), "-o", out, "-i", out, "--from-system"])

    def dump(self, *args):
        with mock.patch("builtins.print") as printed:
            rc = mcv.main(["dump", "-x", sample_path()] + list(args))
        self.assertEqual(rc, 0)
        return "\n".join(call.args[0] for call in printed.call_args_list)

    def test_dump_defaults(self):
        rows = mcv.dump_rows(self.schema)
        by_field = {(r["Namespace"], r["Field"]): r for r in rows}
        self.assertEqual(len(rows), len(by_field))
        self.assertEqual({r["State"] for r in rows}, {"default"})
        row = by_field[(NS1, "COMPLEX_KNOB1a.data[4]")]
        self.assertEqual((row["Type"], row["Value"], row["Default"]), ("uint8_t", "5", "5"))
        self.assertEqual(by_field[(NS1, "COMPLEX_KNOB1a.mode")]["Value"], "First Mode")
        self.assertEqual(by_field[(NS2, "INTEGER_KNOB2")]["Value"], "89")
        # structs and arrays are not leaf fields
        self.assertNotIn((NS1, "COMPLEX_KNOB1a"), by_field)
        self.assertNotIn((NS1, "COMPLEX_KNOB1a.data"), by_field)

    def test_dump_knobs_only(self):
        rows = mcv.dump_rows(self.schema, knobs_only=True)
        self.assertEqual(len(rows), len(self.schema.knobs))
        by_field = {(r["Namespace"], r["Field"]): r for r in rows}
        self.assertEqual(by_field[(NS1, "COMPLEX_KNOB1a")]["Value"], "{{1,2,3,4,5},First Mode}")

    def test_dump_vl_states(self):
        out = self.path("in.vl")
        self.assertEqual(mcv.main(["modify", "-x", sample_path(), "-o", out, "-s", "COMPLEX_KNOB1a.data[2]=9",
                                   "-s", "INTEGER_KNOB=100", "--drop-unrelated"]), 0)
        rows = mcv.dump_rows(self.schema, read_vlist(out))
        states = {(r["Namespace"], r["Field"]): r for r in rows}
        self.assertEqual(states[(NS1, "COMPLEX_KNOB1a.data[2]")]["State"], "modified")
        self.assertEqual(states[(NS1, "COMPLEX_KNOB1a.data[2]")]["Value"], "9")
        self.assertEqual(states[(NS1, "COMPLEX_KNOB1a.data[2]")]["Default"], "3")
        # present in the .vl with its default value
        self.assertEqual(states[(NS1, "INTEGER_KNOB")]["State"], "default")
        # absent from the .vl
        self.assertEqual(states[(NS1, "BOOLEAN_KNOB")]["State"], "not-set")
        self.assertEqual(states[(NS1, "COMPLEX_KNOB1a.data[1]")]["State"], "default")

        changed = mcv.dump_rows(self.schema, read_vlist(out), changed_only=True)
        self.assertEqual([r["Field"] for r in changed], ["COMPLEX_KNOB1a.data[2]"])

    def test_dump_empty_source_is_not_set(self):
        rows = mcv.dump_rows(self.schema, [])
        self.assertEqual({r["State"] for r in rows}, {"not-set"})

    def test_dump_filters(self):
        fields = [r["Field"] for r in mcv.dump_rows(self.schema, filters=["complex_knob1a.*"])]
        self.assertEqual(fields, [f"COMPLEX_KNOB1a.data[{i}]" for i in range(5)] + ["COMPLEX_KNOB1a.mode"])
        # '[' in a pattern is a literal character
        fields = [r["Field"] for r in mcv.dump_rows(self.schema, filters=["COMPLEX_KNOB1a.data[2]"])]
        self.assertEqual(fields, ["COMPLEX_KNOB1a.data[2]"])
        # namespace qualified, only the second namespace has INTEGER_KNOB2
        fields = [r["Field"] for r in mcv.dump_rows(self.schema, filters=[f"{NS2}:INTEGER*", "FLOAT_KNOB"])]
        self.assertEqual(fields, ["FLOAT_KNOB", "INTEGER_KNOB2"])
        self.assertEqual(mcv.dump_rows(self.schema, filters=["NO_MATCH*"]), [])

    def test_dump_cli_formats(self):
        text = self.dump("--filter", "INTEGER_KNOB*")
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["Namespace", "Field", "Type", "Value", "Default", "State"])
        self.assertEqual(lines[1].split(), [NS1, "INTEGER_KNOB", "uint32_t", "100", "100", "default"])
        self.assertEqual(lines[2].split(), [NS2, "INTEGER_KNOB2", "uint32_t", "89", "89", "default"])

        csv_lines = self.dump("--filter", "INTEGER_KNOB", "--format", "csv").splitlines()
        self.assertEqual(csv_lines, ["Namespace,Field,Type,Value,Default,State",
                                     f"{NS1},INTEGER_KNOB,uint32_t,100,100,default"])

        loaded = json.loads(self.dump("--filter", "COMPLEX_KNOB1a", "--knobs", "--format", "json"))
        self.assertEqual(loaded[0]["Value"], "{{1,2,3,4,5},First Mode}")

        # the csv value of a struct contains commas and must be quoted
        csv_rows = list(csv.DictReader(io.StringIO(self.dump("--filter", "COMPLEX_KNOB1a", "--knobs",
                                                             "--format", "csv"))))
        self.assertEqual(csv_rows[0]["Value"], "{{1,2,3,4,5},First Mode}")

    def test_dump_cli_to_file(self):
        out = self.path("dump.csv")
        self.assertEqual(mcv.main(["dump", "-x", sample_path(), "-o", out, "--format", "csv",
                                   "--changed-only"]), 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "Namespace,Field,Type,Value,Default,State\n")

    def test_dump_output_is_optional(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(mcv.main(["dump", "-x", sample_path(), "--filter", "INTEGER_KNOB"]), 0)
        self.assertEqual(len(printed.call_args_list), 1)
        self.assertIn("INTEGER_KNOB", printed.call_args.args[0])

    RANGE_SCHEMA = """<ConfigSchema>
  <Enums>
    <Enum name="MODE_T" help="Modes">
      <Value name="A" prettyname="Mode A" value="0" help="First one" />
      <Value name="B" value="5" />
    </Enum>
  </Enums>
  <Structs>
    <Struct name="s_t">
      <Member name="level" type="uint8_t" default="125" min="100" max="150" help="Level help" />
      <Member name="mode" type="MODE_T" />
      <Member name="vals" type="uint16_t" count="3" default="{1}" />
    </Struct>
  </Structs>
  <Knobs namespace="{11111111-2222-3333-4444-555555555555}">
    <Knob name="TDP" type="uint8_t" default="125" min="100" max="150" help="TDP help" />
    <Knob name="RATIO" type="float" default="1.5" min="0.5" max="2.5" />
    <Knob name="SWITCH" type="bool" default="false" />
    <Knob name="S" type="s_t" help="Struct help" />
  </Knobs>
</ConfigSchema>"""

    def describe_by_field(self, schema, **kwargs):
        return {r["Field"]: r for r in mcv.describe_rows(schema, **kwargs)}

    def test_describe_enum_bool_and_numbers(self):
        rows = self.describe_by_field(self.schema)
        mode = rows["COMPLEX_KNOB1a.mode"]
        self.assertEqual((mode["Type"], mode["Kind"], mode["Default"]), ("OPTION_MODE", "enum", "First Mode"))
        self.assertEqual([(v["name"], v["pretty_name"], v["value"], v["help"]) for v in mode["Values"]],
                         [("FIRST", "First Mode", 0, "First mode"), ("SECOND", "Second Mode", 1, "Second mode"),
                          ("THIRD", "Third Mode", 2, "Third mode")])
        self.assertIn("FIRST=0 (First Mode)", mode["Valid"])

        self.assertEqual(rows["BOOLEAN_KNOB"]["Valid"], "true or false")
        self.assertEqual(rows["BOOLEAN_KNOB"]["Kind"], "bool")
        integer = rows["INTEGER_KNOB"]
        self.assertEqual((integer["Min"], integer["Max"], integer["Default"]), (0, 0xFFFFFFFF, "100"))
        self.assertEqual(rows["FLOAT_KNOB"]["Valid"], "any number")

    def test_describe_collapses_array_elements(self):
        rows = self.describe_by_field(self.schema)
        data = rows["COMPLEX_KNOB1a.data[0..4]"]
        self.assertEqual(data["Default"], "by index: 1, 2, 3, 4, 5")
        self.assertEqual((data["Min"], data["Max"]), (0, 255))
        self.assertNotIn("COMPLEX_KNOB1a.data[2]", rows)
        self.assertIn("COMPLEX_KNOB2.children[0..1].data[0..4]", rows)
        self.assertIn("COMPLEX_KNOB2.children[0..1].mode", rows)
        self.assertNotIn("COMPLEX_KNOB1a", rows)

    def test_describe_limits_from_the_schema(self):
        rows = self.describe_by_field(Schema.parse(self.RANGE_SCHEMA))
        tdp = rows["TDP"]
        self.assertEqual(tdp["Valid"], "100 .. 150, decimal or 0x hex")
        self.assertEqual((tdp["Min"], tdp["Max"], tdp["Help"]), (100, 150, "TDP help"))
        self.assertEqual((rows["S.level"]["Min"], rows["S.level"]["Max"]), (100, 150))
        self.assertEqual(rows["S.level"]["Help"], "Level help")
        self.assertEqual(rows["RATIO"]["Valid"], "0.5 .. 2.5")
        self.assertEqual((rows["RATIO"]["Min"], rows["RATIO"]["Max"]), (0.5, 2.5))
        self.assertEqual(rows["S.vals[0..2]"]["Default"], "1")
        self.assertEqual(rows["S.vals[0..2]"]["Valid"], "0 .. 65535, decimal or 0x hex")
        # a pretty name is only shown when it differs from the name
        self.assertEqual(rows["S.mode"]["Valid"].split(" (name")[0], "A=0 (Mode A); B=5")

    def test_describe_knobs_only(self):
        rows = self.describe_by_field(Schema.parse(self.RANGE_SCHEMA), knobs_only=True)
        self.assertEqual(list(rows), ["TDP", "RATIO", "SWITCH", "S"])
        self.assertEqual(rows["S"]["Kind"], "struct")
        self.assertEqual(rows["S"]["Help"], "Struct help")
        self.assertTrue(rows["S"]["Valid"].startswith("{level,mode,vals}"))
        self.assertNotIn("Min", rows["S"])

    def test_describe_array_knob_as_a_whole(self):
        rows = self.describe_by_field(self.schema, knobs_only=True, filters=["COMPLEX_KNOB2"])
        self.assertEqual(rows["COMPLEX_KNOB2"]["Type"], "sample_t")
        self.assertIn("{counter,children}", rows["COMPLEX_KNOB2"]["Valid"])

    def test_describe_filters(self):
        def fields(*patterns):
            return [r["Field"] for r in mcv.describe_rows(self.schema, filters=list(patterns))]

        # one element of an array selects the described array
        self.assertEqual(fields("COMPLEX_KNOB1a.data[2]"), ["COMPLEX_KNOB1a.data[0..4]"])
        self.assertEqual(fields("COMPLEX_KNOB1a.data[*]"), ["COMPLEX_KNOB1a.data[0..4]"])
        self.assertEqual(fields("complex_knob1a.mode"), ["COMPLEX_KNOB1a.mode"])
        self.assertEqual(fields(f"{NS2}:INTEGER*"), ["INTEGER_KNOB2"])
        self.assertEqual(fields("NO_MATCH"), [])

    def test_describe_same_name_in_two_namespaces_stays_separate(self):
        rows = [r for r in mcv.describe_rows(self.schema, filters=["COMPLEX_KNOB1b.mode"])]
        self.assertEqual({r["Namespace"] for r in rows}, {NS1, NS2})

    def test_describe_formats(self):
        rows = mcv.describe_rows(self.schema, filters=["COMPLEX_KNOB1a.mode", "INTEGER_KNOB"])
        text = mcv.format_description(rows, "text")
        self.assertIn(f"{NS1}:COMPLEX_KNOB1a.mode\n  Type:     OPTION_MODE (enum)\n  Default:  First Mode\n  Valid:    "
                      'FIRST  = 0  "First Mode"  First mode\n            SECOND = 1  "Second Mode"  Second mode',
                      text)
        self.assertIn("  Valid:    0 .. 4294967295, decimal or 0x hex", text)
        self.assertIn("Help:     Integer type", text)

        csv_rows = list(csv.DictReader(io.StringIO(mcv.format_description(rows, "csv"))))
        self.assertEqual([r["Field"] for r in csv_rows], ["COMPLEX_KNOB1a.mode", "INTEGER_KNOB"])
        self.assertEqual(list(csv_rows[0]), mcv.DESCRIBE_COLUMNS)
        self.assertIn("FIRST=0 (First Mode)", csv_rows[0]["Valid"])

        loaded = json.loads(mcv.format_description(rows, "json"))
        self.assertEqual(loaded[0]["Values"][2]["name"], "THIRD")
        self.assertEqual(loaded[1]["Max"], 0xFFFFFFFF)

    def test_describe_cli(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(mcv.main(["describe", "-x", sample_path(), "-f", "COMPLEX_KNOB1a.mode"]), 0)
        self.assertEqual(len(printed.call_args_list), 1)
        self.assertIn("THIRD  = 2", printed.call_args.args[0])

        out = self.path("describe.json")
        self.assertEqual(mcv.main(["describe", "-x", sample_path(), "-o", out, "--format", "json",
                                   "-f", "BOOLEAN_KNOB"]), 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["Valid"], "true or false")

        with mock.patch("builtins.print") as printed:
            self.assertEqual(mcv.main(["describe", "-x", sample_path(), "-f", "NO_MATCH"]), 0)
        printed.assert_not_called()

        # No source is involved
        for other in (["-i", sample_path()], ["--from-system"], ["--current"]):
            with self.assertRaises(SystemExit):
                mcv.main(["describe", "-x", sample_path()] + other)

    def test_set_a_whole_struct_or_array(self):
        result = mcv.modify_variables(self.schema, ["COMPLEX_KNOB1a={{9,8,7,6,5},THIRD}"], drop_unrelated=True)
        self.assertEqual(self.decode(result[0]), {"data": [9, 8, 7, 6, 5], "mode": 2})
        result = mcv.modify_variables(self.schema, ["COMPLEX_KNOB1a.data={7}"], drop_unrelated=True)
        self.assertEqual(self.decode(result[0])["data"], [7, 7, 7, 7, 7])

    def test_enum_values_are_case_insensitive(self):
        result = mcv.modify_variables(
            self.schema, ["COMPLEX_KNOB1a.mode=second", "COMPLEX_KNOB1a.data[0]=7"], drop_unrelated=True)
        self.assertEqual(self.decode(result[0])["mode"], 1)
        for text in ("SECOND", "Second", "sEcOnD", "second mode", "SECOND MODE", "Second Mode", "1"):
            result = mcv.modify_variables(self.schema, [f"COMPLEX_KNOB1a.mode={text}"], drop_unrelated=True)
            self.assertEqual(self.decode(result[0])["mode"], 1, text)

        # also inside the value of a whole struct
        result = mcv.modify_variables(self.schema, ["COMPLEX_KNOB1a={{1,2,3,4,5},third}"], drop_unrelated=True)
        self.assertEqual(self.decode(result[0])["mode"], 2)

        # a wrong name is still an error
        with self.assertRaises(mcv.ModifyError):
            mcv.modify_variables(self.schema, ["COMPLEX_KNOB1a.mode=seconds"])

    def test_enum_case_insensitive_match_must_not_be_ambiguous(self):
        schema = Schema.parse("""<ConfigSchema>
  <Enums>
    <Enum name="CASE_T">
      <Value name="Low" value="0" />
      <Value name="LOW" value="1" />
      <Value name="High" value="2" />
    </Enum>
  </Enums>
  <Knobs namespace="{11111111-2222-3333-4444-555555555555}">
    <Knob name="K" type="CASE_T" />
  </Knobs>
</ConfigSchema>""")
        mcv.accept_any_case_for_enums(schema)
        mcv.accept_any_case_for_enums(schema)    # applying it again must not wrap it twice
        fmt = schema.get_knob("11111111-2222-3333-4444-555555555555", "K").format
        # An exact match always wins
        self.assertEqual((fmt.string_to_object("Low"), fmt.string_to_object("LOW")), (0, 1))
        self.assertEqual(fmt.string_to_object("high"), 2)
        self.assertEqual(fmt.string_to_object(""), 0)
        self.assertEqual(fmt.string_to_object("2"), 2)
        with self.assertRaises(ParseError):
            fmt.string_to_object("low")
        with self.assertRaises(ParseError):
            fmt.string_to_object("nothing")

    def test_enum_case_change_does_not_touch_the_shared_parser(self):
        # Only the schemas handled by this tool get the new behavior, the shared VariableList code does not
        other = Schema.load(sample_path())
        mcv.accept_any_case_for_enums(self.schema)
        mode = other.get_knob(NS1, "COMPLEX_KNOB1a.mode").format
        with self.assertRaises(ParseError):
            mode.string_to_object("second")
        self.assertEqual(mode.string_to_object("SECOND"), 1)
        self.assertEqual(self.schema.get_knob(NS1, "COMPLEX_KNOB1a.mode").format.string_to_object("second"), 1)

    def test_bool_values_are_case_insensitive(self):
        for text in ("true", "True", "TRUE", "tRuE", " true ", "1", "yes", "YES"):
            result = mcv.modify_variables(self.schema, [f"BOOLEAN_KNOB2={text}"], drop_unrelated=True)
            self.assertIs(self.decode(result[0]), True, text)
        for text in ("false", "False", "FALSE", "0"):
            result = mcv.modify_variables(self.schema, [f"BOOLEAN_KNOB={text}"], drop_unrelated=True)
            self.assertIs(self.decode(result[0]), False, text)

        # booleans inside a struct and an array of booleans
        schema = Schema.parse("""<ConfigSchema>
  <Structs>
    <Struct name="s_t">
      <Member name="on" type="bool" default="false" />
      <Member name="flags" type="bool" count="2" default="{false}" />
    </Struct>
  </Structs>
  <Knobs namespace="{11111111-2222-3333-4444-555555555555}">
    <Knob name="S" type="s_t" />
    <Knob name="DEF" type="bool" default="TRUE" />
  </Knobs>
</ConfigSchema>""")
        self.assertIs(schema.get_knob("11111111-2222-3333-4444-555555555555", "DEF").knob.default, True)
        for change, expected in (("S.on=TRUE", {"on": True, "flags": [False, False]}),
                                 ("S.flags[1]=True", {"on": False, "flags": [False, True]}),
                                 ("S={TRUE,{True,false}}", {"on": True, "flags": [True, False]})):
            knob = schema.get_knob("11111111-2222-3333-4444-555555555555", "S").knob
            result = mcv.modify_variables(schema, [change], drop_unrelated=True)
            self.assertEqual(knob.format.binary_to_object(result[0].data), expected, change)

    def test_modify_errors_tell_the_valid_values(self):
        def error_of(change):
            with self.assertRaises(mcv.ModifyError) as context:
                mcv.modify_variables(self.schema, [change])
            return str(context.exception)

        self.assertIn("Valid values: FIRST=0 (First Mode); SECOND=1 (Second Mode); THIRD=2 (Third Mode)",
                      error_of("COMPLEX_KNOB1a.mode=BAD"))
        self.assertIn("Valid values: 0 .. 255", error_of("COMPLEX_KNOB1a.data[2]=300"))
        self.assertIn("Valid values: 0 .. 255", error_of("COMPLEX_KNOB1a.data[2]=abc"))
        self.assertIn("with 5 elements", error_of("COMPLEX_KNOB1a.data={1,2}"))
        self.assertIn("Valid values: {counter,children}", error_of("COMPLEX_KNOB2={1}"))


class ToolIntegrationTests(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe_name = mcv.READ_TOOL_NAME + (".exe" if os.name == "nt" else "")

    def fake_script_dir(self, *file_names):
        """Make find_tool search temporary folders instead of the folders of the real tools."""
        self.script_dir = os.path.join(self.tmp.name, "script")
        self.cwd_dir = os.path.join(self.tmp.name, "cwd")
        os.makedirs(self.script_dir, exist_ok=True)
        os.makedirs(self.cwd_dir, exist_ok=True)
        for name in file_names:
            open(os.path.join(self.script_dir, name), "w").close()
        for patcher in (mock.patch.object(mcv, "__file__", os.path.join(self.script_dir, "ModifyConfVarList.py")),
                        mock.patch("os.getcwd", return_value=self.cwd_dir)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_find_tool_override(self):
        py_tool = os.path.join(self.tmp.name, "tool.py")
        exe_tool = os.path.join(self.tmp.name, "tool.exe")
        for p in (py_tool, exe_tool):
            open(p, "w").close()
        self.assertEqual(mcv.find_tool("x", py_tool), [sys.executable, py_tool])
        self.assertEqual(mcv.find_tool("x", exe_tool), [exe_tool])
        with self.assertRaises(mcv.ModifyError):
            mcv.find_tool("x", os.path.join(self.tmp.name, "missing.exe"))

    def test_find_tool_defaults_to_executable_in_current_directory(self):
        self.fake_script_dir(self.exe_name, mcv.READ_TOOL_NAME + ".py")
        cwd_exe = os.path.join(self.cwd_dir, self.exe_name)
        open(cwd_exe, "w").close()
        with mock.patch("shutil.which", return_value="C:/bin/" + self.exe_name):
            self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME), [cwd_exe])
        # An explicit tool still wins
        override = os.path.join(self.tmp.name, "mine.exe")
        open(override, "w").close()
        self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME, override), [override])

    def test_find_tool_prefers_executable_next_to_script(self):
        self.fake_script_dir(self.exe_name, mcv.READ_TOOL_NAME + ".py")
        with mock.patch("shutil.which", return_value="C:/bin/" + self.exe_name):
            self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME), [os.path.join(self.script_dir, self.exe_name)])

    def test_find_tool_prefers_executable_in_path_over_script(self):
        self.fake_script_dir(mcv.READ_TOOL_NAME + ".py")
        with mock.patch("shutil.which", return_value="C:/bin/" + self.exe_name):
            self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME), ["C:/bin/" + self.exe_name])

    def test_find_tool_falls_back_to_script(self):
        self.fake_script_dir(mcv.READ_TOOL_NAME + ".py")
        script = os.path.join(self.script_dir, mcv.READ_TOOL_NAME + ".py")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME), [sys.executable, script])

    def test_find_tool_not_found(self):
        self.fake_script_dir()
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(mcv.ModifyError):
                mcv.find_tool(mcv.READ_TOOL_NAME)

    def test_find_tool_finds_the_real_tools(self):
        # Without mocks, the tools next to the script are found (as .exe or as .py)
        for name in (mcv.READ_TOOL_NAME, mcv.WRITE_TOOL_NAME):
            command = mcv.find_tool(name)
            self.assertTrue(os.path.isfile(command[-1]), command)

    def fake_read_tool(self, exit_code, vl_data=b""):
        """A real executable (python script) that behaves like ReadUefiVarsToConfVarList, run for real."""
        script = os.path.join(self.tmp.name, "FakeRead.py")
        data_path = os.path.join(self.tmp.name, "fake_data.vl")
        with open(data_path, "wb") as f:
            f.write(vl_data)
        with open(script, "w") as f:
            f.write("import sys, shutil\n"
                    "out = sys.argv[sys.argv.index('-o') + 1]\n"
                    f"shutil.copyfile({data_path!r}, out)\n"
                    f"sys.exit({exit_code})\n")
        return script

    def test_from_system_without_variables_uses_schema_defaults(self):
        # The real read tool exits with -1 when it finds no variable, which is a large number on Windows
        tool = self.fake_read_tool(-1)
        out = os.path.join(self.tmp.name, "out.vl")

        self.assertEqual(mcv.main(["modify", "-x", sample_path(), "-o", out, "--from-system", "--read-tool", tool,
                                   "-s", "INTEGER_KNOB=7"]), 0)
        variables = as_dict(read_vlist(out))
        schema = Schema.load(sample_path())
        self.assertEqual(len(variables), len(schema.knobs))
        knob = schema.get_knob(uuid.UUID(NS1), "INTEGER_KNOB").knob
        self.assertEqual(knob.format.binary_to_object(variables[(NS1, "INTEGER_KNOB")].data), 7)
        other = schema.get_knob(uuid.UUID(NS2), "INTEGER_KNOB2").knob
        self.assertEqual(other.format.binary_to_object(variables[(NS2, "INTEGER_KNOB2")].data), 89)

        with mock.patch("builtins.print") as printed:
            self.assertEqual(mcv.main(["dump", "-x", sample_path(), "--from-system", "--read-tool", tool,
                                       "-f", "INTEGER_KNOB", "--format", "csv"]), 0)
        self.assertEqual(printed.call_args.args[0].splitlines()[1], f"{NS1},INTEGER_KNOB,uint32_t,100,100,not-set")

    def test_from_system_exit_codes(self):
        for code in (-1, 255):
            self.assertIn(mcv.run_tool([sys.executable, self.fake_read_tool(code), "-o", self.fake_vl_path()]),
                          mcv.READ_TOOL_NO_VARIABLES_CODES)
        # Other failures are still reported
        out = os.path.join(self.tmp.name, "out.vl")
        tool = self.fake_read_tool(1)
        self.assertEqual(mcv.main(["modify", "-x", sample_path(), "-o", out, "--from-system", "--read-tool", tool]), 1)
        self.assertFalse(os.path.exists(out))

    def fake_vl_path(self):
        return os.path.join(self.tmp.name, "scratch.vl")

    def test_from_system_with_variables_uses_them(self):
        schema = Schema.load(sample_path())
        knob = schema.get_knob(uuid.UUID(NS1), "INTEGER_KNOB").knob
        data = create_vlist_buffer(UEFIVariable("INTEGER_KNOB", uuid.UUID(NS1), knob.format.object_to_binary(55)))
        tool = self.fake_read_tool(0, data)
        with mock.patch("builtins.print") as printed:
            self.assertEqual(mcv.main(["dump", "-x", sample_path(), "--from-system", "--read-tool", tool,
                                       "-f", "INTEGER_KNOB", "--format", "csv"]), 0)
        self.assertEqual(printed.call_args.args[0].splitlines()[1], f"{NS1},INTEGER_KNOB,uint32_t,55,100,modified")

    def test_from_system_and_apply(self):
        out = os.path.join(self.tmp.name, "out.vl")
        schema = Schema.load(sample_path())
        knob = schema.get_knob(uuid.UUID(NS1), "INTEGER_KNOB").knob
        system_var = UEFIVariable("INTEGER_KNOB", uuid.UUID(NS1), knob.format.object_to_binary(55))
        calls = []

        def fake_run(command):
            calls.append(command)
            if "-o" in command:
                with open(command[command.index("-o") + 1], "wb") as f:
                    f.write(create_vlist_buffer(system_var))
            return 0

        with mock.patch.object(mcv, "run_tool", side_effect=fake_run), \
                mock.patch.object(mcv, "find_tool", side_effect=lambda name, override=None: [name]):
            rc = mcv.main(["modify", "-x", sample_path(), "-o", out, "--from-system", "--apply",
                           "--skip-fw-xml-hash-check", "-s", "BOOLEAN_KNOB=false", "--drop-unrelated"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], mcv.READ_TOOL_NAME)
        self.assertIn("--skip-fw-xml-hash-check", calls[0])
        self.assertEqual(calls[1][:2], [mcv.WRITE_TOOL_NAME, "-l"])
        self.assertEqual(calls[1][2], out)
        self.assertEqual([v.name for v in read_vlist(out)], ["BOOLEAN_KNOB"])

    def test_from_system_nothing_found_and_failures(self):
        out = os.path.join(self.tmp.name, "out.vl")
        base = ["modify", "-x", sample_path(), "-o", out, "--from-system", "-s", "INTEGER_KNOB=1"]
        with mock.patch.object(mcv, "find_tool", return_value=["read"]):
            with mock.patch.object(mcv, "run_tool", return_value=-1):
                self.assertEqual(mcv.main(base), 0)
            with mock.patch.object(mcv, "run_tool", return_value=1):
                self.assertEqual(mcv.main(base), 1)
        with mock.patch.object(mcv, "find_tool", return_value=["write"]), \
                mock.patch.object(mcv, "run_tool", return_value=2):
            self.assertEqual(mcv.main(["modify", "-x", sample_path(), "-o", out, "--apply"]), 1)

    def test_dump_from_system(self):
        schema = Schema.load(sample_path())
        knob = schema.get_knob(uuid.UUID(NS1), "INTEGER_KNOB").knob
        system_var = UEFIVariable("INTEGER_KNOB", uuid.UUID(NS1), knob.format.object_to_binary(55))
        calls = []

        def fake_run(command):
            calls.append(command)
            with open(command[command.index("-o") + 1], "wb") as f:
                f.write(create_vlist_buffer(system_var))
            return 0

        with mock.patch.object(mcv, "run_tool", side_effect=fake_run), \
                mock.patch.object(mcv, "find_tool", side_effect=lambda name, override=None: [name]), \
                mock.patch("builtins.print") as printed:
            rc = mcv.main(["dump", "-x", sample_path(), "--from-system", "--changed-only", "--format", "csv",
                           "--ignore-fw-xml-hash-mismatch"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], mcv.READ_TOOL_NAME)
        self.assertIn("--ignore-fw-xml-hash-mismatch", calls[0])
        self.assertEqual(printed.call_args.args[0].splitlines()[1], f"{NS1},INTEGER_KNOB,uint32_t,55,100,modified")

    def test_dump_current_mixes_system_and_defaults(self):
        schema = Schema.load(sample_path())
        knob = schema.get_knob(uuid.UUID(NS1), "INTEGER_KNOB").knob
        system_var = UEFIVariable("INTEGER_KNOB", uuid.UUID(NS1), knob.format.object_to_binary(55))

        def fake_run(command):
            with open(command[command.index("-o") + 1], "wb") as f:
                f.write(create_vlist_buffer(system_var))
            return 0

        with mock.patch.object(mcv, "run_tool", side_effect=fake_run), \
                mock.patch.object(mcv, "find_tool", return_value=["read"]), \
                mock.patch("builtins.print") as printed:
            rc = mcv.main(["dump", "-x", sample_path(), "--current", "-f", "INTEGER_KNOB", "-f", "BOOLEAN_KNOB",
                           "--format", "csv"])
        self.assertEqual(rc, 0)
        lines = printed.call_args.args[0].splitlines()
        self.assertEqual(lines[1], f"{NS1},INTEGER_KNOB,uint32_t,55,100,modified")
        self.assertEqual(lines[2], f"{NS1},BOOLEAN_KNOB,bool,true,true,not-set")

    def test_dump_current_falls_back_when_system_unreadable(self):
        for patches in ({"run_tool": 1}, {"run_tool": 255}):
            with mock.patch.object(mcv, "find_tool", return_value=["read"]), \
                    mock.patch.object(mcv, "run_tool", return_value=patches["run_tool"]), \
                    mock.patch("builtins.print") as printed:
                rc = mcv.main(["dump", "-x", sample_path(), "--current", "-f", "INTEGER_KNOB"])
            self.assertEqual(rc, 0)
            self.assertEqual(printed.call_args.args[0].splitlines()[1].split()[-3:], ["100", "100", "not-set"])

        # Tool not found
        with mock.patch.object(mcv, "find_tool", side_effect=mcv.ModifyError("not found")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(mcv.main(["dump", "-x", sample_path(), "--current", "-f", "INTEGER_KNOB"]), 0)
        self.assertEqual(printed.call_args.args[0].splitlines()[1].split()[-1], "not-set")

        # --from-system stays strict
        with mock.patch.object(mcv, "find_tool", return_value=["read"]), \
                mock.patch.object(mcv, "run_tool", return_value=1):
            self.assertEqual(mcv.main(["dump", "-x", sample_path(), "--from-system"]), 1)

    def test_dump_current_conflicts_with_other_sources(self):
        for other in (["-i", sample_path()], ["--from-system"]):
            with self.assertRaises(SystemExit):
                mcv.main(["dump", "-x", sample_path(), "--current"] + other)
        with self.assertRaises(SystemExit):
            mcv.main(["modify", "-x", sample_path(), "-o", "o.vl", "--current"])

    def test_dump_from_empty_system(self):
        with mock.patch.object(mcv, "run_tool", return_value=255), \
                mock.patch.object(mcv, "find_tool", return_value=["read"]), \
                mock.patch("builtins.print") as printed:
            rc = mcv.main(["dump", "-x", sample_path(), "--from-system", "--filter", "BOOLEAN_KNOB"])
        self.assertEqual(rc, 0)
        self.assertEqual(printed.call_args.args[0].splitlines()[1].split()[-1], "not-set")


if __name__ == "__main__":
    unittest.main()
