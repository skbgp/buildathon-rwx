"""Regression checks for equality overloads, reflected operators, and identity."""

import ast
from enum import Enum
import unittest
import sys

from blast_radius.operands import Transformer, left_operand, right_operand, specimen
from blast_radius.replay import equivalent


def observations(left, right):
    a, b = specimen(left), specimen(right)
    return None if a is None or b is None else [[list(a), list(b), left is right]]


class TestOperandReplay(unittest.TestCase):
    def test_runtime_replacement_of_comparison_method(self):
        class Parent:
            pass

        class Child(Parent):
            pass

        value = Child()
        self.assertTrue(equivalent(observations(value, None), ast.Is, ast.Eq, []))
        Parent.__eq__ = lambda self, other: other is None
        self.assertTrue(value == None)
        self.assertFalse(equivalent(observations(value, None), ast.Is, ast.Eq, []))

    def test_str_enum_and_an_equal_plain_string(self):
        class Unit(str, Enum):
            METRE = "m"

        self.assertTrue(equivalent(observations(Unit.METRE, Unit.METRE), ast.Is, ast.Eq, []))
        self.assertFalse(equivalent(observations(Unit.METRE, "m"), ast.Is, ast.Eq, []))

    def test_unknown_reflected_comparison(self):
        class Reflected:
            def __eq__(self, other):
                return True
        self.assertFalse(equivalent(observations(None, Reflected()), ast.Is, ast.Eq, []))

    def test_bool_and_int_share_value_but_have_different_identities(self):
        self.assertFalse(equivalent(observations(True, 1), ast.Eq, ast.Is, []))

    def test_integer_threshold_on_observed_values(self):
        edits = [("right", 3)]
        self.assertTrue(equivalent(observations(5, 2), ast.Lt, ast.Lt, edits))
        self.assertFalse(equivalent(observations(2, 2), ast.Lt, ast.Lt, edits))

    def test_unknown_builtin_only_compared_to_none(self):
        self.assertTrue(equivalent(observations([], None), ast.Is, ast.Eq, []))
        self.assertFalse(equivalent(observations([], []), ast.Is, ast.Eq, []))

    def test_observer_preserves_comparison_caller_and_order(self):
        visits = []

        class Value:
            def __eq__(self, other):
                visits.append("compare")
                return sys._getframe(1).f_code.co_name == "evaluate"

        class Source:
            @property
            def value(self):
                visits.append("operand")
                return Value()

        tree = ast.parse("def evaluate(self): return self.value == 3")
        tree = Transformer("example.py", "trace").visit(tree)
        namespace = {"trace_left": left_operand, "trace_right": right_operand}
        exec(compile(ast.fix_missing_locations(tree), "example.py", "exec"), namespace)
        self.assertTrue(namespace["evaluate"](Source()))
        self.assertEqual(visits, ["operand", "compare"])
