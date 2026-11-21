"""Run selected pytest node IDs against an isolated copy of one patch."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ProbeResult:
    passed: frozenset[str]
    failed: frozenset[str]
    skipped: frozenset[str]
    unknown: frozenset[str]


def _run(
    command: list[str], cwd: Path, deadline: float, env: dict[str, str] | None = None
) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            return process.wait(timeout=remaining) == 0
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return False


def _apply_patch(checkout: Path, patch: Path) -> None:
    # The isolated child runs this file directly, so it does not import the
    # sibling module unless preparing a checkout from the entry point.
    from .patches import apply_to_text, parse_patch

    text = patch.read_bytes().decode("utf-8", "surrogateescape")
    if not text:
        return
    if "\0" in text or any(
        line.startswith(("old mode ", "new mode ", "rename ", "copy "))
        or line.startswith(("new file mode ", "deleted file mode "))
        and line.rsplit(" ", 1)[-1] != "100644"
        for line in text.split("\n")
    ):
        raise ValueError("unsupported file metadata in patch")

    changes = parse_patch(text)
    if not changes:
        raise ValueError("no file changes in patch")

    seen = set()
    for change in changes:
        name = change.new_path or change.old_path
        if (
            name is None
            or name in seen
            or change.binary
            or not change.hunks
            or (
                change.old_path is not None
                and change.new_path is not None
                and change.old_path != change.new_path
            )
        ):
            raise ValueError("unsupported file change")
        seen.add(name)
        path = checkout
        for part in Path(name).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("cannot apply a patch through a symbolic link")
        if change.old_path is None:
            if path.exists():
                raise ValueError("new file already exists in checkout")
            original = ""
        else:
            original = path.read_bytes().decode("utf-8", "surrogateescape")
        updated = apply_to_text(original, change)
        if change.new_path is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(updated.encode("utf-8", "surrogateescape"))


def probe(
    source_root: Path,
    patch: Path,
    node_ids: Iterable[str],
    *,
    timeout: float,
    python: str = sys.executable,
) -> ProbeResult:
    """Probe exact node IDs. Keep all IDs outside `passed` in the selection.

    Only use passing results to prune if the probe environment matches the
    scorer and the tests are safe to run in a subset.
    """
    requested = frozenset(node_ids)
    empty = frozenset()
    if not requested or timeout <= 0:
        return ProbeResult(empty, empty, empty, requested)

    deadline = time.monotonic() + timeout
    results: dict[str, str] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="blast-probe-") as directory:
            work = Path(directory)
            checkout = work / "checkout"
            source_root = Path(source_root).resolve()
            patch = Path(patch).resolve()
            shutil.copytree(
                source_root,
                checkout,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache"
                ),
            )
            _apply_patch(checkout, patch)

            request = work / "request.json"
            response = work / "response.json"
            request.write_text(json.dumps(sorted(requested)), encoding="utf-8")
            env = os.environ.copy()
            for option in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
                env.pop(option, None)
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["TMPDIR"] = str(work)
            if _run(
                [python, "-IB", str(Path(__file__).resolve()), str(request), str(response)],
                checkout,
                deadline,
                env,
            ):
                results = json.loads(response.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ProbeResult(empty, empty, empty, requested)

    passed = frozenset(node for node in requested if results.get(node) == "passed")
    failed = frozenset(node for node in requested if results.get(node) == "failed")
    skipped = frozenset(node for node in requested if results.get(node) == "skipped")
    return ProbeResult(passed, failed, skipped, requested - passed - failed - skipped)


def _worker(request: Path, response: Path) -> None:
    import pytest
    from _pytest.subtests import SubtestReport

    if hasattr(os, "sched_getaffinity"):
        try:
            available = sorted(os.sched_getaffinity(0))
            # sqlglot's default process pools otherwise see every host CPU
            # despite the two-core cgroup quota.
            if len(available) > 2:
                os.sched_setaffinity(0, available[:2])
        except OSError:
            pass

    # -I drops inherited import paths. Prefer the patched checkout over any
    # installed copy of sqlglot or tests.
    sys.path.insert(0, os.getcwd())
    requested = set(json.loads(request.read_text(encoding="utf-8")))
    by_file: dict[str, list[str]] = {}
    for node in requested:
        file = node.split("::", 1)[0]
        by_file.setdefault(file, []).append(node)

    results: dict[str, str] = {}
    passed_phases: dict[str, set[str]] = {}
    collection_results: dict[str, str] = {}

    class Reporter:
        def pytest_collection_modifyitems(self, items):
            items[:] = [item for item in items if item.nodeid in requested]

        def pytest_collectreport(self, report):
            if report.failed:
                collection_results[report.nodeid] = "failed"
            elif report.skipped:
                collection_results[report.nodeid] = "skipped"

        def pytest_runtest_logreport(self, report):
            node = report.nodeid
            if node not in requested:
                return
            if report.failed:
                results[node] = "failed"
            elif isinstance(report, SubtestReport):
                # Successful/skipped subtests are separate from the call phase.
                return
            elif report.skipped or hasattr(report, "wasxfail"):
                if results.get(node) != "failed":
                    results[node] = "skipped"
            elif report.passed:
                passed_phases.setdefault(node, set()).add(report.when)

    files = sorted(file for file in by_file if Path(file).is_file())
    if files:
        status = pytest.main(
            [
                "-q",
                "--tb=no",
                "--disable-warnings",
                "--continue-on-collection-errors",
                "-p",
                "no:cacheprovider",
                *files,
            ],
            plugins=[Reporter()],
        )
        if status not in (
            pytest.ExitCode.OK,
            pytest.ExitCode.TESTS_FAILED,
            pytest.ExitCode.NO_TESTS_COLLECTED,
        ):
            return
        for file, nodes in by_file.items():
            if file in collection_results:
                for node in nodes:
                    results[node] = collection_results[file]
        for node, phases in passed_phases.items():
            if phases == {"setup", "call", "teardown"}:
                results.setdefault(node, "passed")

    response.write_text(json.dumps(results), encoding="utf-8")


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), Path(sys.argv[2]))
