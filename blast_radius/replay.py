"""Check a patched comparison against the operand values seen on the clean tests."""

import ast
import operator

from .equivalence import nodes_at_line, same_ast, same_module

ORDERING = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
OPS = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq, *ORDERING)


def literal(expr):
    if isinstance(expr, ast.Constant) and type(expr.value) in (int, str, bool, type(None)):
        return True, expr.value
    if (isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.UAdd, ast.USub))
            and isinstance(expr.operand, ast.Constant) and type(expr.operand.value) is int):
        return True, ast.literal_eval(expr)
    if (isinstance(expr, ast.Tuple) and len(expr.elts) <= 8
            and all(isinstance(item, ast.Constant) and type(item.value) is int for item in expr.elts)):
        return True, tuple(item.value for item in expr.elts)
    return False, None


def candidate(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], OPS):
        return False

    def attribute(expr):
        if isinstance(expr, ast.Attribute):
            return isinstance(expr.value, ast.Name) or attribute(expr.value)
        return False

    def simple(expr):
        return (isinstance(expr, ast.Name) or attribute(expr) or
                isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                and expr.func.id == "len" and len(expr.args) == 1 and not expr.keywords)

    left, right = node.left, node.comparators[0]
    return (attribute(left) and attribute(right)
            or literal(left)[0] and simple(right) or simple(left) and literal(right)[0])


def expression_change(shape, change, old_tree, new_tree):
    if len(change.old_lines) != 1 or change.insertion_anchors:
        return None
    line = next(iter(change.old_lines))
    if shape.function_at(line) is None:
        return None
    stmt = shape.statement_at(line)
    if stmt is None:
        return None
    sites = {(expr.lineno, expr.col_offset): expr for expr in ast.walk(stmt) if candidate(expr)}
    if not sites:
        return None
    for other in nodes_at_line(new_tree, line):
        if not candidate(other):
            continue
        old = sites.get((other.lineno, other.col_offset))
        if old is None or not old.lineno <= line <= old.end_lineno:
            continue
        # Permit an operator edit, or replacement of a single constant
        # operand. Every other AST field must agree across the two modules.
        old_op, new_op = old.ops[0], other.ops[0]
        if any(isinstance(op, (ast.Is, ast.IsNot)) for op in (old_op, new_op)):
            # Introducing identity comparisons with a non-singleton literal
            # can raise a SyntaxWarning while a test imports the module.
            operands = (old.left, old.comparators[0], other.left, other.comparators[0])
            if any(valid and value is not None and type(value) is not bool
                   for valid, value in map(literal, operands)):
                continue
        edits = []
        for name, x, y in (("left", old.left, other.left),
                           ("right", old.comparators[0], other.comparators[0])):
            if not same_ast(x, y):
                valid, replacement = literal(y)
                if not literal(x)[0] or not valid:
                    break
                edits.append((name, replacement))
        else:
            if len(edits) + (type(old_op) is not type(new_op)) != 1:
                continue
            saved = other.ops, other.left, other.comparators
            other.ops, other.left, other.comparators = old.ops, old.left, old.comparators
            try:
                match = same_module(old_tree, new_tree, change.old_lines)
            finally:
                other.ops, other.left, other.comparators = saved
            if match:
                return old, type(new_op), edits
    return None


def _result(operation, left, right, identical):
    if operation is ast.Is:
        return identical
    if operation is ast.IsNot:
        return not identical
    left_type, right_type = left[0], right[0]
    if operation in ORDERING:
        # A string Enum can implement its own ordering independently of
        # equality. Replay ordering only on exact builtin value types.
        if left_type == right_type:
            if left_type in ("int", "str"):
                return ORDERING[operation](left[1], right[1])
            if left_type == "int_tuple":
                return ORDERING[operation](left[1:], right[1:])
        return None
    if left_type == "none" or right_type == "none":
        matches = left_type == right_type
    elif left_type in ("str", "str_enum") and right_type in ("str", "str_enum"):
        matches = left[1] == right[1]
    elif left_type == right_type == "object":
        matches = identical
    elif left_type == "opaque_builtin" or right_type == "opaque_builtin":
        return None
    elif left_type == "object" or right_type == "object":
        return None
    else:
        matches = left == right
    return matches if operation is ast.Eq else not matches


def equivalent(samples, old_op, new_op, edits):
    if not samples:
        return False
    replacement = None
    if edits:
        if old_op in (ast.Is, ast.IsNot) or new_op in (ast.Is, ast.IsNot):
            return False
        name, value = edits[0]
        if value is None:
            replacement = ["none"]
        elif isinstance(value, int):
            replacement = ["int", int(value)]
        elif isinstance(value, tuple):
            replacement = ["int_tuple", *value]
        else:
            replacement = ["str", value]
    for left, right, identical in samples:
        before = _result(old_op, left, right, identical)
        if before is None:
            return False
        if replacement is not None:
            if name == "left":
                left = replacement
            else:
                right = replacement
        if before is not _result(new_op, left, right, identical):
            return False
    return True
