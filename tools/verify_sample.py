#!/usr/bin/env python3
"""Run the selector natively on the official sample and call its scorer."""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_LIMIT = 2_000_000


def child_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def show_details(answers: Path, output: Path) -> None:
    labels = json.loads(answers.read_text(encoding="utf-8"))["commits"]
    for line in output.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        expected = set(labels[row["commit"]]["failing"])
        selected = {test for worker in row["workers"] for test in worker}
        print(
            f"  {row['commit']}: {len(selected)} selected, "
            f"known failures={sorted(expected)}, missed={sorted(expected - selected)}"
        )


def main() -> int:
    arguments = sys.argv[1:]
    selector_args = []
    if "--" in arguments:
        divider = arguments.index("--")
        arguments, selector_args = arguments[:divider], arguments[divider + 1:]

    parser = argparse.ArgumentParser(
        description=__doc__, epilog="Additional selector options may follow --."
    )
    parser.add_argument("kit", type=Path, help="unpacked kit containing score.py and sample/")
    parser.add_argument("--source", required=True, type=Path, help="pinned SQLGlot checkout")
    parser.add_argument("--index", required=True, type=Path, help="coverage.json.gz")
    parser.add_argument("--runs", type=int, choices=(1, 3), default=1)
    parser.add_argument("--details", action="store_true", help="show failing and missed test IDs")
    parser.add_argument("--require-no-misses", action="store_true",
                        help="fail the verification if any known failure was missed")
    args = parser.parse_args(arguments)

    # Reserve these flags so the selector always receives the label-free copy.
    if any(flag.split("=", 1)[0] in {"--data", "--source", "--index"} for flag in selector_args):
        parser.error("--data, --source, and --index cannot be passed after --")

    kit = args.kit.resolve()
    sample = kit / "sample"
    answers = sample / "answers.json"
    scorer = kit / "score.py"
    kit_files = (answers, scorer, sample / "tests.json")
    if not all(path.is_file() for path in kit_files) or not (sample / "commits").is_dir():
        parser.error("kit must contain score.py, sample/answers.json, sample/tests.json, and sample/commits/")
    if not args.source.is_dir() or not args.index.is_file():
        parser.error("--source must be a directory and --index must be a file")

    totals = []
    found_misses = False
    with tempfile.TemporaryDirectory(prefix="blast-radius-verify-") as scratch:
        scratch = Path(scratch)
        data = scratch / "data"
        (data / "commits").mkdir(parents=True)
        for patch in (sample / "commits").glob("*.patch"):
            shutil.copyfile(patch, data / "commits" / patch.name)
        shutil.copyfile(sample / "tests.json", data / "tests.json")

        # Labels stay in the harness and scorer. The selector sees the same
        # two inputs as on the competition platform.
        command = [
            sys.executable, "-B", "-m", "blast_radius.select", *selector_args,
            "--data", str(data), "--source", str(args.source.resolve()),
            "--index", str(args.index.resolve()),
        ]
        output = scratch / "stdout.jsonl"
        for run in range(args.runs):
            print(f"Run {run + 1}", flush=True)
            before = child_cpu_seconds()
            start = time.monotonic()
            try:
                with output.open("wb") as stream:
                    selected = subprocess.run(command, cwd=ROOT, stdout=stream, timeout=180)
            except subprocess.TimeoutExpired:
                print("Selector exceeded the 180-second practice time limit.", file=sys.stderr)
                return 1
            elapsed = time.monotonic() - start
            cpu = child_cpu_seconds() - before
            if selected.returncode:
                print(f"Selector exited with status {selected.returncode}.", file=sys.stderr)
                return 1
            if output.stat().st_size > OUTPUT_LIMIT:
                print("Selector exceeded the 2 MB stdout limit.", file=sys.stderr)
                return 1

            scored = subprocess.run([
                sys.executable, str(scorer), str(data), str(answers), str(output),
                "--cpu-seconds", str(cpu),
            ], stdout=subprocess.PIPE, text=True)
            print(scored.stdout, end="")
            if scored.returncode:
                return scored.returncode
            print(f"Wall time: {elapsed:.3f} s")
            total = next(line.split()[1] for line in scored.stdout.splitlines() if line.startswith("Total:"))
            misses = next(line.split()[1] for line in scored.stdout.splitlines() if line.startswith("Misses:"))
            totals.append(round(float(total) * 1_000_000))
            found_misses |= int(misses) > 0
            if args.details:
                show_details(answers, output)

    if len(totals) == 3:
        rounded_tenths = (int(statistics.median(totals)) + 50_000) // 100_000
        print(f"Sample median (rounded): {rounded_tenths / 10:.1f} s")
    return int(args.require_no_misses and found_misses)


if __name__ == "__main__":
    raise SystemExit(main())
