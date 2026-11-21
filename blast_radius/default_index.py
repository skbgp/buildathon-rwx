"""Record clean tests that could observe a function's original parameter default."""

import ast
from collections import defaultdict
import importlib.machinery
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import sys

from . import operand_index, operands


_codes = {}
_functions = {}
_reads = defaultdict(lambda: defaultdict(set))
_owner = None
_import_depth = 0
_tool = 3
_scalar_types = (type(None), bool, int, str)


def set_context(name):
    if name != operands.active_node:
        operands.active_node = name
        sys.monitoring.restart_events()


def context():
    return "" if _import_depth else operands.active_node


def register(function, site):
    positional_defaults = function.__defaults__ or ()
    code = function.__code__
    values = dict(function.__kwdefaults__ or {})
    if positional_defaults:
        positional = code.co_varnames[:code.co_argcount]
        values.update(zip(positional[-len(positional_defaults):], positional_defaults))
    values = {name: value for name, value in values.items() if type(value) in _scalar_types}
    if values:
        for name in values:
            _reads[site][name]
        _functions[id(function)] = function, site
        _codes[id(code)] = code, site, values
        sys.monitoring.set_local_events(_tool, code, sys.monitoring.events.PY_START)


def entered(code, offset):
    _, site, expected = _codes[id(code)]
    node = context()
    reads = _reads[site]
    frame = sys._getframe(1)
    if frame.f_code is not code:
        reads["*"].add(node)
        return
    for name, default in expected.items():
        if node in reads[name]:
            continue
        value = frame.f_locals[name]
        if type(value) is type(default) and value == default:
            reads[name].add(node)
    if all(node in reads[name] for name in expected):
        return sys.monitoring.DISABLE


def audit(event, args):
    if event not in ("object.__getattr__", "object.__setattr__", "object.__delattr__") or len(args) < 2:
        return
    attribute = args[1]
    if attribute not in ("__defaults__", "__kwdefaults__", "__code__"):
        return
    entry = _functions.get(id(args[0]))
    if entry is None:
        return
    if event != "object.__getattr__":
        # Replacing a code object or its defaults can affect later callers.
        _reads[entry[1]]["*"].add("")
    elif attribute != "__code__":
        _reads[entry[1]]["*"].add(context())


def instrument(body, path):
    symbol = "_rwx_profile_default"
    for index in reversed(range(len(body))):
        stmt = body[index]
        if isinstance(stmt, ast.ClassDef):
            instrument(stmt.body, path)
        elif isinstance(stmt, ast.FunctionDef) and not stmt.decorator_list:
            defaults = (*stmt.args.defaults, *stmt.args.kw_defaults)
            if any(isinstance(d, ast.Constant) and type(d.value) in _scalar_types for d in defaults):
                site = f"{path}:{stmt.lineno}:{stmt.name}"
                call = ast.Call(ast.Name(symbol, ast.Load()),
                                [ast.Name(stmt.name, ast.Load()), ast.Constant(site)], [])
                body.insert(index + 1, ast.copy_location(ast.Expr(call), stmt))


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        global _import_depth
        path = Path(self.path).relative_to(os.environ["RWX_OBSERVE_ROOT"]).as_posix()
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        instrument(tree.body, path)
        module.__dict__["_rwx_profile_default"] = register
        previous = operands.active_node
        set_context("")
        _import_depth += 1
        try:
            exec(compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True), module.__dict__)
        finally:
            _import_depth -= 1
            set_context(previous)


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and type(spec.loader) is importlib.machinery.SourceFileLoader:
                spec.loader = Loader(fullname, spec.origin)
                return spec


def install():
    global _owner
    if _owner == os.getpid() or any("multiprocessing.resource_tracker import main" in arg for arg in sys.orig_argv):
        return
    _owner = os.getpid()
    operand_index.install = install
    operand_index.set_context = set_context
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(audit)
    sys.monitoring.use_tool_id(_tool, "baseline default coverage")
    sys.monitoring.register_callback(_tool, sys.monitoring.events.PY_START, entered)
    output = Path(os.environ["RWX_OBSERVE_OUTPUT"], f"scalar-{_owner}.json")
    output.touch()

    def save():
        snapshot = {site: {name: sorted(nodes) for name, nodes in reads.items()}
                    for site, reads in _reads.items()}
        output.write_text(json.dumps(snapshot))

    Finalize(None, save, exitpriority=-5)
