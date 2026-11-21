"""Track class and instance reads of configuration options on SQLGlot dialects."""

import ast
from collections import defaultdict
import gzip
from importlib.machinery import PathFinder, SourceFileLoader
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import re
import sys

from . import operands

_module = "sqlglot.dialects.dialect"
_records = defaultdict(lambda: defaultdict(set))
_ancestry = {}
_process = 0
_imports = 0


def ancestry(cls):
    identity = id(cls)
    if identity not in _ancestry:
        chain = []
        for base in type.__getattribute__(cls, "__mro__"):
            module = type.__getattribute__(base, "__module__")
            if module.startswith("sqlglot.dialects."):
                name = module + "." + type.__getattribute__(base, "__qualname__")
                chain.append(name)
                _records[name]
        _ancestry[identity] = (cls, chain)
    return _ancestry[identity][1]


def record(cls, name):
    context = "" if _imports else operands.active_node
    for base in ancestry(cls):
        _records[base][name].add(context)


def read_class(cls, name):
    if name.isupper() or name == "__dict__":
        record(cls, "*" if name == "__dict__" else name)
    return type.__getattribute__(cls, name)


def read_instance(instance, name):
    if name.isupper():
        record(type(instance), name)
    return object.__getattribute__(instance, name)


def named_namespace(obj, key):
    # This is used only for a fixed literal-key lookup. Other class mapping
    # reads conservatively mark the entire class as observed.
    if isinstance(obj, type) and type(obj).__getattribute__ is read_class:
        record(obj, key)
        return type.__getattribute__(obj, "__dict__")
    return obj.__dict__


class Rewrite(ast.NodeTransformer):
    def visit_Call(self, node):
        self.generic_visit(node)
        op = node.func
        if (isinstance(op, ast.Attribute) and op.attr == "get" and isinstance(op.value, ast.Attribute)
                and op.value.attr == "__dict__" and node.args
                and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str):
            expr = op.value
            op.value = ast.copy_location(ast.Call(ast.Name("_rwx_named_namespace", ast.Load()),
                                                  [expr.value, node.args[0]], []), expr)
        return node

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        names = {"Dialect": "_rwx_read_instance", "_Dialect": "_rwx_read_class"}
        if node.name in names:
            if any(isinstance(n, ast.FunctionDef) and n.name == "__getattribute__" for n in node.body):
                raise ValueError("upstream dialect already has a custom lookup")
            assignment = ast.Assign([ast.Name("__getattribute__", ast.Store())],
                                    ast.Name(names[node.name], ast.Load()))
            node.body.append(ast.copy_location(assignment, node.body[-1]))
        return node


class Loader(SourceFileLoader):
    def exec_module(self, module):
        raw = self.get_data(self.path)
        tree = compile(raw, self.path, "exec", ast.PyCF_ONLY_AST, dont_inherit=True)
        module.__dict__.update(_rwx_read_instance=read_instance, _rwx_read_class=read_class,
                               _rwx_named_namespace=named_namespace)
        tree = Rewrite().visit(tree)
        exec(compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True), module.__dict__)


class ImportScope(SourceFileLoader):
    def exec_module(self, module):
        global _imports
        _imports += 1
        try:
            return super().exec_module(module)
        finally:
            _imports -= 1


class DialectScope(Loader):
    def exec_module(self, module):
        global _imports
        _imports += 1
        try:
            return super().exec_module(module)
        finally:
            _imports -= 1


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = PathFinder.find_spec(fullname, path)
        if not spec or type(spec.loader) is not SourceFileLoader:
            if fullname == _module:
                raise RuntimeError("unrecognized dialect loader")
            return None
        loader = DialectScope if fullname == _module else ImportScope
        spec.loader = loader(fullname, spec.origin)
        return spec


def install():
    global _process
    argv = sys.orig_argv
    if "-c" in argv:
        code = argv[argv.index("-c") + 1]
        if re.fullmatch(r"from multiprocessing\.resource_tracker import main;main\(\d+\)", code):
            return
    if _process == os.getpid():
        return
    _process = os.getpid()
    from . import operand_index
    operand_index.install = install
    sys.meta_path.insert(0, Finder())
    output = Path(os.environ["RWX_OBSERVE_OUTPUT"], f"scalar-{_process}.json")
    output.touch()

    def save():
        capture = {owner: {key: sorted(items) for key, items in reads.items()}
                   for owner, reads in _records.items()}
        output.write_text(json.dumps(capture, separators=(",", ":")))

    Finalize(None, save, exitpriority=1)


class Fixtures:
    """Mark values that may reach any member of a shared scope as uncertain."""

    import pytest

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if request.scope == "function":
            return (yield)
        prior = operands.active_node
        teardown_prior = []

        def begin_teardown():
            teardown_prior.append(operands.active_node)
            operands.active_node = ""

        def end_teardown():
            operands.active_node = teardown_prior.pop()

        request.addfinalizer(end_teardown)
        operands.active_node = ""
        try:
            return (yield)
        finally:
            operands.active_node = prior
            request.addfinalizer(begin_teardown)


def run():
    # Reuse the existing runner to preserve node identity around process pool
    # work, including serialization in both directions.
    import pytest
    from . import operand_index
    main = pytest.main

    def run_with_fixtures(args, plugins):
        return main(args, plugins=[*plugins, Fixtures()])

    pytest.main = run_with_fixtures
    try:
        return operand_index.run()
    finally:
        pytest.main = main


def merge(directory, output):
    nodes = json.loads((directory / "nodes.json").read_text())
    ids = {node: n for n, node in enumerate(nodes)}
    merged = defaultdict(lambda: defaultdict(set))
    paths = list(directory.glob("scalar-*.json"))
    if not paths:
        raise ValueError("no scalar captures")
    for path in paths:
        if not path.stat().st_size:
            raise ValueError(f"incomplete scalar worker capture: {path}")
        for owner, attributes in json.loads(path.read_text()).items():
            target = merged[owner]
            for key, items in attributes.items():
                target[key].update(ids.get(item, -1) for item in items)
    encoded = {owner: {key: sorted(group) for key, group in records.items()}
               for owner, records in sorted(merged.items())}
    with gzip.GzipFile(str(output), "wb", mtime=0) as stream:
        stream.write(json.dumps({"nodes": nodes, "scalars": encoded}, separators=(",", ":")).encode())


if __name__ == "__main__":
    merge(Path(sys.argv[1]), Path(sys.argv[2]))
