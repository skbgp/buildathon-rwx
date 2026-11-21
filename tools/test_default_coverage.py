import ast
from types import SimpleNamespace
import unittest

from blast_radius.defaults import contexts


class TestDefaultCoverage(unittest.TestCase):
    path = "sqlglot/helper.py"
    source = "def f(a=True, /, *, b=False):\n    return a and b\n"
    site = "sqlglot/helper.py:1:f"

    def check(self, revised, reads, original=None):
        before = ast.parse(original or self.source)
        after = ast.parse(revised)
        snapshot = ast.dump(after)
        result = contexts(self.path, SimpleNamespace(old_lines={1}, insertion_anchors=set()),
                          before, after, {self.site: reads})
        self.assertEqual(ast.dump(after), snapshot)
        return result

    def test_keyword_and_positional_only_defaults(self):
        reads = {"a": [3], "b": [4, 5], "*": [7]}
        self.assertEqual(self.check(self.source.replace("b=False", "b=True"), reads), {4, 5, 7})
        self.assertEqual(self.check(self.source.replace("a=True", "a=False"), reads), {3, 7})
        self.assertEqual(self.check(self.source.replace("b=False", "b=True"), {"b": []}), set())

    def test_incomplete_or_shared_profile(self):
        edited = self.source.replace("b=False", "b=True")
        for reads in ({}, {"b": [-1]}, {"b": [2], "*": [-1]}):
            self.assertIsNone(self.check(edited, reads))

    def test_nonisolated_edits_and_different_types(self):
        edited = self.source.replace("b=False", "b=True")
        for source in (edited.replace("a and b", "a or b"), edited.replace("a=True", "a=False"),
                       self.source.replace("False", "0"), self.source.replace("b=False", "b=make()")):
            self.assertIsNone(self.check(source, {"a": [2], "b": [3]}))

    def test_generator_uses_fallback(self):
        original = self.source.replace("return", "yield")
        self.assertIsNone(self.check(original.replace("b=False", "b=True"), {"b": []}, original))
