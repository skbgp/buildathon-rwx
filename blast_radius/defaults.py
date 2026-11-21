"""Select baseline consumers of a changed literal parameter default."""

import ast

from .equivalence import nodes_at_line, same_module


def parameters(function):
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    start = len(positional) - len(args.defaults)
    for index, param in enumerate(positional[start:]):
        yield param.arg, args.defaults, index
    for index, param in enumerate(args.kwonlyargs):
        yield param.arg, args.kw_defaults, index


def contexts(path, change, before, after, sites):
    if len(change.old_lines) != 1 or change.insertion_anchors:
        return None
    line, = change.old_lines

    def signatures(tree):
        return [node for node in nodes_at_line(tree, line)
                if isinstance(node, ast.FunctionDef) and not node.decorator_list
                and line <= node.body[0].lineno]

    old, new = signatures(before), signatures(after)
    if len(old) != 1 or len(new) != 1 or old[0].name != new[0].name:
        return None
    function = old[0]
    site = sites.get(f"{path}:{function.lineno}:{function.name}")
    if site is None:
        return None
    metadata = site.get("*", ())
    if -1 in metadata:
        return None
    for (name, left, i), (other, right, j) in zip(parameters(function), parameters(new[0])):
        a, b = left[i], right[j]
        if (name != other or name not in site or not isinstance(a, ast.Constant)
                or not isinstance(b, ast.Constant) or a.lineno != line or b.lineno != line
                or type(a.value) not in (bool, int, str) or type(a.value) is not type(b.value)
                or a.value == b.value):
            continue
        if -1 in site[name] or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(function)):
            return None
        right[j] = a
        try:
            if same_module(before, after, change.old_lines):
                return set(metadata).union(site[name])
        finally:
            right[j] = b
    return None
