"""Observe operand values during clean coverage collection, before any patch exists."""

import ast
from enum import Enum
from importlib.machinery import PathFinder, SourceFileLoader
import os
from pathlib import Path
import sys
import weakref

from . import dispatch, map_reads
from .replay import candidate

active_node = ""
observations = {}
_owner = os.getpid()
_pending = {}
_BUILTINS = frozenset((type(None), bool, int, str, list, tuple, dict, set, frozenset, bytes, float, complex))
_dict = type.__dict__["__dict__"].__get__
_mro = type.__dict__["__mro__"].__get__


def _representation(cls):
    mro = _mro(cls)
    def slot(name):
        return next((_dict(base)[name] for base in mro if name in _dict(base)), None)
    eq, ne = slot("__eq__"), slot("__ne__")
    if eq is object.__eq__ and ne is object.__ne__:
        return "object"
    if any(base is Enum for base in mro) and eq is str.__eq__ and ne is str.__ne__:
        return "str"
    return "unknown"


def specimen(value):
    if value is None:
        return ("none",)
    cls = type(value)
    if cls is bool or cls is int:
        return ("int", int(value)) if value.bit_length() <= 64 else None
    if cls is str:
        return ("str", value) if len(value) <= 64 else None
    if cls is tuple and len(value) <= 8 and all(type(v) is int and v.bit_length() <= 64 for v in value):
        return ("int_tuple", *value)
    # Metaclasses may customize hashing or comparison. Never invoke their
    # methods just to record an operand in the clean checkout.
    if any(cls is builtin for builtin in (list, tuple, dict, set, frozenset, bytes, float, complex)):
        return ("opaque_builtin",)
    if type(cls) is not type and type(cls) is not type(Enum):
        return None
    representation = _representation(cls)
    if representation == "str":
        raw = str.__str__(value)
        return ("str_enum", raw) if len(raw) <= 64 else None
    if representation == "object":
        return ("object",)
    return None


def left_operand(site, value):
    # Preserve the original comparison opcode and its caller frame. A weak
    # reference lets us examine an object's current comparison methods after
    # the RHS has run, without extending its lifetime if the RHS raises.
    cls = type(value)
    ref = None
    sample = None
    if type(cls) is type and cls in _BUILTINS:
        sample = specimen(value)
    else:
        try:
            ref = weakref.ref(value)
        except TypeError:
            pass
    _pending[id(sys._getframe(1)), site] = (id(value), ref, sample)
    return value


def right_operand(site, value):
    identity, ref, left = _pending.pop((id(sys._getframe(1)), site))
    by_node = observations.setdefault(site, {})
    seen = by_node.setdefault(active_node, set())
    if seen is not None:
        if ref is not None:
            left = specimen(ref())
        right = specimen(value)
        if left is None or right is None:
            by_node[active_node] = None
        else:
            seen.add((left, right, identity == id(value)))
            if len(seen) > 24:
                by_node[active_node] = None
    return value


class Transformer(ast.NodeTransformer):
    def __init__(self, path, helper):
        self.path = path
        self.helper = helper
        self.changed = False

    def visit_Compare(self, node):
        eligible = not os.environ.get("RWX_OBSERVE_MAPS") and candidate(node)
        self.generic_visit(node)
        if not eligible:
            return node
        self.changed = True
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        def observe(side, operand):
            call = ast.Call(ast.Name(self.helper + side, ast.Load()),
                            [ast.Constant(site), operand], [])
            return ast.copy_location(call, operand)

        node.left = observe("_left", node.left)
        node.comparators[0] = observe("_right", node.comparators[0])
        return node


class Loader(SourceFileLoader):
    def exec_module(self, module):
        raw = self.get_data(self.path)
        helper = "_rwx_clean_compare"
        while helper.encode() in raw:
            helper += "_"
        tree = compile(raw, self.path, "exec", ast.PyCF_ONLY_AST, dont_inherit=True)
        visit = Transformer(Path(self.path).relative_to(self.root).as_posix(), helper)
        tree = visit.visit(tree)
        maps = os.environ.get("RWX_OBSERVE_MAPS") == "1"
        if maps and map_reads.instrument(tree, visit.path, helper + "_mapping"):
            module.__dict__[helper + "_mapping"] = map_reads.ObservedMapping
            visit.changed = True
        if os.environ.get("RWX_OBSERVE_MAPS") == "dispatch" and dispatch.instrument(tree, visit.path, helper + "_call"):
            module.__dict__[helper + "_call"] = dispatch.observe
            visit.changed = True
        if not visit.changed:
            return super().exec_module(module)
        module.__dict__[helper + "_left"] = left_operand
        module.__dict__[helper + "_right"] = right_operand
        exec(compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True),
             module.__dict__)


class Finder:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if spec and type(spec.loader) is SourceFileLoader and spec.origin:
            if Path(spec.origin).is_relative_to(self.root / "sqlglot"):
                loader = Loader(fullname, spec.origin)
                loader.root = self.root
                spec.loader = loader
        return spec


def install(root):
    global _owner
    if _owner != os.getpid():
        observations.clear()
        map_reads.readers.clear()
        _owner = os.getpid()
    map_reads.active_context = lambda: active_node
    dispatch.active_context = lambda: active_node
    if os.environ.get("RWX_OBSERVE_MAPS") == "dispatch":
        dispatch.start()
    if not any(isinstance(finder, Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, Finder(root))


def encoded():
    return {site: {node: sorted(values) if values is not None else None
                   for node, values in by_node.items()}
            for site, by_node in observations.items()}
