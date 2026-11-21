"""Compare parsed Python trees without serializing both to large strings."""

from __future__ import annotations

import ast
from collections.abc import Set


def same_ast(before, after) -> bool:
    """Compare parser-produced AST fields, omitting location attributes."""
    if before is after:
        return True
    kind = type(before)
    if kind is not type(after):
        return False
    if kind is list:
        if len(before) != len(after):
            return False
        for left, right in zip(before, after):
            if not same_ast(left, right):
                return False
        return True
    if isinstance(before, ast.AST):
        for name in before._fields:
            if not same_ast(getattr(before, name), getattr(after, name)):
                return False
        return True
    # AST constants retain the type and representation of numeric literals.
    # Equality alone would conflate infinities' complex NaNs or signed zeros.
    if kind is float or kind is complex:
        return repr(before) == repr(after)
    return before == after


def same_module(
    before: ast.Module, after: ast.Module, changed_lines: Set[int] = frozenset()
) -> bool:
    """Check changed statements first, then compare every remaining field."""
    if len(before.body) != len(after.body):
        return False
    if not changed_lines:
        return same_ast(before, after)
    first, last = min(changed_lines), max(changed_lines)
    unchanged = []
    for left, right in zip(before.body, after.body):
        if left.lineno <= last and first <= left.end_lineno:
            if not same_ast(left, right):
                return False
        else:
            unchanged.append((left, right))
    return same_ast(before.type_ignores, after.type_ignores) and all(
        same_ast(left, right) for left, right in unchanged
    )
