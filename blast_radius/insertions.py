"""Use the next executed statement as a coverage witness for a pure insertion."""

import ast
from functools import lru_cache
from types import CodeType

from .equivalence import same_module

_SIMPLE = (ast.Return, ast.Raise, ast.Assert, ast.Expr, ast.Break, ast.Continue, ast.Assign, ast.AugAssign)
_NEXT = (ast.AnnAssign, ast.Assign, ast.AugAssign, ast.Return, ast.Raise, ast.Expr)
_SCOPE_CHANGES = (ast.Del, ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda,
                  ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _ordinary_statement(stmt):
    if isinstance(stmt, ast.If):
        return all(_ordinary_statement(child) for child in (*stmt.body, *stmt.orelse))
    return isinstance(stmt, _SIMPLE) and not (
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
    )


@lru_cache(maxsize=12)
def _compiled(tree):
    return compile(tree, "<coverage witness>", "exec", dont_inherit=True)


def _scopes(code):
    return (code.co_name, code.co_flags, code.co_varnames, code.co_freevars, code.co_cellvars,
            tuple(_scopes(child) for child in code.co_consts if isinstance(child, CodeType)))


def _traceable(code, lines):
    if any(start < end and line in lines for start, end, line in code.co_lines()):
        return True
    return any(_traceable(child, lines) for child in code.co_consts if isinstance(child, CodeType))


def following_lines(change, old_tree, new_tree, shape, info):
    if len(change.insertion_anchors) != 1 or change.old_lines or len(change.hunks) != 1:
        return None
    anchor, = change.insertion_anchors
    hunk, = change.hunks
    count = hunk.new_count - hunk.old_count
    if count <= 0:
        return None
    first, last = anchor + 1, anchor + count
    # TypeIgnore.lineno is an AST field, unlike ordinary source locations.
    # Require precisely the original directives shifted by the added lines.
    shifted_ignores = [(item.lineno + (count if item.lineno > anchor else 0), item.tag)
                       for item in old_tree.type_ignores]
    if shifted_ignores != [(item.lineno, item.tag) for item in new_tree.type_ignores]:
        return None
    for parent in ast.walk(new_tree):
        for field in ('body', 'orelse', 'finalbody'):
            block = getattr(parent, field, None)
            if not isinstance(block, list):
                continue
            added = [i for i, stmt in enumerate(block)
                     if isinstance(stmt, ast.stmt) and first <= stmt.lineno <= stmt.end_lineno <= last]
            if not added or added[-1] + 1 == len(block):
                continue
            next_stmt = block[added[-1] + 1]
            if not isinstance(next_stmt, _NEXT) or next_stmt.lineno <= last:
                continue
            if isinstance(next_stmt, ast.AnnAssign) and next_stmt.value is None:
                continue
            if isinstance(next_stmt, ast.Expr) and not isinstance(next_stmt.value, ast.Call):
                continue
            old_line = next_stmt.lineno - count
            old_next = shape.statement_at(old_line)
            owner = shape.function_at(old_line)
            if (not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or type(old_next) is not type(next_stmt) or old_next.lineno != old_line):
                continue
            lines = range(old_line, old_next.end_lineno + 1)
            if not any(str(line) in info["lines"] for line in lines):
                if not info["lines"] or not _traceable(_compiled(old_tree), lines):
                    continue
            start, stop = added[0], added[-1] + 1
            inserted = block[start:stop]
            if not all(_ordinary_statement(stmt) for stmt in inserted):
                continue
            if any(isinstance(node, _SCOPE_CHANGES) or
                   isinstance(node, ast.Name) and node.id in ("super", "__class__")
                   for stmt in inserted for node in ast.walk(stmt)):
                continue
            # A new free variable changes the enclosing scope even if this branch
            # never runs. Inspect the lexical ancestors for potentially captured names.
            binds = any(isinstance(node, ast.Store) for stmt in inserted for node in ast.walk(stmt))
            nested = any(outer.lineno < owner.lineno <= outer.end_lineno for outer in shape.functions)
            if binds or nested:
                if _scopes(_compiled(old_tree)) != _scopes(_compiled(new_tree)):
                    return None
            original_ignores = new_tree.type_ignores
            new_tree.type_ignores = old_tree.type_ignores
            del block[start:stop]
            try:
                matches = same_module(old_tree, new_tree)
            finally:
                block[start:start] = inserted
                new_tree.type_ignores = original_ignores
            if matches:
                return lines
    return None
