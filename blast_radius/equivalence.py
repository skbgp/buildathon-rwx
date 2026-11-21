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


def same_patched_module(before, after, change):
    """Match original type-ignore comments at their new locations after line shifts."""
    if not before.type_ignores and not after.type_ignores:
        return same_module(before, after, change.old_lines)
    if len(before.type_ignores) != len(after.type_ignores):
        return False
    # An unchanged directive follows the surrounding lines through the diff.
    # A deleted directive cannot be matched simply because its tag reappears.
    shifts = []
    for hunk in change.hunks:
        line = hunk.old_start - bool(hunk.old_count)
        for kind, _ in hunk.lines:
            if kind == "+":
                shifts.append((line + 1, 1))
            else:
                line += 1
                if kind == "-":
                    shifts.append((line + 1, -1))
    for old, new in zip(before.type_ignores, after.type_ignores):
        shift = sum(delta for line, delta in shifts if line <= old.lineno)
        if old.lineno in change.old_lines or old.lineno + shift != new.lineno or old.tag != new.tag:
            return False
    saved = after.type_ignores
    after.type_ignores = before.type_ignores
    try:
        return same_module(before, after, change.old_lines)
    finally:
        after.type_ignores = saved


def nodes_at_line(root, line):
    """Walk only subtrees that span a source line."""
    stack = [root]
    while stack:
        node = stack.pop()
        if hasattr(node, "lineno") and not node.lineno <= line <= getattr(node, "end_lineno", node.lineno):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))
