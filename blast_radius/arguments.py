"""Trace which required-argument checks could change under a value-only schema edit."""

import ast
from collections import defaultdict


# Each entry is a map from an argument name to the contexts that could observe
# its absence. "*" denotes a check with an already invalid or unfamiliar input.
records = defaultdict(lambda: defaultdict(set))
owners = {}


def observe(expression, context):
    cls = type(expression)
    if cls not in owners:
        owners[cls] = cls.__module__ + "." + cls.__qualname__
    by_key = records[owners[cls]]
    if type(expression.args) is not dict or type(expression.arg_types) is not dict:
        by_key["*"].add(context)
        return
    for key, required in expression.arg_types.items():
        value = expression.args.get(key)
        if isinstance(value, list) and type(value) is not list:
            by_key["*"].add(context)
        elif value is None or (type(value) is list and not value):
            by_key[key].add(context)
            if required:
                # Rebuilding the set can also change the order of existing errors.
                by_key["*"].add(context)


def changed_contexts(reads, original, patched):
    if reads is None or any(not isinstance(k, ast.Constant) or type(k.value) is not str
                            or k.value == "*" for k in original.keys):
        return None
    if any(not isinstance(v, ast.Constant) or type(v.value) is not bool
           for v in (*original.values, *patched.values)):
        return None
    contexts = set(reads.get("*", ()))
    for k, before, after in zip(original.keys, original.values, patched.values):
        if before.value != after.value:
            contexts.update(reads.get(k.value, ()))
    return None if -1 in contexts else contexts


def instrument(tree):
    cls, = (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Expression")
    method, = (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "error_messages")
    loop, = (n for n in method.body if isinstance(n, ast.For) and isinstance(n.iter, ast.Attribute)
             and n.iter.attr == "required_args")
    call = ast.Expr(ast.Call(ast.Name("_rwx_observe_arguments", ast.Load()), [ast.Name("self", ast.Load())], []))
    method.body.insert(method.body.index(loop), ast.copy_location(call, loop))
