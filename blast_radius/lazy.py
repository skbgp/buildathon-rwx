"""Coverage at the entry to conditional expressions in the pristine checkout."""

import ast
from collections import defaultdict

from .equivalence import nodes_at_line, same_module

reads = defaultdict(set)
import_depth = 0
active_context = lambda: ""
# The wrapper preserves value evaluation. Exclude expressions whose enclosing
# truth context can change how Python compiles them (e.g. a nested BoolOp).
_VALUES = (ast.Name, ast.Attribute, ast.Call, ast.BinOp, ast.Subscript, ast.Compare)


def locations(tree, line=None):
    for node in ast.walk(tree) if line is None else nodes_at_line(tree, line):
        if isinstance(node, ast.BoolOp):
            yield from ((node.values, i) for i in range(1, len(node.values)))
        elif isinstance(node, ast.IfExp):
            yield node, "body"
            yield node, "orelse"


def value(owner, field):
    return owner[field] if isinstance(owner, list) else getattr(owner, field)


def put(owner, field, item):
    if isinstance(owner, list):
        owner[field] = item
    else:
        setattr(owner, field, item)


def eligible(expr):
    return isinstance(expr, _VALUES) and (not isinstance(expr, ast.Compare) or len(expr.ops) == 1)


def key(path, expr):
    return f"{path}:{expr.lineno}:{expr.col_offset}"


def observe(site):
    reads[site].add("" if import_depth else active_context())


def instrument(tree, path, helper):
    changed = False
    for owner, field in list(locations(tree)):
        expr = value(owner, field)
        if not eligible(expr):
            continue
        site = key(path, expr)
        reads[site]  # Include covered modules' unvisited expressions.
        call = ast.Call(ast.Name(helper, ast.Load()), [ast.Constant(site)], [])
        pair = ast.Tuple([call, expr], ast.Load())
        wrapped = ast.Subscript(pair, ast.Constant(1), ast.Load())
        put(owner, field, ast.copy_location(wrapped, expr))
        changed = True
    return changed


def bindings(root):
    return sorted(ast.dump(n) for n in ast.walk(root)
                  if isinstance(n, (ast.Name, ast.Yield, ast.YieldFrom, ast.Await,
                                    ast.Lambda, ast.comprehension)))


def changed_consumers(path, change, old_tree, new_tree, sites, shape):
    if not change.old_lines or change.insertion_anchors:
        return None
    first, last = min(change.old_lines), max(change.old_lines)
    old = {}
    statement = shape.statement_at(first)
    if statement is None:
        return None
    for parent, field in locations(statement, first):
        expr = value(parent, field)
        if eligible(expr) and expr.lineno <= first <= last <= expr.end_lineno:
            old[(expr.lineno, expr.col_offset)] = expr
    if not old:
        return None
    for parent, field in locations(new_tree, first):
        expr = value(parent, field)
        original = old.get((expr.lineno, expr.col_offset))
        if original is None:
            continue
        records = sites.get(key(path, original))
        if records is None or -1 in records.get("key:visit", ()):
            continue
        # An edit that changes generator status, name binding or closure layout
        # can affect the function even when this expression never executes.
        if bindings(original) != bindings(expr):
            continue
        put(parent, field, original)
        try:
            matches = same_module(old_tree, new_tree, change.old_lines)
        finally:
            put(parent, field, expr)
        if matches:
            return records.get("key:visit", ())
    return None
