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
overhead" number), and these are directional, not authoritative. `seq` = 1
producer/1 channel; `par` = 8 producers sharing 1 channel (lock
contention); `chan` = 8 producers, 8 independent channels (real parallel
capacity). 200,000 envelopes/trial, 3 trials/config, envelope construction
excluded from the timed window (see "What this found" below), all numbers
envelopes/sec.

| Implementation                | seq     | par     | par vs. seq |          chan | chan vs. seq |
|--------------------------------|--------:|--------:|------------:|--------------:|-------------:|
| Java                           | 861,490 | 556,566 |       0.65x | **1,968,262** |        2.28x |
| Go                             | 408,140 | 412,299 |       1.01x |     1,759,884 |        4.31x |
| Rust                           | 544,162 | 459,738 |       0.84x |       645,609 |        1.19x |
| C#                             | 272,740 | 273,234 |       1.00x |       387,930 |        1.42x |
| C++                            | 175,861 |  68,998 |   **0.39x** |       379,529 |        2.16x |
| Python 3.14t (free-threaded)   | 102,173 |  45,909 |       0.45x |       260,058 |        2.55x |
| TypeScript (Node 22)           |  21,619 |  14,436 |       0.67x |       124,338 |    **5.75x** |
| Python 3.13 (GIL)              |  53,148 |  36,206 |       0.68x |        35,660 |    **0.67x** |

![Throughput by implementation and configuration (log scale)](results/charts/throughput.svg)

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
| Java               | seq    |          12,301.0 |         18,253.4 |         18,650.2 |         26,527.6 |
| Java               | par    |           1,123.9 |          5,204.0 |          5,602.3 |          9,468.1 |
| Java               | chan   |          18,523.3 |         34,827.7 |         35,903.5 |         61,895.7 |
| Go                 | seq    |         172,144.8 |        252,232.7 |        253,427.0 |        299,153.7 |
| Go                 | par    |          53,172.8 |         88,893.3 |         89,190.0 |        102,887.4 |
| Go                 | chan   |          38,795.9 |         68,700.5 |         70,759.2 |         75,217.7 |
| Rust               | seq    |           3,597.4 |         44,018.3 |         44,556.3 |         63,028.0 |
| Rust               | par    |              61.1 |         66,075.7 |         67,513.4 |         70,363.9 |
| Rust               | chan   |         114,914.7 |        200,077.4 |        208,652.1 |        227,169.6 |
| C#                 | seq    |         197,274.0 |        312,282.7 |        314,225.8 |        439,031.3 |
| C#                 | par    |              15.8 |          1,805.3 |         35,645.0 |         79,071.4 |
| C#                 | chan   |           2,975.3 |        129,019.4 |        133,993.6 |        184,477.5 |
| C++                | seq    |              21.6 |            200.1 |            518.4 |         11,358.6 |
| C++                | par    |              13.6 |        153,688.0 |        155,973.3 |        161,260.6 |
| C++                | chan   |          58,806.7 |        234,509.3 |        295,332.8 |        322,910.3 |
| Python 3.14t       | seq    |         272,012.2 |        403,180.6 |        405,352.4 |        461,979.2 |
| Python 3.14t       | par    |       1,145,814.6 |      1,563,480.1 |      1,567,483.2 |      1,604,400.9 |
| Python 3.14t       | chan   |         173,936.8 |        336,498.2 |        338,502.5 |        360,242.7 |
| TypeScript         | seq    |       4,417,982.2 |      6,218,729.6 |      6,228,380.5 |      6,329,220.6 |
| TypeScript         | par    |       6,656,120.5 |      9,436,286.4 |      9,438,434.9 |      9,537,606.6 |
| TypeScript         | chan   |         600,404.0 |      1,076,210.8 |      1,077,789.3 |      1,183,206.8 |
| Python 3.13 (GIL)  | seq    |         581,421.6 |        950,212.9 |        955,746.0 |      1,210,851.6 |
| Python 3.13 (GIL)  | par    |       1,950,780.8 |      2,906,951.6 |      2,923,365.2 |      3,525,135.9 |
| Python 3.13 (GIL)  | chan   |       2,783,704.1 |      4,549,412.7 |      4,624,846.8 |      4,892,312.0 |

![Latency (p50/p99/p999/max) by implementation and configuration (log scale)](results/charts/latency.svg)

Only C++'s `seq`/`par` are genuinely clean (consumer always keeps pace);
everything else shows at least a tail-only stall, and most show a real,
sustained backlog once a fast-enough producer can outrun its consumer.
Rust's `seq` still carries a real, unresolved tail-latency stall
(`p50` fine, `max` still reaches 63ms) — previously attributed to the
`ra_common` rewire via an A/B test that itself predates this pass's
construction-exclusion fix, so that causal claim is flagged as open, not
re-confirmed. Full detail in `RESULTS.md`, including an "Every anomaly,
explained" section walking through each surprising number in both tables
above individually, not just the headline findings.

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

Building this surfaced three real C++ construction-time bugs, a real
methodology failure around a contaminated host, and — the biggest one —
a second real methodology failure: envelope *construction* was folded into
every "bus overhead" number in this report until a direct, specific
challenge caught it (*"we're not measuring the ability of this code to
create an Envelope"*). Fixing that reshuffled the entire throughput ranking
(Java now leads, not Rust) and overturned this report's own standing claim
that compiled languages never show backlog under `seq`/`par`/`chan` — they
do, several of them, once a producer isn't artificially throttled by how
long its own envelope takes to build. See `RESULTS.md`'s "Latency" section
for the corrected finding and the clean/tail-stall/backlog classification
that replaces it.

Separately, still true: the benchmark's `par` config (many producers on one
shared channel) measures lock contention, not parallel capacity, and
`chan` (independent channels, one per producer) was added specifically to
answer "does parallelism actually work here" — it does, substantially, for
every implementation except GIL-bound Python. The first full run also
reported Rust's `par` as flat due to a contaminated host running concurrent
Docker builds during the timed window; caught by direct pushback, fixed by
rerunning with nothing else executing. Both are historical corrections,
independent of this pass's construction-exclusion fix — see
`METHODOLOGY.md` for the full story on each.

C++'s first run was 20-30x slower than Rust for identical work, traced to
three real bugs in `ra-common-cpp`'s random-byte/route-clone construction
path (commits `8700729`, `7e5717b`, `1b3f687`) — all genuine fixes, but all
construction-time costs that this benchmark's timed window no longer
includes by design, so their dramatic before/after numbers are preserved
as history in `RESULTS.md`, not reproducible on this page anymore.

`seda-bus-rust` was the one port never built on `ra-common` until an
earlier pass rewired it onto `ra_common::Envelope` and measured a real
throughput cost from doing so. That conclusion is now revisited: this
pass's construction-excluded `seq` throughput (544,162 eps) is *higher*
than the old pre-rewire, construction-inclusive number (469,164) — hard to
reconcile with "the rewire has an ongoing dispatch-time cost." The likely
correct reading is that most of the previously-measured cost was
construction cost, now properly excluded, not a genuine per-envelope
dispatch tax — flagged as needing a fresh A/B test to confirm, not
asserted as settled. See `RESULTS.md`'s "Rust: the `ra-common` rewire,
revisited" section.
