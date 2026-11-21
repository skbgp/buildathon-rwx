import ast
import difflib
import unittest

from blast_radius import lazy
from blast_radius.equivalence import same_patched_module
from blast_radius.insertions import following_lines
from blast_radius.patches import parse_patch
from blast_radius.select import SourceShape


def inputs(source, patched):
    diff = difflib.unified_diff(source.splitlines(True), patched.splitlines(True), "a/a.py", "b/a.py")
    change, = parse_patch("".join(diff))
    return change, ast.parse(source, type_comments=True), ast.parse(patched, type_comments=True)


class TestBranchCoverage(unittest.TestCase):
    source = "def f(x):\n    if x:\n        return x\n    return y  # type: ignore\n"

    def witness(self, inserted):
        change, old, new = inputs(self.source, self.source.replace("        return x", inserted + "        return x"))
        return following_lines(change, old, new, SourceShape(old), {"lines": {"3": 0}})

    def test_branch_insertion(self):
        self.assertEqual(list(self.witness("        x = 2\n")), [3])

    def test_changed_scope(self):
        for statement in ("y = 1", "yield 1", "if (y := 1): return x"):
            self.assertIsNone(self.witness("        " + statement + "\n"))

    def test_shifted_type_ignore(self):
        change, old, new = inputs(self.source, "# documentation\n" + self.source)
        self.assertTrue(same_patched_module(old, new, change))
        change, old, new = inputs(self.source, self.source.replace("  # type: ignore", "") + "# type: ignore\n")
        self.assertFalse(same_patched_module(old, new, change))

    def test_short_circuit(self):
        source = "def f(a, b):\n    return a and b(1)\n"
        change, old, new = inputs(source, source.replace("b(1)", "not b(2)"))
        site = {"a.py:2:17": {"key:visit": [3]}}
        self.assertEqual(lazy.changed_consumers("a.py", change, old, new, site, SourceShape(old)), [3])
        site["a.py:2:17"]["key:visit"].append(-1)
        self.assertIsNone(lazy.changed_consumers("a.py", change, old, new, site, SourceShape(old)))

    def test_observer_marks_only_the_entered_operand(self):
        tree = ast.parse("def f(enabled, get):\n    return enabled and get()\n")
        calls = []
        self.assertTrue(lazy.instrument(tree, "a.py", "observe"))
        namespace = {"observe": calls.append}
        exec(compile(ast.fix_missing_locations(tree), "<test>", "exec"), namespace)
        def fail():
            raise ValueError("entered")
        self.assertFalse(namespace["f"](False, fail))
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            namespace["f"](True, fail)
        self.assertEqual(calls, ["a.py:2:23"])

    def test_changed_outer_condition(self):
        source = "def f(a, b):\n    return a and b(1)\n"
        change, old, new = inputs(source, source.replace("a and b(1)", "a or b(2)"))
        self.assertIsNone(lazy.changed_consumers("a.py", change, old, new, {"a.py:2:17": {}}, SourceShape(old)))
