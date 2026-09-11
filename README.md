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
- **[`RESULTS.md`](RESULTS.md)** — the full numbers and narrative.
- **[`bench/WORKLOAD.md`](bench/WORKLOAD.md)** — the benchmark specification
  every `bench/<lang>` program implements identically.

## Results at a glance

Full narrative in [`RESULTS.md`](RESULTS.md); read
[`METHODOLOGY.md`](METHODOLOGY.md) first — the first pass at these numbers
was wrong (a contaminated host, caught and corrected) and these are
directional, not authoritative. `seq` = 1 producer/1 channel;
`par` = 8 producers sharing 1 channel (lock contention); `chan` = 8
producers, 8 independent channels (real parallel capacity). 200,000
envelopes/trial, 3 trials/config, all envelopes/sec.

| Implementation               |     seq |     par | par vs. seq |          chan | chan vs. seq |
|------------------------------|--------:|--------:|------------:|--------------:|-------------:|
| Rust                         | 208,568 | 516,836 |       2.48x | **1,075,379** |        5.16x |
| Go                           | 116,026 | 298,989 |       2.58x |       989,465 |    **8.53x** |
| Java                         | 307,890 | 543,493 |       1.77x |       903,183 |        2.93x |
| C++                          | 124,811 |  58,379 |   **0.47x** |       632,414 |        5.07x |
| C#                           | 158,952 | 282,073 |       1.77x |       420,814 |        2.65x |
| Python 3.14t (free-threaded) |  33,258 |  31,930 |       0.96x |        91,537 |        2.75x |
| TypeScript (Node 22)         |  16,344 |  16,577 |       1.01x |        51,407 |        3.15x |
| Python 3.13 (GIL)            |  35,892 |  10,958 |   **0.31x** |        11,959 |        0.33x |

![Throughput by implementation and configuration (log scale)](results/charts/throughput.svg)

Latency (`p50`/`p99`/`p999`/`max`, microseconds) tells a different story —
it's queueing delay under a producer/consumer rate mismatch, not raw
dispatch cost (`bench/WORKLOAD.md` explains why), and it's what actually
exposes TypeScript's and Python's multi-*second* backlogs under `seq`/`par`,
invisible in the throughput table above:

| Implementation    | Config |        p50 (us) |     p99 (us) |    p999 (us) |     max (us) |
|-------------------|--------|----------------:|-------------:|-------------:|-------------:|
| Rust              | seq    | 127,088.2 (noisy)† |  243,387.0 |    245,277.2 |    291,585.8 |
| Rust              | par    |            62.0 |      5,496.6 |      7,079.5 |     11,430.8 |
| Rust              | chan   |        22,022.1 |     52,850.4 |     55,073.8 |     63,226.8 |
| Go                | seq    |           618.9 |      5,150.4 |      7,194.9 |      8,569.6 |
| Go                | par    |         2,756.1 |     10,660.9 |     11,799.1 |     19,957.1 |
| Go                | chan   |         8,855.6 |     37,160.2 |     42,722.5 |     47,050.7 |
| Java              | seq    |             6.3 |         30.5 |         90.9 |      3,583.0 |
| Java              | par    |            12.3 |      1,137.9 |      2,027.3 |      4,001.0 |
| Java              | chan   |            10.8 |      1,611.8 |      2,913.2 |      6,004.8 |
| C++               | seq    |            30.9 |      2,419.7 |      3,316.1 |      6,444.7 |
| C++               | par    |            16.6 |        118.7 |        508.2 |      2,161.6 |
| C++               | chan   |         7,466.9 |     36,392.1 |     39,156.6 |     42,607.2 |
| C#                | seq    |             6.2 |         75.8 |      2,039.1 |     37,001.5 |
| C#                | par    |            10.2 |        122.4 |      1,595.7 |     12,550.3 |
| C#                | chan   |            11.9 |        435.9 |      2,901.8 |      9,638.3 |
| Python 3.14t      | seq    |            41.5 |     10,141.1 |     15,607.1 |     43,090.2 |
| Python 3.14t      | par    | **1,214,855.4** |  2,019,934.4 |  2,024,248.2 |  2,163,819.5 |
| Python 3.14t      | chan   |         1,159.9 |     32,746.1 |     44,886.0 |     58,822.5 |
| TypeScript        | seq    | **5,868,878.1** |  7,097,218.5 |  7,100,861.2 |  7,138,256.1 |
| TypeScript        | par    | **5,802,716.0** |  7,023,989.9 |  7,033,335.5 |  7,138,062.2 |
| TypeScript        | chan   |     1,516,281.1 |  1,884,631.7 |  1,890,423.6 |  1,948,672.1 |
| Python 3.13 (GIL) | seq    |        18,531.7 |     87,611.4 |    102,829.6 |    136,593.7 |
| Python 3.13 (GIL) | par    | **8,732,330.5** | 12,962,985.8 | 13,036,713.4 | 13,510,741.4 |
| Python 3.13 (GIL) | chan   |     8,251,324.3 | 13,130,436.2 | 13,237,334.5 | 14,322,201.9 |

![Latency (p50/p99/p999/max) by implementation and configuration (log scale)](results/charts/latency.svg)

† Rust's `seq` latency is unreliable on the host this was measured on
(real scheduling stalls hitting `seq`'s single-consumer design) — see
`RESULTS.md`'s "Latency" section for the mechanism. Its throughput numbers
above are unaffected.

## Reproducing this

Requires Docker and the sibling repos checked out at the standard monorepo
layout (`setup.sh` verifies this, and can clone them for you):

```sh
./setup.sh                     # verify (or SETUP_CLONE=1 ./setup.sh to clone) sibling repos
./scripts/build_and_run.sh     # build all 8 images (7 languages + Python's
                                # free-threaded variant), run each, write
                                # results/raw/<lang>.jsonl
python3 scripts/aggregate.py   # write results/summary.csv, print the RESULTS.md tables
python3 scripts/plot_charts.py # write results/charts/{throughput,latency}.svg (needs matplotlib)
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
  charts/*.svg      throughput/latency charts, generated by scripts/plot_charts.py
scripts/
  build_and_run.sh  builds + runs every bench/<lang> image
  aggregate.py      raw/*.jsonl -> summary.csv + the RESULTS.md tables
  plot_charts.py    summary.csv -> results/charts/*.svg
setup.sh            verifies/clones the sibling repos this depends on
```

## What this found

Building this surfaced three real C++ bugs, a real throughput cost from
rewiring Rust onto `ra-common`, and one real methodology failure — not
just numbers.

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

`seda-bus-rust` was the one port never built on `ra-common` — its own
minimal envelope instead of the shared one every other port carries.
Rewired onto `ra-common-rust` and measured: throughput dropped 0.39-0.72x
across configs, because `ra_common::Envelope` does ~3x more work per
construction (a routing-slip heap allocation, an identity struct, a
headers map, a document tree) than a bus-specific 7-field struct needs to.
Rust is now consistent with the rest of the ecosystem instead of the one
outlier. See `RESULTS.md`'s "Rust: the `ra-common` rewire" section.
