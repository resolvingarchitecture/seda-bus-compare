<div align="center">
  <h1>seda-bus-compare</h1>
  <p><strong>Resolving Architecture &mdash; Clarity in Design</strong></p>
  <p>A cross-language comparison of the seven <a href="../">seda-bus</a> implementations.</p>
</div>

`seda-bus` — a small, broker-less, staged message bus — exists in seven
language ports: Java, Rust, Python, TypeScript, C++, C#, and Go, all built
from [one shared design](https://github.com/resolvingarchitecture/seda-bus-design). This repo compares them: the same
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
[`METHODOLOGY.md`](METHODOLOGY.md) first. Two benchmarks: **the capacity
curve** is primary — a real bounded queue (capacity 1024), real `Block`
backpressure, swept load at 0.5x/1.0x/1.5x each implementation's own
measured sustained throughput. **The firehose configs** (`seq`/`par`/
`chan`, below) are secondary and diagnostic — capacity large enough that
backpressure never engages, useful for finding lock-contention bugs, not
for capacity planning.

### Capacity curve

Each stage's own measured sustained throughput under real backpressure
(`cap1`: 1 producer; `cap8`: up to 8 producers sharing one channel):

| Implementation | `cap1` sustained eps | `cap8` sustained eps |
|---|--:|--:|
| Rust | 679,363 | 456,160 |
| Java | 648,341 | 444,318 |
| Go | 179,706 | 399,558 |
| C# | 239,952 | 340,295 |
| TypeScript | 203,898 | 219,670 |
| C++ | 179,119 | 90,980 |
| Python 3.14t (free-threaded) | 100,868 | 25,366 |
| Python 3.13 (GIL) | 41,126 | 16,648 |

![Capacity curve: achieved/target rate and p50 latency by load fraction](results/charts/capacity_curve.svg)

At paced load, every implementation tracks its own target closely at
0.5x-1.0x and, as it must, falls short at the deliberate 1.5x-overload
point — the story worth reading is in the shape of that fall (a graceful
latency increase vs. a cliff) and in C++'s `cap8` sustained throughput
being *lower* than its own `cap1` (two-lock-queue contention under 8-way
concurrency, visible here in a way the firehose config doesn't fully
expose) and C#'s consistently high tail latency at every load level, not
growing with load — see `RESULTS.md`'s "The capacity curve" section for
the full table and per-language detail.

### Firehose configs (secondary)

`seq` = 1 producer/1 channel; `par` = 8 producers sharing 1 channel (lock
contention); `chan` = 8 producers, 8 independent channels (real parallel
capacity). 200,000 envelopes/trial, 3 trials/config, envelope construction
excluded from the timed window, all numbers envelopes/sec.

| Implementation                | seq     | par     | par vs. seq |          chan | chan vs. seq |
|--------------------------------|--------:|--------:|------------:|--------------:|-------------:|
| Java                           | 795,225 | 450,484 |       0.57x | **2,028,409** |        2.55x |
| Go                             | 409,220 | 368,838 |       0.90x |     1,517,294 |        3.71x |
| Rust                           | 606,442 | **727,257** |   **1.20x** |       725,894 |        1.20x |
| C#                             | 280,944 | **421,688** |   **1.50x** |       497,239 |        1.77x |
| C++                            | 333,154 | 264,553 |       0.79x |       396,987 |        1.19x |
| Python 3.14t (free-threaded)   | 104,602 |  45,263 |       0.43x |       270,904 |        2.59x |
| TypeScript (Node 22)           |  22,035 |  14,798 |       0.67x |       129,225 |    **5.86x** |
| Python 3.13 (GIL)              |  51,649 |  28,417 |       0.55x |        35,285 |    **0.68x** |

![Throughput by implementation and configuration (log scale)](results/charts/throughput.svg)

Rust's and C#'s `par` both beat their own `seq` now (1.20x and 1.50x —
the best `par`-vs-`seq` ratio in this table) — both replaced a single
shared-mutex `Channel` data queue with a lock-free structure (Rust:
`crossbeam-queue`'s `ArrayQueue`; C#: two `ConcurrentQueue` lanes), the
same class of fix C++ applied earlier (a two-lock ring-buffer queue,
`par` no longer this report's worst collapse). See "What this found"
below.

Latency (`p50`/`p99`/`p999`/`max`, microseconds) is queueing delay under
a producer/consumer rate mismatch, not raw dispatch cost (`bench/
WORKLOAD.md` explains why) — several "compiled, natively-multithreaded"
implementations show a real, sustained backlog here, including in `chan`,
once producers aren't artificially throttled by construction cost. See
`RESULTS.md`'s "Latency" section for the full clean/tail-stall/backlog
classification, table, and per-language detail.

| Implementation    | Config |         p50 (us) |       p99 (us) |      p999 (us) |       max (us) |
|-------------------|--------|------------------:|----------------:|----------------:|----------------:|
| Java               | seq    |              24.7 |          3,511.2 |          3,862.5 |         29,356.2 |
| Java               | par    |             395.3 |          3,302.7 |          3,595.9 |          5,431.1 |
| Java               | chan   |          12,218.1 |         30,809.3 |         31,718.2 |         54,969.5 |
| Go                 | seq    |         202,909.0 |        283,866.0 |        284,528.2 |        324,208.6 |
| Go                 | par    |          61,375.2 |         96,719.1 |         97,144.9 |        117,794.5 |
| Go                 | chan   |          48,450.2 |         83,207.3 |         85,479.8 |         91,513.1 |
| Rust               | seq    |           9,450.3 |         11,338.6 |         11,496.7 |         56,689.6 |
| Rust               | par    |          93,125.7 |        161,851.3 |        162,625.6 |        163,702.1 |
| Rust               | chan   |         100,439.8 |        187,370.0 |        188,353.4 |        194,831.3 |
| C#                 | seq    |         211,067.7 |        339,757.4 |        340,372.7 |        454,541.2 |
| C#                 | par    |           3,945.2 |         82,015.9 |         95,806.7 |        116,063.0 |
| C#                 | chan   |          30,053.8 |        160,742.2 |        185,653.0 |        284,885.2 |
| C++                | seq    |             751.5 |          2,030.2 |          2,334.9 |          5,762.3 |
| C++                | par    |          14,680.6 |        141,592.0 |        145,229.1 |        207,443.1 |
| C++                | chan   |          82,814.2 |        365,371.7 |        369,832.4 |        375,301.1 |
| Python 3.14t       | seq    |         217,867.3 |        341,360.1 |        342,777.1 |        352,758.9 |
| Python 3.14t       | par    |       1,125,006.2 |      1,605,098.8 |      1,610,192.9 |      1,657,645.2 |
| Python 3.14t       | chan   |         211,862.4 |        325,257.1 |        327,857.0 |        348,108.4 |
| TypeScript         | seq    |       4,324,175.5 |      6,121,268.6 |      6,121,988.2 |      6,219,986.0 |
| TypeScript         | par    |       6,405,910.6 |      9,326,253.7 |      9,328,676.1 |      9,433,549.5 |
| TypeScript         | chan   |         599,264.8 |      1,012,212.5 |      1,013,109.0 |      1,043,043.0 |
| Python 3.13 (GIL)  | seq    |         615,576.8 |      1,146,722.5 |      1,156,797.2 |      1,363,498.2 |
| Python 3.13 (GIL)  | par    |       2,746,611.5 |      3,441,896.7 |      3,467,961.8 |      4,021,970.4 |
| Python 3.13 (GIL)  | chan   |       2,928,638.7 |      4,698,809.8 |      4,742,205.3 |      5,218,160.9 |

![Latency (p50/p99/p999/max) by implementation and configuration (log scale)](results/charts/latency.svg)

C++'s `seq` and Rust's `seq` are the only genuinely clean cases now
(consumer always keeps pace); both `par`s moved from "clean, mild tail" /
"a rare severe cliff" to "mild backlog" / "real, sustained backlog" as a
direct, expected consequence of each fix actually working (more real
throughput means a bigger average queue depth, by Little's law — not a
new problem). Everything else shows at least a tail-only stall, and most
show a real, sustained backlog once a fast-enough producer can outrun its
consumer. Rust's `seq` tail stall and `par`'s separate lock-contention
cliff are both fixed this pass (see "What this found" below). `chan`'s
real backlog in Rust is fully root-caused, not just explained: it's
genuine thread-count oversubscription (8 channels need 16 threads; this
benchmark host has 12 cores), confirmed by directly testing and ruling out
two other hypotheses first, with a measured recovery curve as channel
count drops toward the core budget — see `RESULTS.md`'s "Thread/channel
count must stay within the host's core budget." Full detail in
`RESULTS.md`, including an "Every anomaly, explained" section walking
through each surprising number in both tables above individually, and a
"Fixes applied this pass" section covering all four real fixes (and one
reverted attempt) in full.

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
`ra-common-*`/`seda-bus-*` are published to a package registry). Verify the
Docker Desktop VM is actually idle before trusting a run — `docker ps -q`
showing zero containers is not sufficient on its own; see `RESULTS.md`'s
"A note on host quietness" for what caught this pass's host contamination
and how it was confirmed resolved.

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

Building this surfaced three real C++ construction-time bugs, two real
methodology failures, and five real bugs/design issues found and **fixed
in the actual libraries**, not just measured:

- **`seda-bus-go` had a genuine lost-wakeup race**: `schedule()` gave up
  silently when it couldn't get a bus-wide permit, with no guarantee
  anything would ever retry a starved channel — exactly the mechanism
  behind this report's intermittent `chan` drain failures across two
  sessions. Fixed; verified with `go test -race` and 10 consecutive clean
  full-suite runs.
- **`seda-bus-rust`'s `seq` cold-wake stall** (worst case up to 260ms) is
  fixed by swapping the pool's job queue to `parking_lot` — a smaller,
  better-understood cost (~4.6% `par` throughput) than an earlier,
  reverted `crossbeam-channel` attempt (~15-20%).
- **`seda-bus-rust`'s `par` lock-contention cliff** (`p50` ~38us, `p99`
  ~70,000us — a ~1,800x jump) is a separate mechanism from the `seq` stall
  above (the channel's data queue, not the pool's job queue) and is fixed
  by swapping `Mutex<VecDeque<Envelope>>` for `crossbeam-queue`'s
  lock-free, bounded `ArrayQueue`: `par` throughput 427,650 → 709,629 eps
  (+66%), now beating `seq` and nearly matching `chan`. A first attempt
  regressed `seq` ~14x (an unconditional lock touch on every `poll()`,
  even with nothing waiting); fixed with an atomic waiter-count gate.
- **`seda-bus-cpp`'s single-mutex `Channel`** — this report's worst
  throughput collapse (`par` 0.39x) for three passes running — is fixed
  with a two-lock ring-buffer queue: `seq` nearly doubled, `par` more than
  tripled. A first attempt (a linked-list two-lock queue) fixed `par` but
  broke `seq`; verified clean under ThreadSanitizer in Docker after the
  fix.
- **`seda-bus-cs`'s single-lock `Channel`** — a first attempt (a
  hand-rolled two-lock queue) made things worse and was reverted; the
  design that worked replaced the `LinkedList` + single lock with two
  lock-free `ConcurrentQueue` lanes instead (retries and fresh admissions
  drained separately, retries always ahead), avoiding a wait inside a
  shared .NET `ThreadPool` work item — the likely reason the first
  attempt regressed. `par` throughput 272,370 → 421,688 eps, now beating
  `seq` (1.50x, the best ratio in this table).

Each port's own test suite additionally covers backpressure policies,
retry/dead-letter, consumer-failure isolation, shutdown accounting, config
validation, and resource lifecycle — see
[`seda-bus-design/CORRECTNESS_SUITE.md`](https://github.com/resolvingarchitecture/seda-bus-design/blob/master/CORRECTNESS_SUITE.md)
for the shared checklist and per-port coverage, which found and fixed a
handful of further real issues (a shutdown-timeout data-loss bug in Rust,
a zero-capacity hang in C++ and C#, among others) beyond what this
benchmark alone measures.

Separately, still true from earlier passes: envelope *construction* was
folded into every "bus overhead" number in this report until a direct,
specific challenge caught it (*"we're not measuring the ability of this
code to create an Envelope"*) — fixing that reshuffled the throughput
ranking (Java leads, not Rust) and overturned this report's own standing
claim that compiled languages never show backlog under `seq`/`par`/`chan`.
The benchmark's `par` config measures lock contention, not parallel
capacity, and `chan` was added specifically to answer "does parallelism
actually work here" — it does, substantially, for every implementation
except GIL-bound Python. The first full run also reported Rust's `par` as
flat due to a contaminated host; caught by direct pushback, fixed by
rerunning with nothing else executing. See `METHODOLOGY.md` and
`RESULTS.md`'s "Latency" section for both stories in full.

C++'s first run was 20-30x slower than Rust for identical work, traced to
three real bugs in `ra-common-cpp`'s random-byte/route-clone construction
path (commits `8700729`, `7e5717b`, `1b3f687`) — all genuine fixes, but all
construction-time costs that this benchmark's timed window no longer
includes by design, so their dramatic before/after numbers are preserved
as history in `RESULTS.md`, not reproducible on this page anymore.

`seda-bus-rust` was the one port never built on `ra-common` until an
earlier pass rewired it onto `ra_common::Envelope` and measured a real
throughput cost from doing so. That conclusion is still revisited, not
settled: this pass's construction-excluded `seq` throughput (576,594 eps)
is *higher* than the old pre-rewire, construction-inclusive number
(469,164) — hard to reconcile with "the rewire has an ongoing dispatch-time
cost." The likely correct reading is that most of the previously-measured
cost was construction cost, now properly excluded, not a genuine
per-envelope dispatch tax — flagged as needing a fresh A/B test to
confirm, not asserted as settled. Separately, `par`'s lock-contention
cliff (fixed this pass, above) was never a consequence of the rewire
either — it's a channel data-queue bottleneck unrelated to which envelope
type the bus carries. See `RESULTS.md`'s "Rust: the `ra-common` rewire,
revisited" section.
