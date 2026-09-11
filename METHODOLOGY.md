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

## What went wrong, twice, and what it means for reading these numbers

The first C++ run showed ~15,000 envelopes/sec sequential — 20-30x slower
than Rust for an *identical* workload on two languages with nearly
identical bus designs (hand-rolled thread pool, no GC). That gap was
investigated, not accepted, and it took two separate fixes, not one:

1. `ra-common-cpp`'s random-byte source (`SecureRandomBytes`) was
   re-opening `/dev/urandom` with a fresh `fopen`/`fread`/`fclose` on
   *every single call*, and envelope construction calls it twice (once for
   the envelope id, once for the route's internal id) — 400,000 file-open
   cycles per trial. Every other language's random source is a single
   syscall or, for Python's non-cryptographic `random` module, no syscall
   at all. **Fixed: commit `8700729`** — keep the file handle open for the
   process, guarded by a mutex. ~136,000 eps, an 8-9x improvement.
2. That still left C++ ~4.7x behind Rust. Rather than accept "C++ is just
   slower," an isolated micro-benchmark measured envelope construction
   *alone*, no bus at all: 200,000 envelopes in a tight loop hit 1.19M eps —
   nearly 9x faster than the full publish-through-bus number at the time.
   That gap pointed straight at the bus path itself, and
   `Envelope::GetRoute()`/`Ratchet()` turned out to clone the current route
   by serializing it to a JSON tree and immediately re-parsing that tree
   back, just to get an independently-owned pointer — every other port's
   `GetRoute()` just copies a reference, since only C++'s `unique_ptr`
   ownership model needs an independent clone at all. **Fixed: commit
   `7e5717b`** — a proper virtual `Route::Clone()` instead. ~174,000 eps.

This pattern — isolate the suspicious component with a micro-benchmark,
don't accept "that's just how it is" — is why both bugs are in this
document and not a footnote. A 20-30x gap between two structurally similar
implementations is never "just how the language is." See `RESULTS.md`'s
C++ section for the full before/after numbers, both fixes.

`par` for C++ still doesn't scale past `seq` even after both fixes — it
collapses to ~63,000 eps against `seq`'s ~174,000, a 2.8x drop (down from
6x before the fixes). The remaining cause is the same mutex from fix #1:
every envelope still calls `SecureRandomBytes` twice, and those two calls,
from up to 8 producer threads, still serialize through one mutex-guarded
file handle. This specific bottleneck is confirmed markedly more expensive
under Docker's virtualization than running natively — a native,
non-containerized run on the same machine after both fixes showed `par`
(~157,000 eps) roughly matching `seq` (~156,000-176,000 eps), no collapse
at all — reproduced twice under Docker, and not a CPU-limiting artifact
(`nproc` inside the container correctly reports all 12 host cores). This is
documented, not chased further in this pass — a good next step for
`ra-common-cpp` would be per-thread random-byte buffering, or calling
`getrandom()` directly instead of sharing one `FILE*`. It's also a small,
concrete illustration of this report's bigger caveat: *where* you run a
benchmark can change not just the numbers but which implementation looks
best, especially for anything gated on lock contention.

## Rust's flat `par`: checked with the same rigor, different verdict

Rust's `par`/`seq` ratio (0.96x, effectively flat) looks like the same kind
of red flag as C++'s collapse. It was checked the same way — isolate
envelope construction from bus overhead — with the opposite conclusion.
Envelope construction alone, run at the same 1/2/4/8 thread counts as
`seq`/`par`, scales close to linearly (698,780 → 1,846,991 eps, 2.6x at 8
threads): Rust's allocator and runtime are not the bottleneck, and there is
no hidden bug analogous to C++'s two. What's actually happening: Rust's
`seq` (642,641 eps) is already within ~8% of bare single-threaded envelope
construction (698,780 eps) — bus overhead is close to zero, so there's
almost nothing left for more threads to parallelize — and
`seda-bus-rust`'s one channel is a single `Mutex<VecDeque<Envelope>>`
shared by every producer *and* every drain worker. With near-zero
per-envelope consumer work, 8 producers plus up to 8 drain workers
contending on that one mutex costs more than the extra parallelism saves —
a well-known effect (lock contention dominating when there's too little
work per critical section to amortize it), not a defect. Go/C#/Java's
`par` *does* scale under the identical single-mutex-per-channel design
(1.5-1.8x) precisely because their slower sequential baselines leave more
non-lock overhead for added workers to net a gain against. See
`RESULTS.md`'s Rust section for the full thread-count table.

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
