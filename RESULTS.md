# Results

Read [`METHODOLOGY.md`](METHODOLOGY.md) first. It's not optional this time:
the first pass at these numbers was contaminated by concurrent load on the
host and reported a wrong conclusion (Rust's `par` looking flat) with a
plausible-sounding but incorrect explanation attached. That was caught by
direct, specific pushback, verified with a clean rerun, and led to finding
three real bugs in `ra-common-cpp` and a genuine gap in how this
benchmark's `par` configuration was being interpreted. Everything below
reflects that — this is the corrected report, not the first draft.

**Run date:** 2026-09-11 (latest rerun: `seda-bus-rust` rewired onto
`ra-common-rust`, same as every other port, plus per-envelope latency
measurement added across all seven — see "Rust: the `ra-common` rewire"
and "Latency" below for what changed and why the numbers moved). Raw data:
[`results/raw/*.jsonl`](results/raw/), [`results/summary.csv`](results/summary.csv).
Regenerate with
`./scripts/build_and_run.sh && python3 scripts/aggregate.py && python3 scripts/plot_charts.py`
— on a quiet host; see `METHODOLOGY.md` for why that matters. The chart
script needs `matplotlib` (`pip install matplotlib`).

## Throughput: three configurations, not two

200,000 envelopes/trial, 3 trials/configuration. All numbers envelopes/sec,
mean of 3 trials (see `results/summary.csv` for min/max ranges).

- **`seq`** — 1 producer, 1 channel. Baseline.
- **`par`** — 8 producers, **1 shared channel**. Tests lock contention on a
  single stage.
- **`chan`** — 8 producers, **8 independent channels** (1:1). Tests actual
  parallel capacity with the artificial shared-lock contention point
  removed.

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

**Rust's numbers dropped substantially from the previous report** — `seq`
469,164 → 208,568, `chan` 2,725,588 → 1,075,379 — because `seda-bus-rust`
is no longer the one port outside this comparison's main variable: it's
now rewired onto `ra-common-rust`'s `Envelope`, same as the other six. See
"Rust: the `ra-common` rewire" below for why that costs real throughput,
not just changes bookkeeping. Every other language's numbers hold steady
run-to-run within normal noise. **Go's `chan` ratio (8.53x) is the highest
of the eight**, ahead of Rust's 5.16x, though Rust's absolute `chan`
throughput (1.08M eps) still leads Go's (989K) — a smaller `seq`
denominator makes for a bigger ratio without changing which implementation
is actually fastest in absolute terms; read the ratio and the absolute
number as answering different questions, not the same one.

**The headline finding:** every implementation gets real, often substantial
gains from parallelism — `chan` beats `seq` in all eight cases except
GIL-bound Python. The `par` column is not a measure of "how parallel is
this bus" — it's a measure of lock contention on one shared stage, and
conflating the two was the error in this report's first draft. See
`METHODOLOGY.md`'s "Does parallelism work?" section for the controlled
comparison that proves this rather than asserts it.

## Latency: what the tail looks like

Every delivered envelope's latency (consumer-invocation time minus its
embedded publish time) is now recorded and reduced to `p50`/`p99`/`p999`/
`max` per config, in microseconds. **Read `bench/WORKLOAD.md`'s "Latency"
section before these numbers** — this benchmark's capacity is deliberately
large enough that back-pressure never engages, so whenever a producer
publishes faster than its consumer(s) can drain, a real backlog builds and
these numbers measure *queueing delay*, not raw per-envelope dispatch cost.
That's not a flaw; it's a second, genuinely different signal from
throughput, and it shows things throughput alone hides completely.

| Implementation    | Config |        p50 (us) |     p99 (us) |    p999 (us) |     max (us) |
|-------------------|--------|----------------:|-------------:|-------------:|-------------:|
| Rust              | seq    |    **127,088.2**† |    243,387.0 |    245,277.2 |    291,585.8 |
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

**† Rust's `seq` latency is not a trustworthy number and is flagged, not
hidden.** `seq` has exactly one consumer thread draining a queue holding
all 200,000 envelopes at once (the same backlog-by-design structure
documented below for TS/Python) — so if that single thread is paused by
the host scheduler for even a few milliseconds, the *entire remaining
backlog's* measured latency shifts by that amount. On this benchmark host,
repeated runs showed real multi-millisecond single-thread scheduling
stalls (confirmed with an isolated `Instant::now()` jitter probe, unrelated
to this bus's code), and Rust's `seq` — now several hundred milliseconds
long per trial post-rewire, up from ~350ms before it — has enough time
exposure to reliably catch one. `par`/`chan` (8 threads, shorter trials)
mostly don't. The throughput numbers above are unaffected (computed from
only two clock reads per trial, not one pair per envelope) and were
consistent within a tight band across many reruns; the `seq` latency row
is reported for completeness, not as a precise measurement — a clean
number needs a dedicated quiet host, not something achieved this pass.

**The standout finding: TypeScript's and Python's `seq`/`par` latency is in
the single-digit *seconds*, while every other implementation stays in the
microseconds-to-low-milliseconds range for the same configs.** This is not
a per-envelope dispatch cost problem — it's queueing delay from a real
producer/consumer rate mismatch: TS's single-threaded event loop and
Python's GIL/free-threaded thread-switching overhead both mean the
consumer(s) can't keep up with a producer publishing as fast as it can into
a 200,000-capacity queue, so a backlog builds and later envelopes wait
behind thousands of earlier ones — for TS specifically, half of all
200,000 envelopes in a `seq` trial wait **longer than 5.8 seconds** before
being consumed, out of a total trial duration of only ~12 seconds: the
backlog is present for essentially the whole run, not a brief startup
spike. Compare this to `chan`, where splitting into 8
independent single-consumer channels lets each consumer roughly keep pace
with its own producer: TS's `chan` `p50` drops to 1.5 seconds (still huge
in absolute terms — Node's single thread is still splitting time across 8
event-loop tasks — but 4x tighter than `seq`/`par`), and Python 3.14t's
`chan` `p50` drops from 1.2 million microseconds to **1,160 microseconds**
— a >1000x improvement, the clearest evidence in this whole report that
free-threading actually fixes a real backlog problem, not just a
throughput number. The compiled, natively-multithreaded implementations
(Rust, Go, Java, C++, C#) never show this pattern at all — their `p50`
stays in the microseconds-to-low-milliseconds range in every config,
meaning their consumers keep pace with their producers throughout.

**A new, real, unresolved finding surfaced while collecting this data:**
`seda-bus-go`'s `chan` config failed to fully drain within its 60s shutdown
timeout on two separate runs during this session (`delivered` short of
`total`, `drained: false`), both times on a freshly quiet host with no
other Docker activity — not the contamination pattern documented above. A
third rerun of the same binary drained cleanly all three times. This looks
like a real, intermittent race in `seda-bus-go`'s shutdown/drain path under
the `chan` topology specifically (8 independent channels, high throughput),
not a host-contention artifact and not something in this benchmark's own
instrumentation (the failure is in `Shutdown()`'s drain accounting, not in
publish or latency recording). Not root-caused or fixed in this pass — flagged
here rather than silently discarded, per this report's own standard for
handling anomalies. The numbers in both tables above are from the clean
third run; any reproduction attempt should watch for a `drained: false`
line in `results/raw/go.jsonl` and discard/rerun that trial rather than
average it in.

## What went wrong, and what's real

- **Rust's original "flat `par`" (0.96x) was a measurement artifact**, not
  a finding. The host was running concurrent Docker builds during the
  original timed run; a clean rerun with nothing else executing shows real
  scaling (`par` 1.24x) and, more importantly, `chan` shows Rust scaling
  essentially as well as any implementation here (5.44x, second only to
  Go). The original "near-zero bus overhead, nothing left to parallelize"
  explanation was plausible-sounding and wrong — it explained a number that
  was itself wrong.
- **C++'s `par` collapse (now 0.47x, previously 0.34x pre-fix) is real and
  reproduces consistently** across multiple clean runs, unlike Rust's.
  Traced to three now-fixed bugs in `ra-common-cpp` (commits `8700729`,
  `7e5717b`, `1b3f687` — see `METHODOLOGY.md`). The third fix (removing the
  shared `/dev/urandom` mutex) fixed `chan` completely — 1.42x → ~5x,
  now in the same range as Rust/Go — which proves that mutex was never the
  cause of `par`'s collapse: `chan` has no shared queue, so a channel-level
  lock issue couldn't touch it, and it improved anyway once the urandom
  lock was gone. `par`'s remaining collapse is the channel's own
  bounded-queue mutex under Docker's virtualization specifically — a
  clean *native* run of the same binary shows `par` essentially matching
  `seq` (see `METHODOLOGY.md`), so this is a Docker-specific contention
  cost on that one lock, not a bug in the queue implementation itself.
- **Python 3.13's GIL collapse (0.29x/0.28x) is real in both
  configurations** — the GIL serializes bytecode execution regardless of
  how many channels exist, so removing the channel-level lock doesn't help
  when the deeper bottleneck is the interpreter itself.

## C++: three real bugs, found and fixed

The first C++ run (before any fixes) measured ~15,000 eps sequential —
20-30x slower than Rust for identical work between two hand-rolled-thread-
pool, no-GC implementations that should be structurally comparable.

1. **`ra-common-cpp` was re-opening `/dev/urandom`**
   (`fopen`/`fread`/`fclose`) on every random-byte call, twice per envelope
   — 400,000 file-open cycles per trial. **Fixed: commit `8700729`.**
2. **`Envelope::GetRoute()`/`Ratchet()` cloned routes via a JSON
   serialize-then-reparse round trip** instead of a proper clone, found by
   isolating envelope construction from bus overhead (200,000 envelopes,
   no bus: 1.19M eps — 9x the full-bus number at the time). **Fixed: commit
   `7e5717b`**, a proper virtual `Route::Clone()`.
3. **`SecureRandomBytes` kept the fixed `/dev/urandom` handle behind one
   process-wide `std::mutex`**, so every thread on every channel serialized
   on it regardless of channel topology — visible specifically in `chan`
   (independent channels, no queue lock at all), which still only reached
   1.42x versus Rust/Go's 5-6x. **Fixed: commit `1b3f687`**, replacing the
   FILE*+mutex with `getentropy(2)`, a direct syscall with no shared state
   and therefore no lock needed.

`seq` went from ~15,000 → ~136,000 (fix 1) → ~200,000 eps (fix 2, first
measurement) across the first two fixes, all Docker-measured; later clean
reruns put `seq` in the 125,000-135,000 range (see "reading this table
honestly" on run-to-run noise at single-thread scale). After fix 3, `chan`
jumped from 282,287 (1.42x) to **the 630,000-710,000 range across
reruns — consistently ~5x over `seq`**, right in Rust/Go's range. `par` did
*not* improve from fix 3 (still ~0.47x) — proof, not just inference, that
the urandom mutex was never `par`'s problem: `par`'s bottleneck is the
channel's own bounded-queue lock under Docker specifically (a native run of
the identical binary shows `par` ≈ `seq`; see `METHODOLOGY.md`).

## Rust: the `ra-common` rewire

`seda-bus-rust` was the one port never built on `ra-common` — its own
minimal 7-field `Envelope` (`id`, `to`, `sender`, `headers`, `payload:
Vec<u8>`, `slip: VecDeque<String>`, `attempts`). Asked directly whether
rewiring it onto `ra-common-rust`'s richer `Envelope` (a routing slip,
`Did`, headers map, document tree) would cost anything — "it's just code"
— the honest answer needed a real rewire and a real measurement, not an
inference. Both are done now (`ra_common::Envelope`, `make_envelope`/
`envelope_payload`/`target_service` mirroring the other six ports'
helpers, per-hop `attempts` moved onto the channel keyed by envelope id
since `ra_common::Envelope` has none — same pattern as every other port).

The first check was a construction-only micro-benchmark, and it said the
rewire was free — `ra_common::Envelope::document()` even measured *faster*
than the old struct. That check was run outside Docker, in this session's
own sandboxed execution environment, which turned out to be a poor proxy:
re-run inside Docker (this project's own canonical environment), the same
construction-only comparison flips hard, consistently across four runs:

```
ra_common::Envelope::document()+content   ~710-810k constructions/sec
seda_bus::Envelope::new() (old struct)    ~2.3-2.4M constructions/sec
```

`ra_common`'s construction is **~2.9-3.4x more expensive**, not faster —
`Uuid::new_v4()` isn't the cost (isolated: ~2M/sec, plenty fast); it's the
cumulative weight of `Did::default()`, a headers `Map::new()`, a
`DocumentMessage`'s `Vec<Map>`, and — every publish — `ra_common`'s
`DynamicRoutingSlip::next_route()` boxing the popped `Route` on the heap.
The old struct's `to: String` + `VecDeque<String>` slip needed none of
that. That gap shows up end-to-end: rewired `seq` throughput is 208,568
eps versus the pre-rewire 469,164 (0.44x), `par` 516,836 versus 719,627
(0.72x), `chan` 1,075,379 versus 2,725,588 (0.39x) — a real, consistent,
Docker-verified cost, most pronounced exactly where construction cost sits
most directly on the critical path (`seq`, `chan`'s many independent
low-contention channels) and least pronounced where lock contention
already dominates (`par`).

**So: rewiring is not "just code" here — it has a real, now-measured
throughput cost**, because `ra_common::Envelope` does more per envelope
than a bus-specific minimal struct needs to. That's the same tradeoff
every other `ra_common`-carrying port already made (see "Reading this
table honestly" below for how that reframes the whole table); Rust is now
consistent with the rest of the ecosystem instead of the one outlier, at
the price this comparison exists to make visible.

## Mutex vs. lock-free: why the design is what it is

`par`'s ceiling — real in every language, not just C++ — comes from a
single mutex-guarded queue shared by every producer and consumer for a
stage. That's the standard way to implement a bounded, backpressured
multi-producer/multi-consumer queue (what every one of the seven ports
does, including the Java original), not a deliberately introduced
bottleneck: it makes correctness easy, `Block`/`DropOldest` back-pressure
essentially free, and needs no dependency beyond each language's standard
library. The cost is exactly what `par` measures — one serialization point,
non-linear degradation under contention, worse under virtualization than
native (confirmed this session). See `METHODOLOGY.md` for the full
pros/cons and the lowest-risk improvement path (a two-lock queue, not a
full lock-free rewrite).

## Python: GIL vs. free-threaded

`seda-bus-python`'s own README documents a real ~2.6x free-threading
speedup on actual CPU-bound work (hashcash/fib). This benchmark's consumer
does almost no CPU work, so `par` (one shared channel, GIL or not) mostly
measures lock contention, not free-threading's benefit — 3.14t's `par` is
0.97x for exactly that reason. `chan` (no shared lock) shows the real
signal: 1.69x here, up to 3.40x in an isolated native check with more
trials — free-threading working once there's no artificial contention
point in the way. 3.13's GIL cost (0.29x/0.28x) is real in both
configurations, since the GIL serializes bytecode execution independent of
channel topology.

## TypeScript: `chan` helps even with zero real OS parallelism

`ts`'s `par` is flat (1.01x, expected — Node is single-threaded, `par`
means concurrent async tasks not OS parallelism). `chan` still shows a
real 3.37x gain with **no CPU parallelism available at all** — a single
heavily-contended channel carries real per-operation coordination overhead
(permit-checking, promise/microtask scheduling) beyond lock-waiting, and
splitting work across independent channels reduces that overhead even on
one thread.

## Other attributes

Pulled from [`seda-bus/DESIGN.md`](../DESIGN.md)'s own comparison table
(§2.1) — the maintained source, not re-derived here — plus three new
columns.

|                                   |              Java |        Rust |               Python |         TypeScript |                                        C++ |                  C# |                                       Go |
|-----------------------------------|------------------:|------------:|---------------------:|-------------------:|-------------------------------------------:|--------------------:|-----------------------------------------:|
| Version                           |             1.3.1 |       0.4.0 |                0.2.0 |              0.2.0 |                                      0.1.0 |               0.1.0 |                                    0.1.0 |
| Source LOC                        |               962 |         743 |                  595 |                835 |                                        744 |                 620 |                                      739 |
| Integration tests                 |                 8 |           9 |                   11 |                 16 |                                         13 |                  13 |                                       13 |
| Runtime deps beyond `ra-common-*` |                 0 |       `log` |                    0 |                  0 |                                          0 |                   0 |                                        0 |
| Envelope source                   |       `ra-common` | `ra-common` |          `ra-common` |        `ra-common` |                            `ra-common-cpp` |      `ra-common-cs` |                           `ra-common-go` |
| Worker pool                       | `ExecutorService` | hand-rolled | `ThreadPoolExecutor` |         event loop |                                hand-rolled | shared `ThreadPool` |                        none (goroutines) |
| True stage parallelism            |               yes |         yes |   only free-threaded | worker stages only |                                        yes |                 yes |                                      yes |
| Race/sanitizer-verified           |           not run |     not run |              not run |            not run | attempted, untrustworthy (TSan, sandboxed) |             not run | **yes** (`go test -race`, clean, 5 runs) |
| Guaranteed delivery               |               yes |          no |                   no |                 no |                                         no |                  no |                                       no |

Source LOC: `wc -l` over each port's library source only — see
[`scripts/count_loc.sh`](scripts/count_loc.sh).

## Reading this table honestly

Rust and Go's `chan` numbers being 5-8x, well ahead of Java/C#'s 2.6-2.9x,
is a real, structural result (both compile to native code with real OS
threads and no GC pause risk). Within the `ra-common`-carrying
implementations, don't read close percentage differences between adjacent
rows as meaningful given three trials on a single host; do read
order-of-magnitude differences, `par`-vs-`chan` gaps, and collapses as
real — every one reported here was independently checked against a clean,
isolated measurement, not assumed from a single run.

**This section previously carried a claim, then a retraction, about
whether Rust's own envelope mattered — both were wrong in the same way:
checked once, outside this project's own canonical environment, and
trusted too soon.** The first draft asserted (unmeasured) that
`seda-bus-rust`'s minimal struct explained part of its lead. Challenged
directly — "it's just code" — a construction-only micro-benchmark said no,
`ra_common`'s construction was if anything *faster*, and the claim was
retracted. That micro-benchmark ran natively, in this session's own
sandboxed shell, not in Docker; re-run inside Docker, the result reversed
completely (`ra_common` construction ~2.9-3.4x *more* expensive,
consistent across four runs — see "Rust: the `ra-common` rewire" above)
and the full rewired bus confirmed it end-to-end. The lesson generalizes
past Rust: a "verified" result is only as trustworthy as the environment
it was verified in, and this report's own native-vs-Docker gap (already
documented for C++'s `par` collapse) applies to *any* number measured
outside Docker here, not just throughput — this session just hadn't hit
it on a construction micro-benchmark until now. Every environment-crossing
claim from here forward gets checked in Docker before being trusted.

The same applies to the latency table: read the multi-second `seq`/`par`
numbers for TS/Python as a real backlog finding (order-of-magnitude, and
reproduced across trials), not as a precise "TypeScript takes 5.87 seconds
to process an envelope" claim — see "Latency" above and `bench/WORKLOAD.md`
for why this benchmark's capacity choice makes queueing delay, not raw
dispatch cost, the dominant signal whenever a producer outruns its
consumer.
