import ast
import sys
import types
import unittest

from blast_radius import flag_guards


class TestFlagGuards(unittest.TestCase):
    def test_short_circuit_directions(self):
        for expression, masks_when in (("self.A and x", False), ("self.A or x", True),
                                      ("not self.A and not x", True), ("self.A or not x", False)):
            tree = ast.parse("if " + expression + ": pass")
            flag_guards.annotate(tree)
            helper, = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            self.assertIs(helper.args[-1].value, masks_when)

    def test_does_not_touch_nonboolean_context(self):
        tree = ast.parse("result = self.A and value()")
        before = ast.dump(tree)
        flag_guards.annotate(tree)
        self.assertEqual(before, ast.dump(tree))

    def test_pure_type_check(self):
        module = types.ModuleType("demo")
        module.Target = type("Target", (), {})
        value = object()
        self.assertTrue(flag_guards.masked(sys._getframe(), "instance", "value", "module", "Target", False))
        value = module.Target()
        self.assertFalse(flag_guards.masked(sys._getframe(), "instance", "value", "module", "Target", False))

    def test_observable_truth_and_class_checks(self):
        class Observable:
            def __bool__(self):
                raise AssertionError("must not probe truth")

            @property
            def __class__(self):
                raise AssertionError("must not probe class")

        value = Observable()
        module = types.ModuleType("demo")
        module.Target = int
        self.assertFalse(flag_guards.masked(sys._getframe(), "truth", "value", "", "", False))
        self.assertFalse(flag_guards.masked(sys._getframe(), "instance", "value", "module", "Target", False))
