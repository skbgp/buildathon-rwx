"""Identify isolated edits to values of literal configuration dictionaries."""

import ast

from .equivalence import same_module


def dictionary(node):
    if isinstance(node, ast.AnnAssign):
        target, value = node.target, node.value
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    else:
        return None
    if not isinstance(target, ast.Name) or not target.id.isupper() or not isinstance(value, ast.Dict):
        return None
    try:
        constant = ast.literal_eval(value)
    except (ValueError, TypeError):
        return None
    if not constant or len(constant) != len(value.keys) or any(type(k) is not str for k in constant):
        return None
    if any(not _simple_literal(v) for v in constant.values()):
        return None
    return target.id, value, constant


def _simple_literal(value):
    if type(value) is tuple:
        return all(_simple_literal(v) for v in value)
    return type(value) in (type(None), str, bool, int)


def definitions(tree, prefix=()):
    for i, node in enumerate(tree.body):
        position = (*prefix, i)
        if isinstance(node, ast.ClassDef):
            yield from definitions(node, position)
        elif prefix and (item := dictionary(node)):
            yield position, node, item


def changed_keys(path, file_diff, old_tree, new_tree):
    if not file_diff.old_lines or file_diff.insertion_anchors:
        return None
    for position, old, (name, old_ast, before) in definitions(old_tree):
        if not all(old_ast.lineno <= line <= old_ast.end_lineno for line in file_diff.old_lines):
            continue
        owner = old_tree
        for index in position[:-1]:
            owner = owner.body[index]
        # A class can use its mapping to initialize another value in the
        # same class body without invoking attribute access.
        if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
               and node.id == name for node in ast.walk(owner)):
            continue
        revised = new_tree
        try:
            for index in position:
                revised = revised.body[index]
        except (AttributeError, IndexError):
            continue
        item = dictionary(revised)
        if item is None:
            continue
        _, new_ast, after = item
        if tuple(before) != tuple(after):
            continue
        # Exact AST checks include spelling-independent type differences such
        # as replacing 1 by True, including when nested inside a tuple.
        touched = {key for key, a, b in zip(before, old_ast.values, new_ast.values)
                   if ast.dump(a) != ast.dump(b)}
        if not touched:
            continue
        revised.value = old_ast
        try:
            same = same_module(old_tree, new_tree, file_diff.old_lines)
        finally:
            revised.value = new_ast
        if same:
            return f"{path}:{old_ast.lineno}:{old_ast.col_offset}", touched
    return None
