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
from VariableList import Schema, UEFIVariable, read_vlist, create_vlist_buffer      # noqa: E402

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


class ToolIntegrationTests(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe_name = mcv.READ_TOOL_NAME + (".exe" if os.name == "nt" else "")

    def fake_script_dir(self, *file_names):
        """Make find_tool search a temporary folder instead of the folder of the real tools."""
        for name in file_names:
            open(os.path.join(self.tmp.name, name), "w").close()
        patcher = mock.patch.object(mcv, "__file__", os.path.join(self.tmp.name, "ModifyConfVarList.py"))
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

    def test_find_tool_prefers_executable_next_to_script(self):
        self.fake_script_dir(self.exe_name, mcv.READ_TOOL_NAME + ".py")
        with mock.patch("shutil.which", return_value="C:/bin/" + self.exe_name):
            self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME), [os.path.join(self.tmp.name, self.exe_name)])

    def test_find_tool_prefers_executable_in_path_over_script(self):
        self.fake_script_dir(mcv.READ_TOOL_NAME + ".py")
        with mock.patch("shutil.which", return_value="C:/bin/" + self.exe_name):
            self.assertEqual(mcv.find_tool(mcv.READ_TOOL_NAME), ["C:/bin/" + self.exe_name])

    def test_find_tool_falls_back_to_script(self):
        script = os.path.join(self.tmp.name, mcv.READ_TOOL_NAME + ".py")
        self.fake_script_dir(mcv.READ_TOOL_NAME + ".py")
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
