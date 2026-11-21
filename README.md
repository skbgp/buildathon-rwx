# Blast Radius

This entry uses test coverage from the unmodified SQLGlot checkout to select and schedule tests for each incoming patch. The scorer supplies the patches and test catalogue at runtime. The entry writes one JSON line per patch with up to eight workers.

The index records pytest node IDs, Python line coverage, tests that read fixtures or source files, and use of dialect and expression classes. Process pool work and its serialization are attributed to the test that submitted the job. We map coverage back to the catalogue's test numbers at runtime. Patch parsing accounts for insertions, deletions, renames, multiple files, multiline expressions, and invalid Python. Changes that cannot be localized fall back to a wider set of tests.

Workers are assigned using the recorded test durations and the two-second cost per worker. The scheduler checks one through eight workers, balances the load, and attempts a bounded exact search for small selections when the lower bound suggests a better arrangement.

## Build and verify

The bundled index comes from [SQLGlot](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa) at the contest's pinned commit. To build the submission image:

```sh
docker build --platform linux/amd64 -t blast-radius .
```

Unpack the official [sample kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) in a separate directory. From the repository root, use its absolute path:

```sh
kit=/absolute/path/to/unpacked/sample-kit
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 -e HOME=/tmp \
  --mount "type=bind,src=$kit/sample/commits,dst=/data/commits,readonly" \
  --mount "type=bind,src=$kit/sample/tests.json,dst=/data/tests.json,readonly" \
  blast-radius > /tmp/blast-radius-output.jsonl
python3 "$kit/score.py" "$kit/sample" "$kit/sample/answers.json" /tmp/blast-radius-output.jsonl
```

This reports the simulated cost and missed commits. The contest adds container CPU time to the simulated cost. The optional native checker runs the selector with wall-time and CPU measurements while keeping the answer file outside the selector's data directory:

```sh
python3 tools/verify_sample.py "$kit" --source /path/to/pinned/sqlglot \
  --index coverage.json.gz --runs 3 --require-no-misses
```

On the 30-patch public sample, this entry selected all 73 labeled failures and scored 178.780 simulated seconds. The organizer's starter scored 1,210.470 seconds on the same input. The entry's median including CPU was 180.0 seconds in three local Linux-container runs. Hidden practice and final patches will differ.

## Refresh the index

The index is generated only from clean upstream tests. No labeled patches are used to generate it. The Docker build uses the organizer's pinned Python image and dependency lock. [coverage.py](https://github.com/nedbat/coveragepy) records which lines each pytest item executes.

```sh
docker build --platform linux/amd64 --target coverage-index -t blast-coverage .
container=$(docker create --platform linux/amd64 blast-coverage)
docker cp "$container:/app/coverage.json.gz" coverage.json.gz
docker rm "$container"
```

Each build of the submission image downloads the pinned SQLGlot archive and verifies its SHA-256. The submission build reuses the index from this repository. Optional local pytest probes can be enabled with `RWX_PROBE=1`.

The entrant code is MIT-licensed. SQLGlot and the dependencies in `requirements.lock` retain their upstream licenses.
