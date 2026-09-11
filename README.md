<div align="center">
  <h1>seda-bus-compare</h1>
  <p><strong>Resolving Architecture &mdash; Clarity in Design</strong></p>
  <p>A cross-language comparison of the seven <a href="../">seda-bus</a> implementations.</p>
</div>

`seda-bus` — a small, broker-less, staged message bus — exists in seven
language ports: Java, Rust, Python, TypeScript, C++, C#, and Go, all built
from [one shared design](../DESIGN.md). This repo compares them: the same
benchmark workload, run identically in all seven, plus a table of the
structural differences already documented per-port (worker pool model,
dependencies, envelope source, and more).

- **[`METHODOLOGY.md`](METHODOLOGY.md)** — what's measured, how, and — just
  as important — what it doesn't mean. Read this before the numbers.
- **[`RESULTS.md`](RESULTS.md)** — the numbers.
- **[`bench/WORKLOAD.md`](bench/WORKLOAD.md)** — the benchmark specification
  every `bench/<lang>` program implements identically.

## Reproducing this

Requires Docker and the sibling repos checked out at the standard monorepo
layout (`setup.sh` verifies this, and can clone them for you):

```sh
./setup.sh                     # verify (or SETUP_CLONE=1 ./setup.sh to clone) sibling repos
./scripts/build_and_run.sh     # build all 8 images (7 languages + Python's
                                # free-threaded variant), run each, write
                                # results/raw/<lang>.jsonl
python3 scripts/aggregate.py   # write results/summary.csv, print the RESULTS.md table
```

Every language builds inside its own Docker container with a pinned base
image — see `bench/<lang>/Dockerfile` — so results don't depend on whatever
happens to already be installed on the machine running this. See
`METHODOLOGY.md` for why that matters here specifically (none of
`ra-common-*`/`seda-bus-*` are published to a package registry).

## Structure

```
bench/
  WORKLOAD.md       the shared benchmark specification
  <lang>/           one program per language, same logic, reading the spec
    Dockerfile      pinned toolchain, builds against the sibling ra-common-*/seda-bus-*
results/
  raw/<lang>.jsonl  one JSON object per trial (see WORKLOAD.md's output contract)
  summary.csv       aggregated by scripts/aggregate.py
scripts/
  build_and_run.sh  builds + runs every bench/<lang> image
  aggregate.py      raw/*.jsonl -> summary.csv + the RESULTS.md table
setup.sh            verifies/clones the sibling repos this depends on
```

## What this found

Building this surfaced three real bugs and one real methodology failure —
not just numbers.

The first full run reported Rust's `par` as flat and explained why with a
plausible-sounding theory. The theory was wrong, because the number was
wrong: the host was running concurrent Docker builds during the timed run.
Caught by direct pushback, confirmed with a clean isolated rerun, and fixed
by redoing the entire benchmark with nothing else executing concurrently —
see `METHODOLOGY.md`'s "A contaminated first run" section for the whole
story, including the exact numbers that didn't add up.

Separately, real: the first C++ run was 20-30x slower than Rust for
identical work. Traced to `ra-common-cpp` reopening `/dev/urandom` on every
random-byte call (fixed, commit `8700729`), then, after an isolated
micro-benchmark showed envelope construction alone was 9x faster than the
full bus path, to `Envelope::GetRoute()` cloning routes via a JSON
serialize/re-parse round trip instead of a proper clone (fixed, commit
`7e5717b`). A third bug — a single process-wide mutex still guarding the
(by-then already fixed) `/dev/urandom` handle, serializing every thread
regardless of channel — capped C++'s independent-channel scaling at 1.42x
versus Rust/Go's 5-6x; fixed by switching to `getentropy(2)`, a direct
syscall needing no shared state or lock (commit `1b3f687`), which took
`chan` to 5.29x.

The biggest structural finding: the benchmark's original `par` config (many
producers on one shared channel) measures lock contention, not parallel
capacity, and conflating the two was this report's own methodology gap. A
third configuration, `chan` (independent channels, one per producer), was
added specifically to answer "does parallelism actually work here" — and it
does, substantially, for every implementation except GIL-bound Python. See
`RESULTS.md` and `METHODOLOGY.md`'s "Does parallelism work?" section for
the controlled proof.

Throughput alone also turned out to hide a real finding: adding per-
envelope latency percentiles (`p50`/`p99`/`p999`/`max`) surfaced that
TypeScript's and Python's `seq`/`par` configs carry multi-*second* queueing
delays — the consumer can't keep pace with its producer, so a backlog
builds for the whole run — while every compiled, natively-multithreaded
implementation stays in the microseconds-to-low-milliseconds range in the
same configs. `chan` fixes most of it, most dramatically for free-threaded
Python (a >1000x drop in `p50`). See `RESULTS.md`'s "Latency" section.
