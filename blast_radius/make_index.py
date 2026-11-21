"""Record line, file, and collection coverage from the unmodified checkout."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
from multiprocessing.reduction import ForkingPickler
import os
from pathlib import Path
import sys
import tempfile

from .child_coverage import ModelUse, covered_job


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", nargs="+", default=["tests"])
    args = parser.parse_args()
    import coverage
    import pytest

    root = args.source.resolve()
    output = args.output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    prefixes = (f"{root}/sqlglot/", f"{root}/tests/")
    cov = coverage.Coverage(
        data_file=None,
        include=[prefix + "*" for prefix in prefixes],
        config_file=False,
    )
    temporary = tempfile.TemporaryDirectory(prefix="blast-coverage-")
    pool_dir = Path(temporary.name)
    original_submit = ProcessPoolExecutor.submit
    original_init = ProcessPoolExecutor.__init__
    model_use = ModelUse()
    context = ""
    opened = defaultdict(set)
    collection_reads = defaultdict(set)
    nodes = []
    baseline_nonpasses = set()
    descendants = defaultdict(set)
    fixture_members = {}
    fixture_keys = {}

    def switch_context(name):
        nonlocal context
        context = model_use.node = name
        cov.switch_context(name)

    def audit(event, values):
        if event != "open" or not isinstance(values[0], str):
            return
        path = values[0]
        if not path.startswith(prefixes):
            if path.startswith(("tests/", "sqlglot/")):
                path = os.path.join(root, path)
            else:
                return
        if path.endswith(".pyc") or "__pycache__" in path:
            return
        relative = os.path.relpath(path, root)
        if path.endswith(".py"):
            # Imports produce many source reads with no direct dependency on
            # text. Keep only explicit reads made by source-inspection tests.
            if not context:
                return
            frame = sys._getframe(1)
            while frame:
                filename = frame.f_code.co_filename
                if "importlib" in filename or "_pytest/assertion" in filename:
                    return
                if filename.startswith(prefixes[1]):
                    break
                frame = frame.f_back
            if frame is None:
                return
        if context:
            opened[relative].add(context)
            return
        frame = sys._getframe(1)
        while frame:
            filename = frame.f_code.co_filename
            if filename.startswith(prefixes[1]):
                collection_reads[relative].add(os.path.relpath(filename, root))
            frame = frame.f_back

    class Recorder:
        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            previous = context
            switch_context(item.nodeid)
            try:
                return (yield)
            finally:
                switch_context(previous)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if request.scope == "function":
                return (yield)
            scope = request.node
            if scope not in fixture_keys:
                key = f"fixture-scope:{len(fixture_keys)}"
                fixture_keys[scope] = key
                fixture_members[key] = descendants[scope]
            key = fixture_keys[scope]
            # pytest runs registered finalizers in reverse order. Bracket
            # teardown with the same context as setup, including on errors.
            teardown_parent = []

            def start_teardown():
                teardown_parent.append(context)
                switch_context(key)

            def end_teardown():
                switch_context(teardown_parent.pop())

            request.addfinalizer(end_teardown)
            previous = context
            switch_context(key)
            try:
                return (yield)
            finally:
                switch_context(previous)
                request.addfinalizer(start_teardown)

        def pytest_collection_finish(self, session):
            for item in session.items:
                nodes.append(item.nodeid)
                for collector in item.listchain():
                    descendants[collector].add(item.nodeid)
            model_use.install()

        def pytest_runtest_logreport(self, report):
            if report.failed or (report.skipped and report.when in ("setup", "call")):
                baseline_nonpasses.add(report.nodeid)

    def init_pool(self, max_workers=None, *args, **kwargs):
        original_init(self, 3 if max_workers is None else max_workers, *args, **kwargs)

    def submit(self, fn, /, *args, **kwargs):
        if not context:
            return original_submit(self, fn, *args, **kwargs)
        # Serialization is application code for SQLGlot expressions. Make
        # it happen while the submitter's pytest context is active on both
        # sides, instead of in an unattributed pool feeder/result thread.
        payload = bytes(ForkingPickler.dumps((fn, args, kwargs)))
        future = original_submit(self, covered_job, str(root), str(pool_dir),
                                 context, payload)
        result = future.result
        received = []

        def decode(timeout=None):
            if not received:
                received.append(ForkingPickler.loads(result(timeout)))
            return received[0]

        future.result = decode
        return future

    ProcessPoolExecutor.__init__ = init_pool
    ProcessPoolExecutor.submit = submit
    sys.addaudithook(audit)
    cov.start()
    result = pytest.main(
        ["-q", "-p", "no:cacheprovider", "-o", "console_output_style=count",
         "--continue-on-collection-errors", *args.limit], plugins=[Recorder()],
    )
    cov.stop()
    if not nodes:
        return int(result) or 1

    numbers = {node: i for i, node in enumerate(nodes)}
    data = cov.get_data()
    for path in sorted(pool_dir.glob("child-*")):
        if not path.suffix:
            child_data = coverage.CoverageData(basename=str(path))
            child_data.read()
            data.update(child_data)
        elif path.name.endswith(".usage.json"):
            usage = json.loads(path.read_text(encoding="utf-8"))
            for group in ("dialects", "expressions"):
                target = getattr(model_use, group)
                for name, items in usage[group].items():
                    target[name].update(items)
    temporary.cleanup()
    files = {}
    # Shared coverage sets (many adjacent lines are covered by identical items).
    cohorts = []
    cohort_ids = {}

    fixture_numbers = {key: {numbers[node] for node in members}
                       for key, members in fixture_members.items()}

    def expanded(contexts):
        ids = set()
        for name in contexts:
            if name in numbers:
                ids.add(numbers[name])
            elif name in fixture_numbers:
                ids.update(fixture_numbers[name])
        return sorted(ids)

    def cohort(test_nodes):
        ids = tuple(expanded(test_nodes))
        if ids not in cohort_ids:
            cohort_ids[ids] = len(cohorts)
            cohorts.append(ids)
        return cohort_ids[ids]

    for file in sorted(data.measured_files()):
        path = os.path.relpath(file, root)
        execution = data.contexts_by_lineno(file)
        files[path] = {
            "lines": {line: cohort(test_nodes) for line, test_nodes in sorted(execution.items())},
            "collection": sorted(line for line, names in execution.items() if "" in names),
            "shared": {line: cohort(shared) for line, names in sorted(execution.items())
                       if (shared := fixture_numbers.keys() & names)},
        }
    index = {
        "nodes": nodes,
        "cohorts": cohorts,
        "files": files,
        "data_reads": {path: expanded(ids) for path, ids in sorted(opened.items())},
        "collection_reads": {path: sorted(readers) for path, readers in sorted(collection_reads.items())},
        "dialects": {path: expanded(ids) for path, ids in sorted(model_use.dialects.items())},
        "expressions": {name: expanded(ids) for name, ids in sorted(model_use.expressions.items())},
        "nonpasses": sorted(baseline_nonpasses),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(filename=str(output), mode="wb", mtime=0) as stream:
        stream.write(json.dumps(index, separators=(",", ":")).encode())
    print(f"Coverage recorded for {len(nodes)} tests and {len(files)} Python files at {output}",
          file=sys.stderr)
    # A host-specific failing baseline test still contributes coverage up to the
    # failure. Consumers retain its scheduler ID if its selection is uncertain.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
