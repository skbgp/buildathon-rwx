"""Recognize a side-effect-free second condition that masks a boolean flag read."""

import ast
import builtins
import types


def annotate(tree):
    for stmt in ast.walk(tree):
        if not isinstance(stmt, (ast.If, ast.IfExp, ast.While)):
            continue
        cond = stmt.test
        if not isinstance(cond, ast.BoolOp):
            continue
        first, second = cond.values[:2]
        flag = first.operand if isinstance(first, ast.UnaryOp) and isinstance(first.op, ast.Not) else first
        if not (isinstance(flag, ast.Attribute) and isinstance(flag.value, ast.Name)
                and flag.value.id == "self" and flag.attr.isupper()):
            continue
        negated = isinstance(second, ast.UnaryOp) and isinstance(second.op, ast.Not)
        check = second.operand if negated else second
        # isinstance(local, module.Class) and a bare local have no SQLGlot
        # calls in their syntax. The observer verifies the actual runtime types.
        if isinstance(check, ast.Name):
            kind, var, module, cls = "truth", check.id, "", ""
        elif (isinstance(check, ast.Call) and isinstance(check.func, ast.Name)
              and check.func.id == "isinstance" and len(check.args) == 2 and not check.keywords
              and isinstance(check.args[0], ast.Name) and isinstance(check.args[1], ast.Attribute)
              and isinstance(check.args[1].value, ast.Name)):
            kind, var = "instance", check.args[0].id
            module, cls = check.args[1].value.id, check.args[1].attr
        else:
            continue
        masks_when = isinstance(cond.op, ast.Or) ^ negated
        args = [flag.value, *(ast.Constant(x) for x in (flag.attr, kind, var, module, cls, masks_when))]
        wrapped = ast.copy_location(ast.Call(ast.Name("_rwx_flag_value", ast.Load()), args, []), flag)
        if first is flag:
            cond.values[0] = wrapped
        else:
            first.operand = wrapped


_ABSENT = object()


def masked(frame, kind, var, module, cls, masks_when):
    local = frame.f_locals
    value = local.get(var, _ABSENT)
    if value is _ABSENT:
        return False
    if kind == "truth":
        if type(value) not in (bool, int, str, bytes, list, dict, tuple, frozenset, set, type(None)):
            return False
        truth = bool(value)
    else:
        called = local.get("isinstance", frame.f_globals.get("isinstance", frame.f_builtins.get("isinstance")))
        namespace = local.get(module, frame.f_globals.get(module))
        if called is not builtins.isinstance or type(namespace) is not types.ModuleType:
            return False
        target = vars(namespace).get(cls)
        if type(target) is not type or type(type(value)) is not type:
            return False
        mro = type(value).__mro__
        # A custom __class__ or __getattribute__ can make even a negative
        # isinstance check observable. Only simulate default object lookup.
        get = next(base.__dict__["__getattribute__"] for base in mro if "__getattribute__" in base.__dict__)
        descr = next(base.__dict__["__class__"] for base in mro if "__class__" in base.__dict__)
        if get is not object.__getattribute__ or descr is not object.__dict__["__class__"]:
            return False
        truth = any(base is target for base in mro)
    return truth is masks_when
