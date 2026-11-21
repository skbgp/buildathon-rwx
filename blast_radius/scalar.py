"""Locate isolated changes to scalar configuration on a dialect class."""

import ast

from .equivalence import same_module


def _assignment(stmt):
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        name = stmt.targets[0]
    elif isinstance(stmt, ast.AnnAssign):
        name = stmt.target
    else:
        return None
    if not isinstance(name, ast.Name) or not name.id.isupper() or stmt.value is None:
        return None
    try:
        val = ast.literal_eval(stmt.value)
    except (ValueError, TypeError):
        return None
    if type(val) in (type(None), str, int, bool):
        return name.id, val
    return None


def consumers(selector, path, change, old_tree, new_tree):
    """Return test numbers, or None if the ordinary coverage is required."""
    if not path.startswith("sqlglot/dialects/") or not change.old_lines or change.insertion_anchors:
        return None
    if not selector.scalar_reads or len(old_tree.body) != len(new_tree.body):
        return None
    for cls, new_cls in zip(old_tree.body, new_tree.body):
        if not isinstance(cls, ast.ClassDef) or not isinstance(new_cls, ast.ClassDef):
            continue
        if len(cls.body) != len(new_cls.body):
            continue
        owner = path[:-3].replace("/", ".") + "." + cls.name
        reads = selector.scalar_reads.get(owner)
        if reads is None:
            continue
        for stmt, new_stmt in zip(cls.body, new_cls.body):
            if not all(stmt.lineno <= line <= stmt.end_lineno for line in change.old_lines):
                continue
            original, revised = _assignment(stmt), _assignment(new_stmt)
            if not original or not revised or original[0] != revised[0] or type(original[1]) is not type(revised[1]):
                continue
            name = original[0]
            # Bare references during class definition bypass attribute access.
            if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name
                   for n in ast.walk(cls)):
                return None
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in
                   ("locals", "eval", "exec") for n in ast.walk(cls)):
                return None
            actual = new_stmt.value
            new_stmt.value = stmt.value
            try:
                same = same_module(old_tree, new_tree, change.old_lines)
            finally:
                new_stmt.value = actual
            if not same:
                return None
            records = set(reads.get(name, ())) | set(reads.get("*", ()))
            if -1 in records:
                return None
            return selector.translate(records)
    return None
