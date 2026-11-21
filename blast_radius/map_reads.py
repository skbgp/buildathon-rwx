"""Record configuration value reads in the clean Python suite."""

import ast
import os
import sys

from .configuration import definitions


# A string key is an exact lookup. None represents a bulk or opaque read.
# The caller supplies the active test context so the map can be built both
# in the main interpreter and in workers that execute parts of a test.
readers = {}
active_context = lambda: ""


_source = os.environ.get("RWX_OBSERVE_ROOT", "") + "/sqlglot/"


def _record(site, key=None):
    node = active_context()
    if node:
        # SQLGlot loads some dialects on demand, while a test is running.
        # Configuration consumed by module initialization can reach later tests.
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "<module>" and frame.f_code.co_filename.startswith(_source):
                node = ""
                break
            frame = frame.f_back
    readers.setdefault(site, {}).setdefault(node, set()).add(key)


class ObservedMapping(dict):
    def __init__(self, entries, site):
        super().__init__(entries)
        self.site = site
        readers.setdefault(site, {})

    def __getitem__(self, key):
        _record(self.site, key if type(key) is str else None)
        return super().__getitem__(key)

    def get(self, key, default=None):
        _record(self.site, key if type(key) is str else None)
        return super().get(key, default)

    def __iter__(self):
        # A consumer may put the result into a C-backed dict without using
        # our __getitem__. Charge bulk reads conservatively.
        _record(self.site)
        return super().__iter__()

    def keys(self):
        _record(self.site)
        return super().keys()

    def values(self):
        _record(self.site)
        return super().values()

    def items(self):
        _record(self.site)
        return super().items()

    def __eq__(self, other):
        _record(self.site)
        if isinstance(other, ObservedMapping):
            _record(other.site)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        _record(self.site)
        if isinstance(other, ObservedMapping):
            _record(other.site)
        return dict.__ne__(self, other)

    def __repr__(self):
        _record(self.site)
        return dict.__repr__(self)

    def copy(self):
        _record(self.site)
        return dict.copy(self)

    def __or__(self, other):
        _record(self.site)
        if isinstance(other, ObservedMapping):
            _record(other.site)
        return dict.__or__(self, other)

    def __ror__(self, other):
        _record(self.site)
        return dict.__ror__(self, other)

    def __ior__(self, other):
        _record(self.site)
        return dict.__ior__(self, other)

    def __reversed__(self):
        _record(self.site)
        return dict.__reversed__(self)

    def __reduce_ex__(self, protocol):
        _record(self.site)
        return dict, (dict(self),)

    def setdefault(self, key, default=None):
        _record(self.site)
        return dict.setdefault(self, key, default)

    def pop(self, *args):
        _record(self.site)
        return dict.pop(self, *args)

    def popitem(self):
        _record(self.site)
        return dict.popitem(self)

    def update(self, *args, **kwargs):
        _record(self.site)
        return dict.update(self, *args, **kwargs)

    def __setitem__(self, key, value):
        _record(self.site)
        return dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        _record(self.site)
        return dict.__delitem__(self, key)

    def clear(self):
        _record(self.site)
        return dict.clear(self)


def instrument(tree, path, helper):
    found = False
    for _, node, (_, value, _) in definitions(tree):
        site = f"{path}:{value.lineno}:{value.col_offset}"
        call = ast.Call(ast.Name(helper, ast.Load()), [value, ast.Constant(site)], [])
        node.value = ast.copy_location(call, value)
        found = True
    return found


def snapshot():
    return {site: {node: sorted(keys, key=lambda key: (key is not None, key))
                   for node, keys in nodes.items()}
            for site, nodes in readers.items()}
