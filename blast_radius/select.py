"""Build a fresh test selection for each patch using the pinned checkout."""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache
import gzip
import json
import math
import os
from pathlib import Path
import re
import time
import tokenize

from .configuration import changed_keys
from .dispatch import changed_site
from .insertions import following_lines
from .equivalence import same_module, same_patched_module
from .patches import FileDiff, apply_to_text, parse_patch
from .probe import probe
from .recall import eager_imports_changed, shared_fixture_consumers
from . import arguments, defaults, lazy, scalar
from .replay import equivalent, expression_change
from .scheduling import fill_idle_time, schedule


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
RUNTIME_ROOTS = ("sqlglot/", "tests/")
PROJECT_FILES = frozenset({"pytest.ini", "conftest.py", "pyproject.toml", "setup.cfg", "setup.py"})
SAFE_DOC_ROOTS = ("docs/", "posts/", "pdoc/", ".github/")
DIRECTIVE = re.compile(r"#(?:[^\r\n]*?\bcoding\s*[:=]|\s*(?:type:|!|pragma:|noqa\b))", re.IGNORECASE)
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


@lru_cache(maxsize=24)
def baseline_tree(path):
    return ast.parse(read_source(path), filename=str(path), type_comments=True)


class SourceShape:
    def __init__(self, tree: ast.Module):
        statements = []
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.stmt, ast.ExceptHandler)):
                statements.append(node)
            if isinstance(node, (*FUNCTIONS, ast.Lambda)):
                functions.append(node)
        self.statements = sorted(
            statements,
            key=lambda node: (node.end_lineno - block_start(node), -node.col_offset),
        )
        self.functions = sorted(
            functions,
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


@lru_cache(maxsize=16)
def source_shape(tree):
    return SourceShape(tree)


def localize_mixed_edits(path, change, original, patched_tree):
    """Discard standalone comment edits only when the remaining edits produce the same AST."""
    edits = []
    skipped = False
    removed_lines = set()
    anchors = set()
    for hunk in change.hunks:
        cursor = hunk.old_start - bool(hunk.old_count)
        start = cursor
        added = []
        deleted = []
        for kind, line in (*hunk.lines, (" ", "")):
            if kind == "+":
                added.append(line)
                continue
            if kind == "-":
                deleted.append(line)
                cursor += 1
                continue
            if added or deleted:
                comment_only = all(
                    (not value.strip() or value.lstrip().startswith("#"))
                    and not DIRECTIVE.search(value) for value in (*added, *deleted)
                )
                if comment_only:
                    skipped = True
                else:
                    edits.append((start, cursor, added))
                    removed_lines.update(range(start + 1, cursor + 1))
                    if not deleted:
                        anchors.add(start)
                added, deleted = [], []
            cursor += 1
            start = cursor
    if not skipped or not edits:
        return change, patched_tree
    # A line that looks like a comment could actually be inside a string.
    # Rebuild the proposed source and check all AST fields, including constants.
    lines = original.splitlines(keepends=True)
    for start, stop, added in reversed(edits):
        lines[start:stop] = added
    try:
        revised = ast.parse("".join(lines), filename=path, type_comments=True)
    except (SyntaxError, ValueError):
        return change, patched_tree
    if not same_module(revised, patched_tree):
        return change, patched_tree
    return replace(change, old_lines=removed_lines, insertion_anchors=anchors), revised


class Selector:
    def __init__(self, source: Path, index: dict, tests: list[Test]):
        self.source = source
        self.files = index["files"]
        self.has_shared_scopes = any("shared" in file for file in self.files.values())
        self.readers = index["data_reads"]
        self.collection_reads = index["collection_reads"]
        self.dialects = index.get("dialects", {})
        self.expressions = index.get("expressions", {})
        self.default_sites = {}
        self.default_paths = set()
        self.scalar_reads = {}
        self.operand_sites = {}
        self.dispatch = {}
        self.dispatch_paths = set()
        self.lazy_sites = {}
        self.lazy_paths = set()
        self.configurations = {}
        self.configuration_paths = set()
        self.operand_files = set()
        nodes = index["nodes"]
        self.by_number = {t.number: t for t in tests}
        self.all = set(self.by_number)
        self.by_file = defaultdict(set)
        number_for = {t.node: t.number for t in tests}
        self.index_numbers = [number_for.get(node, -1) for node in nodes]
        self.cohorts = index["cohorts"]
        self.translated_cohorts = {}
        self.uncovered = self.all - set(self.index_numbers)
        self.uncovered.update(number_for[node] for node in index["nonpasses"] if node in number_for)
        for test in tests:
            self.by_file[test.file].add(test.number)

    def translate(self, items):
        return {number for i in items if (number := self.index_numbers[i]) >= 0}

    def cohort(self, number):
        group = self.translated_cohorts.get(number)
        if group is None:
            group = self.translated_cohorts[number] = self.translate(self.cohorts[number])
        return group

    def lines(self, file_info, lines):
        result = set()
        by_line = file_info["lines"]
        for line in lines:
            cohort = by_line.get(str(line))
            if cohort is not None:
                result.update(self.cohort(cohort))
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
                        consumers.update(self.cohort(cohort))
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
        token = name.encode("utf-8")
        for root in RUNTIME_ROOTS:
            for path in (self.source / root).rglob("*.py"):
                raw = path.read_bytes()
                if token in raw and references.search(raw.decode("utf-8", errors="replace")):
                    return True
        return False

    def arg_type_consumers(self, path, shape, change, old_tree, new_tree, single_file, affected):
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
            if single_file:
                contexts = arguments.changed_contexts(self.scalar_reads.get("arguments:" + key),
                                                      assignment.value, patched_value)
                if contexts is not None:
                    return self.translate(contexts)
            return affected | self.translate(self.expressions.get(key, ()))
        return None

    def configuration_consumers(self, path, change, old_tree, new_tree):
        if path not in self.configuration_paths:
            return None
        mutation = changed_keys(path, change, old_tree, new_tree)
        if mutation is None:
            return None
        site, changed = mutation
        reads = self.configurations.get(site)
        if not reads or reads.get("*"):
            return None
        consumers = set(reads.get("*", ()))
        for key in changed:
            consumers.update(reads.get(f"key:{key}", ()))
        if -1 in consumers:
            return None
        selected = self.translate(consumers)
        return selected | shared_fixture_consumers(self, selected)

    def test_body_owners(self, path, change, shape, old_tree, new_tree):
        """A body-only edit to one collected test preserves sibling tests' definitions."""
        owned = self.by_file.get(path, set())
        if not owned or not change.old_lines or change.insertion_anchors:
            return owned
        owners = {shape.function_at(line) for line in change.old_lines}
        if len(owners) != 1:
            return owned
        function = owners.pop()
        if not isinstance(function, FUNCTIONS) or not function.name.startswith("test"):
            return owned
        for outer, revised_outer in zip(old_tree.body, new_tree.body):
            if not isinstance(outer, ast.ClassDef) or not isinstance(revised_outer, ast.ClassDef):
                continue
            if function not in outer.body or len(outer.body) != len(revised_outer.body):
                continue
            revised = revised_outer.body[outer.body.index(function)]
            if type(revised) is not type(function):
                return owned
            original_body = revised.body
            revised.body = function.body
            try:
                if not same_module(old_tree, new_tree, change.old_lines):
                    return owned
            finally:
                revised.body = original_body
            exact = f"{path}::{outer.name}::{function.name}"
            matched = {n for n in owned if self.by_number[n].node.split("[", 1)[0] == exact}
            return matched or owned
        return owned

    def python_effects(self, path: str, change: FileDiff, single_file=True):
        info = self.files.get(path, {"lines": {}, "collection": []})
        owned = self.by_file.get(path, set())
        old_text = read_source(self.source / path)
        new_text = apply_to_text(old_text, change)
        old_tree = baseline_tree(self.source / path)
        readers = self.translate(self.readers.get(path, ()))
        try:
            new_tree = ast.parse(new_text, filename=path, type_comments=True)
            if same_patched_module(old_tree, new_tree, change):
                if not DIRECTIVE.search("\n".join(
                    line for hunk in change.hunks for kind, line in hunk.lines if kind != " "
                )):
                    return readers
            # Parsing alone accepts statements such as a return at module scope.
            # An equal AST already has the pinned module's validated semantics.
            compile(new_tree, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return owned if owned and not self.test_module_is_shared(path) else self.all
        if len(change.hunks) > 1 or (change.old_lines and change.insertion_anchors):
            change, new_tree = localize_mixed_edits(path, change, old_text, new_tree)
        if single_file and path in self.default_paths and not any(
            DIRECTIVE.search(line) for hunk in change.hunks for kind, line in hunk.lines if kind != " "
        ):
            nodes = defaults.contexts(path, change, old_tree, new_tree, self.default_sites)
            if nodes is not None:
                consumers = self.translate(nodes)
                # Shared fixtures and imports are recorded as unknown by the default profiler.
                return readers | owned | consumers | self.uncovered
        if single_file and path in self.dispatch_paths:
            site = changed_site(path, change, old_tree, new_tree, self.dispatch)
            if site is not None:
                calls = self.dispatch[site].get("key:call", ())
                if -1 not in calls:
                    consumers = self.translate(calls)
                    return readers | owned | consumers | shared_fixture_consumers(self, consumers) | self.uncovered
        shape = source_shape(old_tree)
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
        witness = following_lines(change, old_tree, new_tree, shape, info) if change.insertion_anchors else None
        if witness is not None:
            changed_lines.update(witness)
        for anchor in change.insertion_anchors if witness is None else ():
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
        visits = None
        if single_file and not import_time and path in self.lazy_paths:
            visits = lazy.changed_consumers(path, change, old_tree, new_tree, self.lazy_sites, shape)
        if visits is None and changed_lines.intersection(info["collection"]):
            import_time = True
        if not import_time and owned:
            owned = self.test_body_owners(path, change, shape, old_tree, new_tree)
        runtime = self.lines(info, changed_lines) if visits is None else self.translate(visits)
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
                runtime.update(self.cohort(cohort))
        affected = runtime | owned | readers
        if import_time:
            if path.startswith("sqlglot/") and eager_imports_changed(old_tree, new_tree):
                return self.all
            tracked = scalar.consumers(self, path, change, old_tree, new_tree) if single_file else None
            if tracked is not None:
                # Evaluating a class-body literal does not consume its value.
                # Import-time reads already force a fallback in scalar.consumers.
                return readers | owned | tracked | self.uncovered
            configuration = self.configuration_consumers(path, change, old_tree, new_tree) if single_file else None
            if configuration is not None:
                return affected | configuration | self.uncovered
            arg_types = self.arg_type_consumers(path, shape, change, old_tree, new_tree, single_file, affected)
            affected = self.collection_effects(path, affected) if arg_types is None else owned | readers | arg_types
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

    def input_only(self, change):
        """Identify companion files whose effects are captured by their source/data readers."""
        path = change.old_path
        if not path or path != change.new_path or not change.hunks or change.binary:
            return False
        if not path.endswith(".py"):
            return (path.startswith(SAFE_DOC_ROOTS) or
                    path.startswith("tests/fixtures/") and path in self.readers)
        if not path.startswith(RUNTIME_ROOTS) or path.endswith("/conftest.py"):
            return False
        modified = [line for hunk in change.hunks for kind, line in hunk.lines if kind != " "]
        if any((line.strip() and not line.lstrip().startswith("#")) or DIRECTIVE.search(line)
               for line in modified):
            return False
        original = read_source(self.source / path)
        revised = ast.parse(apply_to_text(original, change), filename=path, type_comments=True)
        return same_patched_module(baseline_tree(self.source / path), revised, change)

    def select(self, text: str):
        selected = set()
        try:
            changes = parse_patch(text)
            if not changes and text.strip():
                return set(self.all)
            # Fixture/source readers cover the companion edits independently.
            # Other tests see just the remaining executable patch.
            executable = []
            if len(changes) > 1:
                for change in changes:
                    if not self.input_only(change):
                        executable.append(change)
                    elif change.old_path.endswith(".py"):
                        selected.update(self.translate(self.readers.get(change.old_path, ())))
                    else:
                        selected.update(self.one_file(change))
            else:
                executable = changes
            for change in executable:
                selected.update(self.one_file(change, len(executable) == 1))
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
        def profile(filename, field):
            try:
                with gzip.open(args.index.with_name(filename), "rt", encoding="utf-8") as stream:
                    data = json.load(stream)
                if data["nodes"] == nodes:
                    return data[field]
            except (OSError, ValueError, KeyError):
                pass
            return {}

        def paths(data):
            return {site.rsplit(":", 2)[0] for site in data}

        selector.operand_sites = profile("operands.json.gz", "sites")
        selector.operand_files = paths(selector.operand_sites)
        selector.configurations = profile("configurations.json.gz", "maps")
        selector.configuration_paths = paths(selector.configurations)
        selector.dispatch = profile("dispatch.json.gz", "maps")
        selector.dispatch_paths = paths(selector.dispatch)
        selector.lazy_sites = profile("expressions.json.gz", "maps")
        selector.lazy_paths = paths(selector.lazy_sites)
        selector.default_sites = profile("defaults.json.gz", "scalars")
        selector.default_paths = paths(selector.default_sites)
        selector.scalar_reads = profile("scalars.json.gz", "scalars")
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
