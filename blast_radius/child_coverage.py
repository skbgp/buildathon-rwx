"""Attribute a process pool job's coverage to the pytest item that submitted it."""

from __future__ import annotations

from collections import defaultdict
import json
from multiprocessing.reduction import ForkingPickler
from multiprocessing.util import Finalize
import os
from pathlib import Path

_child = None


class ModelUse:
    def __init__(self):
        self.node = ""
        self.dialects = defaultdict(set)
        self.expressions = defaultdict(set)

    def install(self):
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.expressions.core import Expr

        def record_dialect(dialect):
            if self.node:
                for cls in type(dialect).__mro__:
                    module = cls.__module__
                    if module.startswith("sqlglot."):
                        self.dialects[module.replace(".", "/") + ".py"].add(self.node)

        get_or_raise = Dialect.get_or_raise.__func__
        init_dialect = Dialect.__init__
        init_expression = Expr.__init__

        def tracked_get_or_raise(cls, *args, **kwargs):
            dialect = get_or_raise(cls, *args, **kwargs)
            record_dialect(dialect)
            return dialect

        def tracked_dialect(self_, *args, **kwargs):
            init_dialect(self_, *args, **kwargs)
            record_dialect(self_)

        def tracked_expression(self_, **kwargs):
            init_expression(self_, **kwargs)
            if self.node:
                cls = type(self_)
                self.expressions[f"{cls.__module__}.{cls.__qualname__}"].add(self.node)

        Dialect.get_or_raise = classmethod(tracked_get_or_raise)
        Dialect.__init__ = tracked_dialect
        Expr.__init__ = tracked_expression


def covered_job(root, directory, node, payload):
    global _child
    if _child is None:
        import coverage

        file = Path(directory) / f"child-{os.getpid()}"
        tracer = coverage.Coverage(
            data_file=str(file),
            include=[f"{root}/sqlglot/*", f"{root}/tests/*"],
            config_file=False,
        )
        model = ModelUse()
        model.install()
        tracer.start()
        _child = tracer, model

        def save():
            tracer.stop()
            tracer.save()
            file.with_suffix(".usage.json").write_text(json.dumps({
                "dialects": {key: sorted(nodes) for key, nodes in model.dialects.items()},
                "expressions": {key: sorted(nodes) for key, nodes in model.expressions.items()},
            }), encoding="utf-8")

        Finalize(None, save, exitpriority=0)
    tracer, model = _child
    model.node = node
    tracer.switch_context(node)
    try:
        function, args, kwargs = ForkingPickler.loads(payload)
        return bytes(ForkingPickler.dumps(function(*args, **kwargs)))
    finally:
        model.node = ""
        tracer.switch_context("")
