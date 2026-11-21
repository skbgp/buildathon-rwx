# Blast Radius

This entry uses test coverage from the unmodified SQLGlot checkout to select and schedule tests for each incoming patch. The scorer supplies the patches and test catalogue at runtime. The entry writes one JSON line per patch with up to eight workers.

The index records pytest node IDs, Python line coverage, tests that read fixtures or source files, and use of dialect and expression classes. Process pool work and its serialization are attributed to the test that submitted the job. We map coverage back to the catalogue's test numbers at runtime. Patch parsing accounts for insertions, deletions, renames, multiple files, multiline expressions, and invalid Python. Changes that cannot be localized fall back to a wider set of tests.

For inserted statements the selector can use the next executed statement in the same branch. It checks scope changes before narrowing the selection. For short-circuit conditions and conditional expressions it records which tests entered each operand or branch during clean execution. Import and shared-fixture visits to an expression keep the broader selection.

Shared fixture setup and teardown contribute coverage to every test in the fixture scope. Collection-time code also contributes dependencies when a function runs during module initialization.

A second run of the clean suite records operand samples for comparisons, capped at 24 distinct pairs per test and source location. For an isolated change to one operator or one literal, the selector evaluates the old and new comparison on the recorded inputs. It keeps tests with unknown operators, overloaded equality, missing observations, shared fixture dependencies, or differing results. The same checks apply to any incoming diff.

For isolated class configuration edits, the selector can also consult clean-suite records of scalar attribute lookups and individual dictionary key reads. A bulk dictionary read or an import-time dependency triggers a wider selection. Handler tables have a separate index of calls to individual lambdas and closures returned by simple factories. Complex factories use the existing coverage fallback.

Workers are assigned using the recorded test durations and the two-second cost per worker. The scheduler checks one through eight workers, balances the load, and attempts a bounded exact search for small selections when the lower bound suggests a better arrangement. Unscheduled short tests can use spare worker time up to the existing peak.

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

On the 30-patch public sample the selector caught all 73 labeled failures and scored 106.632 simulated seconds. Commit `a031a6b` reached the same public score. On 13 local patches chosen to exercise branch selection and companion file edits, simulated time fell from 342.715 to 123.997 seconds. Four additional seeded mutations scored 12.505 seconds with either version. Both versions selected all 102 observed failing test/patch pairs across those 17 patches. The official practice and final batches use different patches.

## Refresh the coverage maps

The index is generated only from clean upstream tests. No labeled patches are used to generate it. The Docker build uses the organizer's pinned Python image and dependency lock. [coverage.py](https://github.com/nedbat/coveragepy) records which lines each pytest item executes.

```sh
docker build --platform linux/amd64 --target coverage-index -t blast-coverage .
container=$(docker create --platform linux/amd64 blast-coverage)
docker cp "$container:/app/coverage.json.gz" coverage.json.gz
docker rm "$container"

docker build --platform linux/amd64 --target operand-index -t blast-operands .
container=$(docker create --platform linux/amd64 blast-operands)
docker cp "$container:/app/operands.json.gz" operands.json.gz
docker rm "$container"
```

The additional profiles use the same pinned source and tests. Build the `configuration-index` target with its default `PROFILE=1` argument to reproduce the dictionary map, or with `--build-arg PROFILE=dispatch` to trace callable dispatch. Both targets write `/app/profile.json.gz`. The `scalar-index` target writes `/app/scalars.json.gz`. The `expression-index` target writes `/app/expressions.json.gz`.

Each build of the submission image downloads the pinned SQLGlot archive and verifies its SHA-256. The submission build reuses the index from this repository. Optional local pytest probes can be enabled with `RWX_PROBE=1`.

The entrant code is MIT-licensed. SQLGlot and the dependencies in `requirements.lock` retain their upstream licenses.
