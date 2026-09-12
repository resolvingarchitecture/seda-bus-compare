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
[`METHODOLOGY.md`](METHODOLOGY.md) first — this report has been
substantially corrected twice by direct, specific pushback (a contaminated
host, then envelope construction cost silently folded into every "bus
overhead" number), and this pass went further and fixed three real bugs in
the libraries it measures (Go, Rust, C++ — see "What this found" below),
so these are directional, not authoritative. `seq` = 1 producer/1 channel;
`par` = 8 producers sharing 1 channel (lock contention); `chan` = 8
producers, 8 independent channels (real parallel capacity). 200,000
envelopes/trial, 3 trials/config, envelope construction excluded from the
timed window, all numbers envelopes/sec.

| Implementation                | seq     | par     | par vs. seq |          chan | chan vs. seq |
|--------------------------------|--------:|--------:|------------:|--------------:|-------------:|
| Java                           | 881,024 | 525,459 |       0.60x | **2,063,028** |        2.34x |
| Go                             | 400,955 | 423,400 |       1.06x |     1,505,869 |        3.76x |
| Rust                           | 537,687 | 427,650 |       0.80x |       660,248 |        1.23x |
| C#                             | 272,474 | 284,868 |       1.05x |       388,969 |        1.43x |
| C++                            | 325,638 | 247,211 |       0.76x |       385,515 |        1.18x |
| Python 3.14t (free-threaded)   | 104,790 |  44,899 |       0.43x |       268,483 |        2.56x |
| TypeScript (Node 22)           |  21,634 |  14,444 |       0.67x |       131,097 |    **6.06x** |
| Python 3.13 (GIL)              |  56,343 |  34,506 |       0.61x |        35,472 |    **0.63x** |

![Throughput by implementation and configuration (log scale)](results/charts/throughput.svg)

C++ moved the most here, and not from a methodology change — from a real
fix: `Channel`'s single mutex (shared by every producer *and* consumer on
a stage) was replaced with a two-lock ring-buffer queue this pass, nearly
doubling `seq` and more than tripling `par`. C++'s `par` is no longer this
report's worst collapse (0.76x now, was 0.39x). See "What this found"
below.

Latency (`p50`/`p99`/`p999`/`max`, microseconds) tells a different story —
it's queueing delay under a producer/consumer rate mismatch, not raw
dispatch cost (`bench/WORKLOAD.md` explains why). **This pass's central
finding: once producers aren't artificially throttled by construction
cost, several "compiled, natively-multithreaded" implementations show a
real, sustained backlog, including in `chan`** — the configuration this
report previously held up as having no artificial contention point left to
hide behind. That overturns this report's own earlier claim that compiled
languages never show this pattern. See `RESULTS.md`'s "Latency" section for
the full clean/tail-stall/backlog classification, table, and per-language
detail — it's long, and worth reading in full rather than summarized here.

| Implementation    | Config |         p50 (us) |       p99 (us) |      p999 (us) |       max (us) |
|-------------------|--------|------------------:|----------------:|----------------:|----------------:|
| Java               | seq    |          14,621.5 |         17,435.5 |         17,526.7 |         30,467.9 |
| Java               | par    |           1,659.5 |          7,307.2 |          7,883.4 |         21,449.0 |
| Java               | chan   |          12,353.7 |         30,062.0 |         31,827.3 |         50,403.2 |
| Go                 | seq    |         227,499.9 |        287,429.5 |        289,938.3 |        310,773.2 |
| Go                 | par    |          62,068.0 |         90,891.3 |         91,356.3 |        106,276.4 |
| Go                 | chan   |          50,100.7 |         83,650.7 |         87,403.5 |         92,168.9 |
| Rust               | seq    |           1,112.6 |          7,498.5 |         25,923.9 |         59,343.2 |
| Rust               | par    |              38.0 |         70,518.5 |         72,358.1 |         74,752.5 |
| Rust               | chan   |         114,795.5 |        202,384.8 |        205,534.6 |        213,932.0 |
| C#                 | seq    |         226,235.5 |        313,601.7 |        314,575.6 |        451,411.1 |
| C#                 | par    |              30.3 |         17,186.1 |         71,260.1 |        104,894.3 |
| C#                 | chan   |           5,964.8 |        161,171.8 |        165,363.0 |        215,132.7 |
| C++                | seq    |              75.9 |          8,219.4 |          9,564.6 |         23,344.3 |
| C++                | par    |          12,971.9 |         85,030.7 |         86,364.2 |        205,017.2 |
| C++                | chan   |          90,153.7 |        385,085.0 |        392,052.9 |        405,388.4 |
| Python 3.14t       | seq    |         253,086.8 |        381,380.1 |        383,181.2 |        426,324.5 |
| Python 3.14t       | par    |       1,155,311.8 |      1,630,461.8 |      1,632,925.8 |      1,687,036.4 |
| Python 3.14t       | chan   |         215,160.8 |        326,430.2 |        328,666.7 |        337,837.8 |
| TypeScript         | seq    |       4,429,715.5 |      6,243,199.7 |      6,244,154.2 |      6,281,834.1 |
| TypeScript         | par    |       6,646,763.5 |      9,524,249.1 |      9,525,822.8 |      9,560,764.3 |
| TypeScript         | chan   |         556,248.2 |      1,005,049.2 |      1,006,138.9 |      1,029,988.0 |
| Python 3.13 (GIL)  | seq    |         599,324.2 |      1,008,934.4 |      1,014,961.2 |      1,260,292.6 |
| Python 3.13 (GIL)  | par    |       1,833,113.1 |      2,529,340.5 |      2,551,785.8 |      2,968,997.8 |
| Python 3.13 (GIL)  | chan   |       2,814,971.7 |      4,547,659.7 |      4,672,587.2 |      5,169,673.3 |

![Latency (p50/p99/p999/max) by implementation and configuration (log scale)](results/charts/latency.svg)

Only C++'s `seq` is genuinely clean now (consumer always keeps pace); `par`
moved from "clean, mild tail" to "mild backlog" as a direct, expected
consequence of the fix actually working (more real throughput means a
bigger average queue depth, by Little's law — not a new problem).
Everything else shows at least a tail-only stall, and most show a real,
sustained backlog once a fast-enough producer can outrun its consumer.
Rust's `seq` tail stall is fixed this pass (worst case down from 260ms to
~27ms; see "What this found" below); `par`'s own cliff is separate and
still open. `chan`'s real backlog in Rust is fully root-caused, not just
explained: it's genuine thread-count oversubscription (8 channels need 16
threads; this benchmark host has 12 cores), confirmed by directly testing
and ruling out two other hypotheses first, with a measured recovery curve
as channel count drops toward the core budget — see `RESULTS.md`'s
"Thread/channel count must stay within the host's core budget." Full
detail in `RESULTS.md`, including an "Every anomaly, explained" section
walking through each surprising number in both tables above individually,
and a "Fixes applied this pass" section covering all three real fixes (and
one reverted attempt) in full.

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
methodology failures, and — this pass — three real bugs/design issues
found and **fixed in the actual libraries**, not just measured:

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
- **`seda-bus-cpp`'s single-mutex `Channel`** — this report's worst
  throughput collapse (`par` 0.39x) for three passes running — is fixed
  with a two-lock ring-buffer queue: `seq` nearly doubled, `par` more than
  tripled. A first attempt (a linked-list two-lock queue) fixed `par` but
  broke `seq`; verified clean under ThreadSanitizer in Docker after the
  fix.
- **A matching attempt on `seda-bus-cs` made things worse and was
  reverted** — not shipped as a partial win. See `RESULTS.md`'s "Fixes
  applied this pass" for the full investigation on all four, including why
  the C# one didn't work.

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
settled: this pass's construction-excluded `seq` throughput (537,687 eps)
is *higher* than the old pre-rewire, construction-inclusive number
(469,164) — hard to reconcile with "the rewire has an ongoing dispatch-time
cost." The likely correct reading is that most of the previously-measured
cost was construction cost, now properly excluded, not a genuine
per-envelope dispatch tax — flagged as needing a fresh A/B test to
confirm, not asserted as settled. See `RESULTS.md`'s "Rust: the
`ra-common` rewire, revisited" section.
