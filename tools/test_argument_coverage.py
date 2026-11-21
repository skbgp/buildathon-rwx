import ast
import unittest

from blast_radius import arguments


def schema(value):
    return ast.parse(value, mode="eval").body


class TestArguments(unittest.TestCase):
    def test_distinguishes_valid_and_missing_values(self):
        class Example:
            arg_types = {"source": True, "option": False}
            args = {"source": 0, "option": []}

        arguments.observe(Example(), "test")
        key = Example.__module__ + "." + Example.__qualname__
        reads = arguments.records.pop(key)
        self.assertEqual(reads, {"option": {"test"}})

    def test_existing_error_is_charged_to_all_edits(self):
        class Example:
            arg_types = {"source": True}
            args = {}

        arguments.observe(Example(), "")
        key = Example.__module__ + "." + Example.__qualname__
        self.assertEqual(arguments.records.pop(key), {"source": {""}, "*": {""}})

    def test_import_and_shared_fixture_fallback(self):
        before = schema('{"x": False, "y": True}')
        after = schema('{"x": True, "y": True}')
        self.assertIsNone(arguments.changed_contexts({"x": [-1]}, before, after))
        self.assertIsNone(arguments.changed_contexts({"*": [-1]}, before, after))
        self.assertEqual(arguments.changed_contexts({"*": [0], "y": [9], "x": [3]}, before, after), {0, 3})
        self.assertIsNone(arguments.changed_contexts(None, before, after))
        self.assertIsNone(arguments.changed_contexts({}, before, schema('{"x": 1, "y": True}')))
