# Methodology

How the numbers in `RESULTS.md` were produced, and — more importantly —
what they don't mean.

## The workload

See [`bench/WORKLOAD.md`](bench/WORKLOAD.md) for the full specification.
Short version: one channel, one no-op consumer (an atomic increment), 200,000
envelopes per trial, three trials per configuration, at `seq` (1 producer,
concurrency 1) and `par` (P producers, concurrency P, P = min(8, cores)).
This isolates bus/scheduler overhead from business logic — it is
**deliberately not** a realistic application workload.

## Reproducing this

```sh
./setup.sh              # verifies the 14 sibling repos this depends on are present
./scripts/build_and_run.sh   # builds all 8 Docker images (7 languages + Python's
                              # free-threaded variant), runs each, writes
                              # results/raw/<lang>.jsonl
python3 scripts/aggregate.py # writes results/summary.csv, prints the RESULTS.md table
```

Every benchmark runs inside a Docker container with a pinned base image
(see each `bench/<lang>/Dockerfile`), not on whatever happens to be
installed on the machine running this. That matters here specifically
because none of `ra-common-*`/`seda-bus-*` are published to a package
registry — they're only resolvable as monorepo-relative source, so
"reproducible" has to mean "the same toolchain version building the same
source," not just "the same source."

All benchmarks in one run were executed on a single shared-tenancy Docker
host (see `results/raw/*.jsonl` for the exact numbers and `RESULTS.md` for
the date). Machine noise (other containers, host scheduler pressure,
thermal throttling) is not controlled for. Treat every number here as
**directional**, not a benchmark-grade measurement — the *relative*
comparisons (which implementation is faster than which, how much `par`
helps) are far more trustworthy than any single absolute throughput figure.

## What went wrong once, and what it means for reading these numbers

The first C++ run showed ~15,000 envelopes/sec sequential — 20-30x slower
than Rust for an *identical* workload on two languages with nearly
identical bus designs (hand-rolled thread pool, no GC). That gap was
investigated, not accepted: `ra-common-cpp`'s random-byte source
(`SecureRandomBytes`) was re-opening `/dev/urandom` with a fresh
`fopen`/`fread`/`fclose` on *every single call*, and envelope construction
calls it twice (once for the envelope id, once for the route's internal
id) — 400,000 file-open cycles per trial. Every other language's random
source is a single syscall (Go's `crypto/rand`, C#'s
`RandomNumberGenerator`, Node's `crypto.randomBytes`) or, for Python's
non-cryptographic `random` module, no syscall at all. Rust doesn't touch
randomness for IDs at all (`nanos-seq`). This was fixed in `ra-common-cpp`
(commit `8700729`) — keep the file handle open for the process, guarded by
a mutex — which brought C++ to ~8-9x faster, in the range of the other
compiled/hand-rolled-pool implementations. **The numbers in this report are
post-fix.**

The reason this is in the methodology doc rather than a footnote: it's the
best argument for why "throughput should all be about the same since
processing is negligible" is *not* a safe assumption to build this
benchmark around, and it's exactly what a workload this small is good for
finding. A 20-30x gap between two structurally similar implementations is
never "just how the language is" — it's a bug waiting to be found, and a
trivial-consumer benchmark makes bus/library overhead the *only* thing
being measured, which is precisely what surfaced this one. See
`RESULTS.md`'s C++ section for the before/after numbers.

`par` for C++ doesn't just fail to scale past `seq` after the fix — inside
Docker it gets *dramatically worse* (~45,000 eps vs. ~136,000 eps for
`seq`; see `RESULTS.md`), where a native, non-containerized run on the same
machine showed `par` roughly matching `seq` (~120,000 eps both). The root
cause is the same either way: every envelope still calls
`SecureRandomBytes` twice, and those now serialize through one
mutex-guarded file handle shared by all 8 producer threads. What's new is
that this specific bottleneck — 8 threads contending on one mutex/futex —
is markedly more expensive under Docker Desktop's virtualization layer than
running natively; confirmed reproducible (two independent runs, both
~3x worse under `par`) and not a CPU-limiting artifact (`nproc` inside the
container correctly reports all 12 host cores). This is documented, not
chased further — fixing the underlying contention (e.g., per-thread
buffering, or `getrandom()` directly instead of a shared `FILE*`) is future
work. It's also a small, concrete illustration of this report's bigger
caveat: *where* you run a benchmark can change not just the numbers but
which implementation looks best, especially for anything gated on lock
contention.

## Known limitations (by design, not oversight)

- **Trivial consumer work.** This isolates bus overhead, which is the point
  — but it means Python 3.14t's free-threading benefit barely shows up
  here (see `RESULTS.md`), even though `seda-bus-python`'s own README
  documents a real ~2.6x speedup for actual CPU-bound work (hashcash/fib).
  There is almost no CPU work in this benchmark's consumer for
  free-threading to parallelize — that's a property of this workload, not
  a finding about free-threaded Python's value.
- **No latency percentiles**, only aggregate throughput. A slow-tail
  envelope is invisible here.
- **No multi-stage/routing-slip itineraries, no back-pressure, no
  retry/dead-letter path.** Those are covered functionally (not for
  performance) by each port's own test suite.
- **Single machine, shared tenancy, three trials.** Enough to see real
  effects (GIL contention making Python slower under `par`, real
  multi-core scaling in Rust/Go/Java/C#/C++), not enough for statistical
  rigor. If you need that, increase `TRIALS` in `bench/WORKLOAD.md`'s spec
  and each `bench/<lang>` program.
- **The Java/`common` version-skew workaround.** `seda-bus-java`'s
  `pom.xml` depends on `resolvingarchitecture:common:1.2.0`, but
  `ra-common-java`'s own `pom.xml` is currently at `1.3.2`. Rather than
  edit either repo, `bench/java/Dockerfile` installs the built jar into the
  local Maven repo under both its real version and a `1.2.0` alias. This is
  a real, observed piece of drift in the ecosystem, not a benchmark
  artifact — worth fixing at the source at some point, out of scope here.
- **No official free-threaded Python Docker image exists** (checked:
  `python:3.14t-slim` and similar tags don't resolve on Docker Hub as of
  this writing), so `bench/python/Dockerfile.freethreaded` builds CPython
  3.14.0 from source with `--disable-gil`. This is a real, if slow
  (multi-minute), reproduction path — not a workaround that skips the
  variant.

## "Other attributes"

`RESULTS.md`'s attributes table pulls most of its columns directly from
[`seda-bus/DESIGN.md`](../DESIGN.md)'s own comparison table (§2.1) — that
document is the maintained source of truth for design decisions (worker
pool model, envelope source, `BATCH` size, dependencies, etc.); this report
doesn't re-derive it. New columns added here: source lines of code (`wc -l`
over each port's library source, excluding tests/vendored dependencies/build
output), integration test count, and whether the *implementation itself*
(not this benchmark) has been verified under a race detector/sanitizer —
`seda-bus-go` is the only one with a clean, trustworthy sanitizer run
(`go test -race`, five repeated runs); `seda-bus-cpp`'s ThreadSanitizer
attempt could not be trusted in the sandbox it was built in (see that
port's `DESIGN.md`).
