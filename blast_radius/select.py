"""Build a fresh test selection for each patch using the pinned checkout."""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import gzip
import json
import math
import os
from pathlib import Path
import re
import time
import tokenize

from .equivalence import same_module
from .patches import FileDiff, apply_to_text, parse_patch
from .probe import probe
from .recall import eager_imports_changed, shared_fixture_consumers
from .replay import equivalent, expression_change
from .scheduling import fill_idle_time, schedule


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
RUNTIME_ROOTS = ("sqlglot/", "tests/")
PROJECT_FILES = frozenset({"pytest.ini", "conftest.py", "pyproject.toml", "setup.cfg", "setup.py"})
SAFE_DOC_ROOTS = ("docs/", "posts/", "pdoc/", ".github/")
DIRECTIVE = re.compile(r"#\s*(?:coding\s*[:=]|type:|\!|pragma:|noqa\b)", re.IGNORECASE)
DIALECT_GROUPS = ("dialects", "generators", "parsers", "typing")


@dataclass(frozen=True)
class Test:
    number: int
    node: str
    file: str
    duration: int


@lru_cache(maxsize=32)
def read_source(path: Path) -> str:
    # Respect Python's encoding declaration, preserving exact line endings
    # so a valid unified diff applies the same way as it would with git.
    with path.open("rb") as stream:
        encoding, _ = tokenize.detect_encoding(stream.readline)
    return path.read_bytes().decode(encoding)


def block_start(node: ast.AST) -> int:
    return min((node.lineno, *(x.lineno for x in getattr(node, "decorator_list", ()))))


class SourceShape:
    def __init__(self, tree: ast.Module):
        self.statements = sorted(
            (node for node in ast.walk(tree) if isinstance(node, (ast.stmt, ast.ExceptHandler))),
            key=lambda node: (node.end_lineno - block_start(node), -node.col_offset),
        )
        self.functions = sorted(
            (node for node in ast.walk(tree) if isinstance(node, (*FUNCTIONS, ast.Lambda))),
            key=lambda node: (node.end_lineno - node.lineno, -node.col_offset),
        )
        self.classes = [node for node in self.statements if isinstance(node, ast.ClassDef)]

    def function_at(self, line: int):
        for node in self.functions:
            if self.body_start(node) <= line <= node.end_lineno:
                return node
        return None

    @staticmethod
    def body_start(node):
        # A multiline lambda body is deferred. Its first line can also hold
        # the enclosing assignment, and is treated as import-time if so.
        if isinstance(node, ast.Lambda):
            return max(node.lineno + 1, node.body.lineno)
        return node.body[0].lineno

    def statement_at(self, line: int):
        for node in self.statements:
            if block_start(node) <= line <= node.end_lineno:
                return node
        return None

    def comparable_lines(self, line: int):
        """Coverage can attach a multiline expression to a nearby source line."""
        node = self.statement_at(line)
        if node is None:
            return range(line, line + 1)
        start = block_start(node)
        body = getattr(node, "body", None)
        stop = body[0].lineno if isinstance(body, list) and body else node.end_lineno + 1
        return range(start, max(line + 1, stop))


class Selector:
    def __init__(self, source: Path, index: dict, tests: list[Test]):
        self.source = source
        self.files = index["files"]
        self.has_shared_scopes = any("shared" in file for file in self.files.values())
        self.readers = index["data_reads"]
        self.collection_reads = index["collection_reads"]
        self.dialects = index.get("dialects", {})
        self.expressions = index.get("expressions", {})
        self.operand_sites = {}
        self.operand_files = set()
        nodes = index["nodes"]
        self.by_number = {t.number: t for t in tests}
        self.all = set(self.by_number)
        self.by_file = defaultdict(set)
        number_for = {t.node: t.number for t in tests}
        self.index_numbers = [number_for.get(node, -1) for node in nodes]
        self.cohorts = [self.translate(items) for items in index["cohorts"]]
        self.uncovered = self.all - set(self.index_numbers)
        self.uncovered.update(number_for[node] for node in index["nonpasses"] if node in number_for)
        for test in tests:
            self.by_file[test.file].add(test.number)

    def translate(self, items):
        return {number for i in items if (number := self.index_numbers[i]) >= 0}

    def lines(self, file_info, lines):
        result = set()
        by_line = file_info["lines"]
        for line in lines:
            cohort = by_line.get(str(line))
            if cohort is not None:
                result.update(self.cohorts[cohort])
        return result

    @lru_cache(maxsize=64)
    def test_module_is_shared(self, path):
        module = re.escape(path.removesuffix(".py").replace("/", "."))
        importer = re.compile(rf"^\s*(?:from|import)\s+{module}\b", re.MULTILINE)
        return any(importer.search(read_source(self.source / other))
                   for other in self.by_file if other != path)

    def collection_effects(self, path, affected):
        groups = tuple(f"sqlglot/{group}/" for group in DIALECT_GROUPS)
        if path.startswith(groups) and path.count("/") == 2:
            dialect = Path(path).stem
            dialect_path = f"sqlglot/dialects/{dialect}.py"
            if dialect_path in self.dialects and dialect not in ("__init__", "dialect"):
                consumers = self.translate(self.dialects[dialect_path])
                for group in DIALECT_GROUPS:
                    sibling = self.files.get(f"sqlglot/{group}/{dialect}.py", {"lines": {}})
                    for cohort in set(sibling["lines"].values()):
                        consumers.update(self.cohorts[cohort])
                return affected | consumers | self.by_file.get(f"tests/dialects/test_{dialect}.py", set())
        if path in self.by_file:
            if self.test_module_is_shared(path):
                return self.all
            return affected | self.by_file[path]
        return self.all

    @lru_cache(maxsize=32)
    def has_external_class_dependency(self, name):
        cls = re.escape(name)
        references = re.compile(
            rf"\b{cls}\s*\.\s*(?:arg_types|required_args)\b"
            rf"|\bclass\s+\w+\s*\([^)]*\b{cls}\b",
        )
        return any(references.search(path.read_bytes().decode("utf-8", errors="replace"))
                   for root in RUNTIME_ROOTS for path in (self.source / root).rglob("*.py"))

    def arg_type_consumers(self, path, shape, change, old_tree, new_tree):
        """Narrow value-only edits to a concrete Expression's arg_types."""
        if (not path.startswith("sqlglot/expressions/") or not change.old_lines
                or change.insertion_anchors or not self.expressions):
            return None
        old_nodes = {shape.statement_at(line) for line in change.old_lines}
        if len(old_nodes) != 1:
            return None
        assignment = old_nodes.pop()
        if (not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1
                or not isinstance(assignment.targets[0], ast.Name)
                or assignment.targets[0].id != "arg_types"
                or not isinstance(assignment.value, ast.Dict)):
            return None
        for index, original in enumerate(old_tree.body):
            if not isinstance(original, ast.ClassDef) or assignment not in original.body:
                continue
            if index >= len(new_tree.body):
                return None
            revised = new_tree.body[index]
            position = original.body.index(assignment)
            if (not isinstance(revised, ast.ClassDef) or revised.name != original.name
                    or len(original.body) != len(revised.body)):
                return None
            other = revised.body[position]
            if (not isinstance(other, ast.Assign) or not isinstance(other.value, ast.Dict)
                    or None in assignment.value.keys or None in other.value.keys
                    or [ast.dump(k) for k in assignment.value.keys]
                    != [ast.dump(k) for k in other.value.keys]
                    or any(not isinstance(v, ast.Constant)
                           for v in (*assignment.value.values, *other.value.values))):
                return None
            if self.has_external_class_dependency(original.name):
                return None
            patched_value = other.value
            other.value = assignment.value
            try:
                if not same_module(old_tree, new_tree, change.old_lines):
                    return None
            finally:
                other.value = patched_value
            key = path.removesuffix(".py").replace("/", ".") + "." + original.name
            return self.translate(self.expressions.get(key, ()))
        return None

    def python_effects(self, path: str, change: FileDiff, single_file=True):
        info = self.files.get(path, {"lines": {}, "collection": []})
        owned = self.by_file.get(path, set())
        old_text = read_source(self.source / path)
        new_text = apply_to_text(old_text, change)
        try:
            new_tree = ast.parse(new_text, filename=path, type_comments=True)
            # ast.parse accepts some invalid statements, e.g. a return at
            # module scope. A compile failure may break every dependent import.
            compile(new_tree, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return owned if owned and not self.test_module_is_shared(path) else self.all
        old_tree = ast.parse(old_text, filename=path, type_comments=True)
        readers = self.translate(self.readers.get(path, ()))
        if same_module(old_tree, new_tree, change.old_lines):
            if not DIRECTIVE.search("\n".join(
                line for hunk in change.hunks for kind, line in hunk.lines if kind != " "
            )):
                return readers
        shape = SourceShape(old_tree)
        changed_lines = set()
        import_time = False
        for old_line in change.old_lines:
            owner = shape.function_at(old_line)
            if isinstance(owner, ast.Lambda):
                changed_lines.update(range(owner.body.lineno, owner.body.end_lineno + 1))
            else:
                changed_lines.update(shape.comparable_lines(old_line))
            if owner is None:
                import_time = True
        for anchor in change.insertion_anchors:
            before = shape.function_at(anchor)
            after = shape.function_at(anchor + 1)
            if before is after and before is not None:
                # An inserted statement has no old trace location. Use the
                # containing function, including entry via less common branches.
                changed_lines.update(range(shape.body_start(before), before.end_lineno + 1))
            elif after is not None:
                changed_lines.update(range(shape.body_start(after), after.end_lineno + 1))
                import_time = True
            else:
                import_time = True
        # A function body can execute while pytest imports its test modules
        # (metaclasses and default factories are common examples).
        if not import_time and changed_lines.intersection(info["collection"]):
            import_time = True
        runtime = self.lines(info, changed_lines)
        if single_file and not import_time and path in self.operand_files:
            mutation = expression_change(shape, change, old_tree, new_tree)
            if mutation:
                original, operation, edits = mutation
                key = f"{path}:{original.lineno}:{original.col_offset}"
                for samples, group in self.operand_sites.get(key, ()):
                    if equivalent(samples, type(original.ops[0]), operation, edits):
                        runtime.difference_update(self.translate(group))
        for line in changed_lines:
            cohort = info.get("shared", {}).get(str(line))
            if cohort is not None:
                runtime.update(self.cohorts[cohort])
        affected = runtime | owned | readers
        if import_time:
            if path.startswith("sqlglot/") and eager_imports_changed(old_tree, new_tree):
                return self.all
            arg_types = self.arg_type_consumers(path, shape, change, old_tree, new_tree)
            affected = self.collection_effects(path, affected) if arg_types is None else affected | arg_types
        return affected | self.uncovered

    def data_effects(self, path: str):
        affected = self.translate(self.readers.get(path, ()))
        if not self.has_shared_scopes:
            affected.update(shared_fixture_consumers(self, affected))
        for reader in self.collection_reads.get(path, ()):
            if reader not in self.by_file:
                return self.all
            affected.update(self.by_file[reader])
        return affected or self.all

    def one_file(self, change: FileDiff, single_file=True):
        paths = {p for p in (change.old_path, change.new_path) if p}
        if paths and all(p in PROJECT_FILES or p.startswith(RUNTIME_ROOTS) for p in paths):
            if change.binary or change.old_path != change.new_path:
                if len(paths) == 1 and not any(p.endswith(".py") for p in paths):
                    return self.data_effects(next(iter(paths)))
                return self.all
            path = change.old_path
            if path in PROJECT_FILES or path.endswith("/conftest.py"):
                return self.all
            if not change.hunks:
                return self.all
            if path.endswith(".py"):
                return self.python_effects(path, change, single_file)
            return self.data_effects(path)

        if paths and all(p.startswith(SAFE_DOC_ROOTS) or (
            "/" not in p and p.endswith((".md", ".rst"))
        ) for p in paths):
            return set()
        return self.all

    def select(self, text: str):
        selected = set()
        try:
            changes = parse_patch(text)
            if not changes and text.strip():
                return set(self.all)
            for change in changes:
                selected.update(self.one_file(change, len(changes) == 1))
                if len(selected) == len(self.all):
                    break
        except (TypeError, ValueError, OSError, SyntaxError, UnicodeError, RecursionError):
            return set(self.all)
        return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/sqlglot"))
    parser.add_argument("--index", type=Path, default=Path("/app/coverage.json.gz"))
    args = parser.parse_args()
    catalogue = json.loads((args.data / "tests.json").read_text())
    tests = [Test(t["number"], t["id"], t["file"], math.floor(t["seconds"] * 1000 + 0.5))
             for t in catalogue["tests"]]
    try:
        with gzip.open(args.index, "rt", encoding="utf-8") as stream:
            index = json.load(stream)
            nodes = index["nodes"]
            selector = Selector(args.source, index, tests)
    except (OSError, ValueError, KeyError):
        selector = None
    if selector is not None:
        try:
            with gzip.open(args.index.with_name("operands.json.gz"), "rt", encoding="utf-8") as stream:
                operand_index = json.load(stream)
            if operand_index["nodes"] == nodes:
                selector.operand_sites = operand_index["sites"]
                selector.operand_files = {site.rsplit(":", 2)[0] for site in selector.operand_sites}
        except (OSError, ValueError, KeyError):
            pass
    by_number = {t.number: t for t in tests}
    durations = {t.number: t.duration for t in tests}
    extras = sorted(durations.items(), key=lambda pair: (pair[1], pair[0]))
    can_probe = selector is not None and os.getenv("RWX_PROBE", "0") == "1"
    deadline = time.monotonic() + 35
    for patch in sorted((args.data / "commits").glob("*.patch")):
        numbers = set(by_number)
        if selector is not None:
            try:
                text = patch.read_bytes().decode("utf-8")
                numbers = selector.select(text)
            except (OSError, UnicodeError):
                numbers = selector.all
        selected = [(n, by_number[n].duration) for n in sorted(numbers)]
        workers = schedule(selected)
        if can_probe and len(selected) <= 100:
            weighted = sum(duration for _, duration in selected)
            timeout = min(2.5, deadline - time.monotonic())
            if timeout > 1.5 and 25 <= weighted <= 3600:
                result = probe(args.source, patch, (by_number[n].node for n, _ in selected), timeout=timeout)
                selected = [(n, duration) for n, duration in selected
                            if n in selector.uncovered or by_number[n].node not in result.passed]
                workers = schedule(selected)
        fill_idle_time(workers, durations, extras)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
