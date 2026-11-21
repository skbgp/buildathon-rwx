"""Build a bounded operand-coverage index by executing the pristine test suite."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import os
import re
from pathlib import Path
import sys
from multiprocessing.reduction import ForkingPickler
from multiprocessing.util import Finalize

from . import dispatch, lazy, map_reads, operands

_installed = None


def install():
    global _installed
    argv = sys.orig_argv
    if "-c" in argv:
        code = argv[argv.index("-c") + 1]
        if re.fullmatch(r"from multiprocessing\.resource_tracker import main;main\(\d+\)", code):
            return
    if _installed == os.getpid():
        return
    _installed = os.getpid()
    operands.install(os.environ["RWX_OBSERVE_ROOT"])
    directory = Path(os.environ["RWX_OBSERVE_OUTPUT"])
    path = directory / f"worker-{os.getpid()}.json"
    # A marker without a completed snapshot makes the whole index unusable.
    path.touch()
    def save():
        capture = map_reads.snapshot() if os.environ.get("RWX_OBSERVE_MAPS") == "1" else operands.encoded()
        if os.environ.get("RWX_OBSERVE_MAPS") == "dispatch":
            capture = {site: {node: ["call"] for node in reads} for site, reads in dispatch.sites.items()}
        if os.environ.get("RWX_OBSERVE_MAPS") == "lazy":
            capture = {site: {node: ["visit"] for node in nodes} for site, nodes in lazy.reads.items()}
        path.write_text(json.dumps(capture, separators=(",", ":")))
    Finalize(None, save, exitpriority=-5)


def set_context(name):
    operands.active_node = name


def job(node, payload):
    install()
    set_context(node)
    try:
        fn, args, kwargs = ForkingPickler.loads(payload)
        return bytes(ForkingPickler.dumps(fn(*args, **kwargs)))
    finally:
        set_context("")


def run():
    import pytest
    root = Path(os.environ["RWX_OBSERVE_ROOT"]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    submit = ProcessPoolExecutor.submit
    pool_init = ProcessPoolExecutor.__init__

    def init(self, max_workers=None, *args, **kwargs):
        pool_init(self, max_workers or 3, *args, **kwargs)

    def tracked_submit(self, fn, /, *args, **kwargs):
        node = operands.active_node
        if not node:
            return submit(self, fn, *args, **kwargs)
        future = submit(self, job, node, bytes(ForkingPickler.dumps((fn, args, kwargs))))
        result = future.result
        received = []
        def decoded(timeout=None):
            if not received:
                received.append(ForkingPickler.loads(result(timeout)))
            return received[0]
        future.result = decoded
        return future

    class Recorder:
        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            set_context(item.nodeid)
            try:
                return (yield)
            finally:
                set_context("")

        def pytest_collection_finish(self, session):
            Path(os.environ["RWX_OBSERVE_OUTPUT"], "nodes.json").write_text(
                json.dumps([item.nodeid for item in session.items])
            )

    ProcessPoolExecutor.submit = tracked_submit
    ProcessPoolExecutor.__init__ = init
    return pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[Recorder()])


def merge(directory, output):
    nodes = json.loads((directory / "nodes.json").read_text())
    ids = {node: i for i, node in enumerate(nodes)}
    sites = defaultdict(lambda: defaultdict(set))
    paths = list(directory.glob("worker-*.json"))
    if not paths:
        raise ValueError("missing operand workers")
    for path in paths:
        if not path.stat().st_size:
            # An abruptly stopped Python subprocess can miss its exit hook.
            # Retain the ordinary coverage selector if the profile is incomplete.
            raise ValueError(f"incomplete worker capture {path}")
        for site, by_node in json.loads(path.read_text()).items():
            for node, samples in by_node.items():
                target = sites[site]
                if samples is None or target[node] is None:
                    target[node] = None
                else:
                    target[node].update((tuple(a), tuple(b), same) for a, b, same in samples)
                    if len(target[node]) > 24:
                        target[node] = None
    encoded = {}
    for site, by_node in sites.items():
        if "" in by_node:
            continue
        groups = defaultdict(list)
        for node, samples in by_node.items():
            if samples:
                groups[tuple(sorted(samples))].append(ids[node])
        if groups:
            encoded[site] = [(samples, sorted(group)) for samples, group in groups.items()]
    with gzip.GzipFile(filename=str(output), mode="wb", mtime=0) as out:
        out.write(json.dumps({"nodes": nodes, "sites": encoded}, separators=(",", ":")).encode())
    print(f"Saved {len(encoded)} sites in {len(paths)} processes")


if __name__ == "__main__":
    if sys.argv[1:] == ["run"]:
        raise SystemExit(run())
    if sys.argv[1] == "maps":
        from .map_index import merge
        merge(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        merge(Path(sys.argv[1]), Path(sys.argv[2]))
