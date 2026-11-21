"""Per-test invocation coverage for functions stored in SQLGlot dispatch tables."""

import ast
from functools import lru_cache
import sys
import types

from .equivalence import same_ast, same_module

sites = {}
_codes = {}
_monitor_id = None
active_context = lambda: ""


def _entered(code, offset):
    key = _codes.get(id(code))
    if key is not None:
        sites[key[0]].add(active_context())


def observe(function, site, factory=None):
    if type(function) is not types.FunctionType or _monitor_id is None:
        return function
    if factory is not None:
        if type(factory) is not types.FunctionType or not pure_factory(factory.__code__):
            return function
        if function.__code__ not in factory.__code__.co_consts:
            return function
    # Give closures from separate factory calls their own monitoring state.
    # All executable instructions, locations, and names stay the same.
    code = function.__code__.replace()
    function.__code__ = code
    _codes[id(code)] = (site, code)
    sites.setdefault(site, set())
    sys.monitoring.set_local_events(_monitor_id, code, sys.monitoring.events.PY_START)
    return function


@lru_cache(maxsize=256)
def pure_factory(code):
    # Only straight-line creation of an inner function can defer all behavior
    # to its caller. Defaults, branching, calls and decorator use are excluded.
    import dis
    allowed = {"MAKE_CELL", "RESUME", "LOAD_CLOSURE", "LOAD_FAST", "LOAD_FAST_BORROW",
               "BUILD_TUPLE", "LOAD_CONST", "MAKE_FUNCTION", "SET_FUNCTION_ATTRIBUTE",
               "STORE_FAST", "RETURN_VALUE", "COPY_FREE_VARS", "NOP"}
    instructions = list(dis.get_instructions(code))
    return (sum(op.opname == "MAKE_FUNCTION" for op in instructions) == 1
            and all(op.opname in allowed for op in instructions)
            and all(op.arg in (4, 8) for op in instructions if op.opname == "SET_FUNCTION_ATTRIBUTE")
            and sum(isinstance(v, types.CodeType) for v in code.co_consts) == 1)


def start():
    global _monitor_id
    if not hasattr(sys, "monitoring") or _monitor_id is not None:
        return
    for i in range(6):
        if sys.monitoring.get_tool(i) is None:
            sys.monitoring.use_tool_id(i, "dispatch coverage")
            sys.monitoring.register_callback(i, sys.monitoring.events.PY_START, _entered)
            _monitor_id = i
            return


def entries(tree):
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for assignment in cls.body:
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                continue
            mapping = assignment.value
            if not isinstance(mapping, ast.Dict):
                continue
            for pos, value in enumerate(mapping.values):
                if isinstance(value, ast.Lambda) or (isinstance(value, ast.Call)
                                                      and isinstance(value.func, ast.Name)):
                    yield mapping, pos, value


def instrument(tree, path, helper):
    locations = list(entries(tree))
    for mapping, pos, value in locations:
        site = f"{path}:{value.lineno}:{value.col_offset}"
        args = [value, ast.Constant(site)]
        if isinstance(value, ast.Call):
            args.append(ast.Name(value.func.id, ast.Load()))
        mapping.values[pos] = ast.copy_location(ast.Call(ast.Name(helper, ast.Load()), args, []), value)
    return bool(locations)


def _literal_same_type(a, b):
    # Restrict factory edits to a change in bound scalar arguments.
    return (same_ast(a, b) or isinstance(a, ast.Constant) and isinstance(b, ast.Constant)
            and type(a.value) is type(b.value))


def deferred_change(a, b):
    if isinstance(a, ast.Lambda):
        return isinstance(b, ast.Lambda) and same_ast(a.args, b.args)
    return (isinstance(b, ast.Call) and same_ast(a.func, b.func)
            and len(a.args) == len(b.args) and len(a.keywords) == len(b.keywords)
            and all(_literal_same_type(x, y) for x, y in zip(a.args, b.args))
            and all(x.arg == y.arg and _literal_same_type(x.value, y.value)
                    for x, y in zip(a.keywords, b.keywords)))


def _enclosing_entries(tree, first, last):
    # Only class bodies and direct dictionary assignments can contain one
    # of the indexed handlers. Avoid walking every expression in a parser.
    for stmt in tree.body:
        if not stmt.lineno <= first <= last <= stmt.end_lineno:
            continue
        if isinstance(stmt, ast.ClassDef):
            yield from _enclosing_entries(stmt, first, last)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Dict):
            for pos, expr in enumerate(stmt.value.values):
                if expr.lineno <= first <= last <= expr.end_lineno:
                    yield stmt.value, pos, expr


def changed_site(path, change, old, new, indexed):
    if not change.old_lines or change.insertion_anchors:
        return None
    first, last = min(change.old_lines), max(change.old_lines)
    for (_, _, original), (mapping, pos, revised) in zip(
        _enclosing_entries(old, first, last), _enclosing_entries(new, first, first)
    ):
        if not original.lineno <= first <= last <= original.end_lineno:
            continue
        key = f"{path}:{original.lineno}:{original.col_offset}"
        if key not in indexed or not deferred_change(original, revised):
            continue
        mapping.values[pos] = original
        try:
            if same_module(old, new, change.old_lines):
                return key
        finally:
            mapping.values[pos] = revised
    return None
