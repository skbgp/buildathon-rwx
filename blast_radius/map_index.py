"""Merge clean configuration-map read observations."""

from collections import defaultdict
import gzip
import json


def merge(directory, output):
    nodes = json.loads((directory / "nodes.json").read_text())
    numbers = {node: i for i, node in enumerate(nodes)}
    sites = {}
    captures = sorted(directory.glob("worker-*.json"))
    if not captures:
        raise ValueError("missing configuration capture")
    for path in captures:
        for site, reads in json.loads(path.read_text()).items():
            consumers = sites.setdefault(site, defaultdict(set))
            for test, keys in reads.items():
                for key in keys:
                    consumers["*" if key is None else f"key:{key}"].add(numbers.get(test, -1))
    encoded = {site: {key: sorted(group) for key, group in consumers.items()}
               for site, consumers in sorted(sites.items())}
    with gzip.GzipFile(filename=str(output), mode="wb", mtime=0) as stream:
        stream.write(json.dumps({"nodes": nodes, "maps": encoded}, separators=(",", ":")).encode())
    print(f"Saved value dependencies for {len(encoded)} profiled locations")
