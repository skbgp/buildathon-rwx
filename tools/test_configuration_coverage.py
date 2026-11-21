"""Regression checks for conservative configuration and callable selection."""
import ast
import unittest
from types import SimpleNamespace

from blast_radius.configuration import changed_keys
from blast_radius.dispatch import changed_site
from blast_radius.map_reads import ObservedMapping, readers
from blast_radius.replay import equivalent


class TestConfigurationCoverage(unittest.TestCase):
    def setUp(self):
        self.site = "module.py:2:8"
        self.change = SimpleNamespace(old_lines={2}, insertion_anchors=set())

    def keys(self, before, after):
        return changed_keys("module.py", self.change, ast.parse(before), ast.parse(after))

    def test_value_only_and_same_shape(self):
        old = 'class C:\n    D = {"a": (1, 2)}\n'
        changed = old.replace("1, 2", "True, 2")
        self.assertEqual(self.keys(old, changed), (self.site, {"a"}))
        self.assertIsNone(self.keys(old, changed.replace('"a"', '"b"')))
        self.assertIsNone(self.keys(old, changed + '    ALIAS = D\n'))
        self.assertIsNone(self.keys(old + '    ALIAS = D\n', changed + '    ALIAS = D\n'))

    def test_membership_does_not_consume_value(self):
        site = "synthetic"
        readers.pop(site, None)
        table = ObservedMapping({"x": 1}, site)
        self.assertIn("x", table)
        self.assertEqual(readers[site], {})
        self.assertEqual(table.get("x"), 1)
        self.assertEqual(readers[site][""], {"x"})
        self.assertEqual(table.copy(), {"x": 1})
        self.assertIn(None, readers[site][""])

    def test_factory_binding_and_argument_arity(self):
        old = 'class C:\n    H = {"x": make("x", target=True)}\n'
        new = old.replace("True", "False")
        key = "module.py:2:14"
        find = lambda body: changed_site("module.py", self.change, ast.parse(old), ast.parse(body), {key: {}})
        self.assertEqual(find(new), key)
        self.assertIsNone(find(new.replace("make(", "other(")))
        self.assertIsNone(find(new.replace("target=False", "other=False")))
        self.assertIsNone(find(new + "flag = True\n"))

    def test_version_threshold(self):
        op = [["int_tuple", 23, 12], ["int_tuple", 23, 12], False]
        self.assertFalse(equivalent([op], ast.Lt, ast.LtE, []))
        op[0] = ["int_tuple", 22, 12]
        self.assertTrue(equivalent([op], ast.Lt, ast.LtE, []))

    def test_class_mapping_bulk_and_import_dependencies(self):
        from blast_radius.select import Selector
        from blast_radius.patches import parse_patch
        from difflib import unified_diff
        path = "sqlglot/dialects/example.py"
        before = 'class C:\n    D = {"x": 1}\n'
        after = before.replace("1", "2")
        diff = "".join(unified_diff(before.splitlines(True), after.splitlines(True), "a/" + path, "b/" + path))
        change, = parse_patch(diff)
        selector = Selector.__new__(Selector)
        selector.configuration_paths = {path}
        site = f"{path}:2:8"
        for reads in ({"*": [7]}, {"key:x": [-1]}, {}):
            selector.configurations = {site: reads}
            self.assertIsNone(selector.configuration_consumers(path, change, ast.parse(before), ast.parse(after)))

    def test_handler_change_outside_original_body(self):
        old = 'class C:\n    H = {"x": lambda a: abs(a)}\n'
        new = old.replace("lambda a:", "lambda a=1:")
        key = "module.py:2:14"
        self.assertIsNone(changed_site("module.py", self.change, ast.parse(old), ast.parse(new), {key: {}}))
