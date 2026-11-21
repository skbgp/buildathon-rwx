"""Account for pytest dependencies shared by several items in the baseline trace."""

from __future__ import annotations

import ast
from collections import defaultdict
from functools import lru_cache


_CLASS_HOOKS = {"setUpClass", "tearDownClass", "setup_class", "teardown_class"}
_MODULE_HOOKS = {"setUpModule", "tearDownModule", "setup_module", "teardown_module"}


def _eager_imports(nodes, scope=()):
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield scope, ast.dump(node)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            children = getattr(node, field, None)
            if isinstance(children, list) and children:
                yield from _eager_imports(children, (*scope, (type(node).__name__, field)))


def eager_imports_changed(old_tree, new_tree):
    """A failed eager import can stop collection across unrelated dialects."""
    return tuple(_eager_imports(old_tree.body)) != tuple(_eager_imports(new_tree.body))


@lru_cache(maxsize=2)
def _fixture_owners(selector):
    """Map the item that happened to run a shared fixture to its whole scope."""
    owners = defaultdict(set)
    for path, numbers in selector.by_file.items():
        source = (selector.source / path).read_bytes()
        if not any(name.encode() in source for name in _CLASS_HOOKS | _MODULE_HOOKS):
            continue
        info = selector.files.get(path)
        if not info:
            continue
        module = ast.parse(source, filename=path)

        def add_scope(function, scope):
            # Definition and decorator lines run on import. Only the body
            # identifies the actual pytest context that executed the hook.
            body = range(function.body[0].lineno, function.end_lineno + 1)
            for owner in selector.lines(info, body):
                owners[owner].update(scope)

        for node in module.body:
            if isinstance(node, ast.FunctionDef) and node.name in _MODULE_HOOKS:
                add_scope(node, numbers)
            elif isinstance(node, ast.ClassDef):
                prefix = f"{path}::{node.name}::"
                scope = {number for number in numbers
                         if selector.by_number[number].node.startswith(prefix)}
                for method in node.body:
                    if isinstance(method, ast.FunctionDef) and method.name in _CLASS_HOOKS:
                        add_scope(method, scope)
    return dict(owners)


def shared_fixture_consumers(selector, readers):
    """A fixture opened during setUpClass can affect the entire test class."""
    if not readers:
        return set()
    owners = _fixture_owners(selector)
    return {number for reader in readers for number in owners.get(reader, ())}
