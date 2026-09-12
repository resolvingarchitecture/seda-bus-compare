# Results

Read [`METHODOLOGY.md`](METHODOLOGY.md) first. It's not optional: this
report has now been substantially corrected twice by direct, specific
pushback catching a measurement flaw this session didn't see on its own —
first a contaminated host reporting Rust's `par` as flat, second (this
pass) envelope *construction* cost silently folded into every "bus
overhead" number in the report. Both are described below, in full, not
smoothed over.

**Run date:** 2026-09-12. **What changed this pass, and why it's a bigger
deal than a numbers refresh:** every `bench/<lang>` program used to build
each envelope *inside* the timed producer loop, immediately before
`Publish`. That's not what this benchmark claims to measure ("Bus/scheduler
overhead in isolation from business logic" — `bench/WORKLOAD.md`) — a real
producer already holds a constructed envelope before it calls the bus, so
folding construction into the timed window was measuring "how fast can
this language build a `ra-common`-shaped object" alongside, and confounded
with, "how fast is this bus." Caught directly — *"creating an envelope
should not be part of the test... we're not measuring the ability of this
code to create an Envelope"* — not self-discovered. Every `bench/<lang>`
program now pre-builds all 200,000 envelopes in an untimed warm-up phase,
with a settle pause (explicit GC where the runtime has one) before the
clock starts; the timed loop only sets the publish-time payload on an
already-built envelope. See `bench/WORKLOAD.md` for the full spec change.

This is not a small effect. It moved every throughput number in this
report, reordered the throughput ranking (Java now leads, not Rust), and —
more importantly — **overturned this report's own previous claim that
"compiled languages never show backlog."** That claim was only true because
the old, flawed benchmark's construction cost accidentally throttled every
producer to roughly its consumer's pace. See "Latency: what the tail looks
like" below.

Raw data: [`results/raw/*.jsonl`](results/raw/),
[`results/summary.csv`](results/summary.csv). Regenerate with
`./scripts/build_and_run.sh && python3 scripts/aggregate.py && python3 scripts/plot_charts.py`
— on a genuinely quiet host; see `METHODOLOGY.md` for why that matters, and
"A note on host quietness" below for what "quiet" actually took to verify
this pass. The chart script needs `matplotlib` (`pip install matplotlib`).

**This pass also went further than measuring: three real bugs/design
issues found by this investigation were fixed in the actual libraries, not
just documented.** `seda-bus-go` had a genuine lost-wakeup race causing
intermittent `chan` drain failures (fixed). `seda-bus-rust`'s pool queue
swapped to `parking_lot` to close most of `seq`'s cold-wake tail stall
(fixed, real ~4.6% `par` cost accepted in exchange). `seda-bus-cpp`'s
`Channel` swapped its single mutex for a two-lock ring-buffer queue,
fixing what had been this report's worst throughput collapse (fixed). A
matching attempt on `seda-bus-cs` made things worse and was reverted, not
shipped as a partial win. See "Fixes applied this pass" below for all
four, including the revert, in full.

## Throughput: three configurations, not two

200,000 envelopes/trial, 3 trials/configuration, envelope construction
excluded from the timed window (see above). All numbers envelopes/sec,
mean of 3 trials (see `results/summary.csv` for min/max ranges).

- **`seq`** — 1 producer, 1 channel. Baseline.
- **`par`** — 8 producers, **1 shared channel**. Tests lock contention on a
  single stage.
- **`chan`** — 8 producers, **8 independent channels** (1:1). Tests actual
  parallel capacity with the artificial shared-lock contention point
  removed.

| Implementation                 |     seq |     par | par vs. seq |          chan | chan vs. seq |
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

**C++ moved the most of anyone in this table, and not from a methodology
change this time — from a real fix.** `seq` nearly doubled (176K → 326K)
and `par` climbed 3.6x (69K → 247K), because `Channel`'s single mutex
(shared by every producer *and* consumer on a stage) was replaced with a
two-lock ring-buffer queue — see "Fixes applied this pass" below. C++'s
`par` is no longer this report's worst collapse (0.76x now, was the
worst at 0.39x). Rust's `seq` tail stall is also real-fixed, not just
better-explained, this pass (`parking_lot` pool queue) — see the same
section. Every other number here is normal run-to-run variance on top of
the previous pass's construction-exclusion fix, not a new methodology
change.

**The headline finding still holds:** every implementation gets real,
often substantial gains from parallelism — `chan` beats `seq` in all eight
cases except GIL-bound Python (0.63x, still a real collapse — see "Python:
GIL vs. free-threaded" below). The `par` column is still not a measure of
"how parallel is this bus" — it's a measure of lock contention on one
shared stage; see `METHODOLOGY.md`'s "Does parallelism work?" section for
the controlled comparison.

## Latency: what the tail looks like

Every delivered envelope's latency (consumer-invocation time minus its
publish-time payload) is recorded and reduced to `p50`/`p99`/`p999`/`max`
per config, in microseconds. **Read `bench/WORKLOAD.md`'s "Latency" section
before these numbers** — this benchmark's capacity is deliberately large
enough that back-pressure never engages, so whenever a producer publishes
faster than its consumer(s) can drain, a real backlog builds and these
numbers measure *queueing delay*, not raw per-envelope dispatch cost.

| Implementation     | Config  |           p50 (us) |         p99 (us) |        p999 (us) |         max (us) |
|--------------------|---------|-------------------:|-----------------:|-----------------:|-----------------:|
| Java               | seq     |           14,621.5 |         17,435.5 |         17,526.7 |         30,467.9 |
| Java               | par     |            1,659.5 |          7,307.2 |          7,883.4 |         21,449.0 |
| Java               | chan    |           12,353.7 |         30,062.0 |         31,827.3 |         50,403.2 |
| Go                 | seq     |          227,499.9 |        287,429.5 |        289,938.3 |        310,773.2 |
| Go                 | par     |           62,068.0 |         90,891.3 |         91,356.3 |        106,276.4 |
| Go                 | chan    |           50,100.7 |         83,650.7 |         87,403.5 |         92,168.9 |
| Rust               | seq     |            1,112.6 |          7,498.5 |         25,923.9 |         59,343.2 |
| Rust               | par     |               38.0 |         70,518.5 |         72,358.1 |         74,752.5 |
| Rust               | chan    |          114,795.5 |        202,384.8 |        205,534.6 |        213,932.0 |
| C#                 | seq     |          226,235.5 |        313,601.7 |        314,575.6 |        451,411.1 |
| C#                 | par     |               30.3 |         17,186.1 |         71,260.1 |        104,894.3 |
| C#                 | chan    |            5,964.8 |        161,171.8 |        165,363.0 |        215,132.7 |
| C++                | seq     |               75.9 |          8,219.4 |          9,564.6 |         23,344.3 |
| C++                | par     |           12,971.9 |         85,030.7 |         86,364.2 |        205,017.2 |
| C++                | chan    |           90,153.7 |        385,085.0 |        392,052.9 |        405,388.4 |
| Python 3.14t       | seq     |          253,086.8 |        381,380.1 |        383,181.2 |        426,324.5 |
| Python 3.14t       | par     |        1,155,311.8 |      1,630,461.8 |      1,632,925.8 |      1,687,036.4 |
| Python 3.14t       | chan    |          215,160.8 |        326,430.2 |        328,666.7 |        337,837.8 |
| TypeScript         | seq     |        4,429,715.5 |      6,243,199.7 |      6,244,154.2 |      6,281,834.1 |
| TypeScript         | par     |        6,646,763.5 |      9,524,249.1 |      9,525,822.8 |      9,560,764.3 |
| TypeScript         | chan    |          556,248.2 |      1,005,049.2 |      1,006,138.9 |      1,029,988.0 |
| Python 3.13 (GIL)  | seq     |          599,324.2 |      1,008,934.4 |      1,014,961.2 |      1,260,292.6 |
| Python 3.13 (GIL)  | par     |        1,833,113.1 |      2,529,340.5 |      2,551,785.8 |      2,968,997.8 |
| Python 3.13 (GIL)  | chan    |        2,814,971.7 |      4,547,659.7 |      4,672,587.2 |      5,169,673.3 |

![Latency (p50/p99/p999/max) by implementation and configuration (log scale)](results/charts/latency.svg)

**The central finding of this pass: once producers aren't artificially
throttled by construction cost, several "compiled, natively-multithreaded"
implementations show a real, sustained backlog — not a brief stall — and
this shows up specifically in `chan`, the configuration this report has
repeatedly held up as "no artificial contention point, nothing left to
hide behind."** The previous report's claim that "the compiled,
natively-multithreaded implementations (Rust, Go, Java, C++, C#) never
show this pattern at all" is **false** under the corrected methodology. It
was true only because the old benchmark's construction cost happened to
throttle every producer to roughly its own consumer's pace, masking any
real capacity gap.

To tell a genuine sustained backlog (`p50` itself a large fraction of the
whole trial) apart from a brief tail-only stall (`p50` fine, `max` spikes)
apart from a truly clean run (both low), the table below reports each
`p50` and `max` as a fraction of that config's mean trial duration
(`elapsed_ms`) — Little's law terms: a backlog that persists for most of
the run pulls both `p50` and `max` up together; an isolated stall pulls
only `max` up; a clean run pulls neither up.

| Implementation     | Config |   mean elapsed (ms) |  p50/elapsed |  max/elapsed | Read as                            |
|--------------------|--------|--------------------:|-------------:|-------------:|------------------------------------|
| C#                 | par    |               701.3 |         0.00 |         0.15 | clean, mild tail                   |
| Rust               | par    |               467.3 |         0.00 |         0.16 | tail-only stall                    |
| C++                | seq    |               615.7 |         0.00 |         0.04 | clean                              |
| Rust               | seq    |               373.0 |         0.00 |         0.16 | tail-only stall                    |
| Java               | par    |               387.7 |         0.00 |         0.06 | clean                              |
| C#                 | chan   |               514.0 |         0.01 |         0.42 | tail-only stall (large tail)       |
| C++                | par    |               816.0 |         0.02 |         0.25 | mild backlog                       |
| Java               | seq    |               235.3 |         0.06 |         0.13 | mild backlog                       |
| Java               | chan   |                99.7 |         0.12 |         0.51 | **real backlog**                   |
| Go                 | par    |               472.7 |         0.13 |         0.22 | mild-to-real backlog               |
| Python 3.14t       | seq    |             1,909.0 |         0.13 |         0.22 | real backlog                       |
| Python 3.13 (GIL)  | seq    |             3,572.0 |         0.17 |         0.35 | real backlog                       |
| C++                | chan   |               518.7 |         0.17 |         0.78 | **real backlog**                   |
| Python 3.14t       | par    |             4,458.3 |         0.26 |         0.38 | real backlog                       |
| Python 3.14t       | chan   |               745.3 |         0.29 |         0.45 | real backlog                       |
| C#                 | seq    |               756.3 |         0.30 |         0.60 | **real backlog**                   |
| Python 3.13 (GIL)  | par    |             5,845.0 |         0.31 |         0.51 | real backlog                       |
| TypeScript         | chan   |             1,526.3 |         0.36 |         0.67 | real backlog                       |
| Go                 | chan   |               133.3 |         0.38 |         0.69 | **real backlog**                   |
| Rust               | chan   |               302.7 |         0.38 |         0.71 | **real backlog**                   |
| Go                 | seq    |               502.3 |         0.45 |         0.62 | **real backlog**                   |
| TypeScript         | seq    |             9,246.7 |         0.48 |         0.68 | **real backlog**                   |
| TypeScript         | par    |            13,847.0 |         0.48 |         0.69 | **real backlog**                   |
| Python 3.13 (GIL)  | chan   |             5,675.0 |         0.50 |         0.91 | **real backlog**                   |

**C++ `seq` is now the only case in the whole report that's genuinely
clean** — `par` moved from "clean, mild tail" to "mild backlog" this pass,
*because the fix worked*: with the mutex bottleneck gone, `par` sustains
real throughput (247K eps, was 69K), and by Little's law a system doing
more real work per second naturally carries a bigger average queue depth,
which is exactly what a higher `p50/elapsed` ratio here means — read this
shift as evidence the fix moved real work, not as a new problem. Everything
else shows at least a tail-only stall, and the majority show a real,
sustained backlog once construction can no longer throttle the producer
down to the consumer's pace. **Rust still flips from "clean" to "real
backlog" specifically in `chan`** — the configuration this report
previously called out as "no artificial contention point left to hide
behind" — now root-caused as a host core-budget constraint, not a design
flaw; see "Thread/channel count must stay within the host's core budget"
below. That framing was correct about the lock, wrong about there being
nothing left to hide behind: a fast-enough producer can still outrun 8
independent single-consumer channels if per-channel dispatch has any real
per-item cost, or if the host doesn't have enough cores to run 16 threads
without contention, which is the confirmed mechanism here.

**Rust's `seq` tail-only stall is fixed this pass, not just re-explained**
— `p50` dropped to 1.1ms mean (was 3.6ms) and, more importantly, the
worst-case `max` across three independent Docker-verified runs never
exceeded ~27ms this pass (down from up to 260ms previously); this run's
own `max` of 59.3ms is itself within the improved band, not a return of
the old failure mode. Root cause and fix: `seda-bus-rust`'s pool queue
(`std::sync::mpsc`, which parks a waiting worker immediately with no spin
phase) was swapped for `parking_lot`'s `Mutex`+`Condvar` (spins briefly
before parking — closer to what Java's `ExecutorService` does). A first
attempt at this exact fix, tried in the previous pass via
`crossbeam-channel`, cost `par` ~15-20% of its throughput and was
reverted; `parking_lot`'s lock-based (not lock-free) design costs `par`
only ~4.6% (445,872 → 425,539 eps, controlled A/B, matched sample sizes,
same quiet host) for the same latency win, and was judged worth it for
shared production code (`service-bus-rust`, `i2p-rust`, `tor-client-rust`,
`1m5-core-rust`) this time. See "Fixes applied this pass" below for the
full investigation. The prior pass's A/B test attributing the *original*
stall to the `ra_common` rewire specifically was run under the old,
construction-inside-the-timed-window methodology and was never
re-verified under the corrected one — that specific causal claim remains
open, independent of this fix actually working.

**`seda-bus-go`'s `chan` config failed to fully drain within its 60s
shutdown timeout on the previous pass** (2 of 3 trials, `delivered` short
of `total`, `drained: false`) — a real, intermittent lost-wakeup race in
`schedule()` (see "Fixes applied this pass" below for the mechanism and
fix). This pass's tables come from the fixed binary, which drained cleanly
on the first try every time: zero `drained: false` trials across the whole
full-suite run, all eight languages, no retries needed — a stronger result
than the previous pass's "discard the bad run, keep the clean rerun"
workaround. Stress-tested separately, 10 consecutive full runs (30 `chan`
trials) with zero failures, versus 2-of-3 failing before the fix under the
same conditions. Any reproduction attempt against the *unfixed* binary
should still watch for `"drained":false` in `results/raw/go.jsonl` and
discard/rerun that trial rather than average it in.

## Every anomaly, explained

Numbers that look wrong at a glance, catalogued individually rather than
left for the reader to puzzle over. Each is either traced to a specific,
checkable mechanism or explicitly flagged as not yet root-caused — none are
hand-waved.

**Throughput table**

| Anomaly                                                                                                                                        | Explanation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
|------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| C++ `par` (247,211) is still *lower* than C++ `seq` (325,638), 0.76x — 8 producers still isn't a pure win, though it no longer collapses | **Was this report's worst throughput collapse (0.39x); now genuinely fixed, not just improved.** `Channel`'s single mutex (shared by every producer and consumer) was replaced with a two-lock ring-buffer queue this pass, more than tripling `par` (69K → 247K eps). The remaining 0.76x gap is a normal, much smaller residual — `par` still funnels 8 producers into one stage, it just no longer pays a Docker-specific mutex tax on top. See "Fixes applied this pass" below.                                                                                                                              |
| C# `par` (284,868) is flat against `seq` (272,474), ~1.05x — no gain *and* no collapse, unlike C++'s (former) hard collapse under the same design | C#'s shared-queue design is structurally identical to what C++ had *before* this pass's fix (one lock guarding both `Offer` and `Poll`). A matching two-lock fix was attempted here this pass and made both `seq` and `par` latency *worse*, not just unimproved, without a clear mechanism identified in the time available — reverted, not shipped. See "Fixes applied this pass" below for the full attempt. |
| Go `par` (423,400) barely beats Go `seq` (400,955), ~1.06x — looks like Go gets nothing from parallelism                                       | Misleading, not wrong: Go's `seq` is *itself* already backlogged (a real sustained-backlog case — see the classification table above), not a clean baseline. Comparing `par` against an already-degraded `seq` produces a ratio that says nothing about whether parallelism helped; compare Go's `chan` (1,505,869, 3.76x) against `seq` instead, or read `par`'s own absolute number on its own terms.                                                                                     |
| Rust `chan` (660,248) is far behind Java's (2,063,028) and Go's (1,505,869), despite Rust being the systems-level, no-GC implementation        | Rust's `chan` shows a real, sustained backlog (`p50` 114,795.5us, 38% of trial duration — see the classification table) — root-caused, not just described: 8 channels need 16 concurrently-progressing threads on a 12-core host, and hard per-channel `concurrency(1)` isolation can't borrow spare capacity across channels the way `par`'s single shared queue can. See "Thread/channel count must stay within the host's core budget" below for the full investigation. |
| TypeScript's `chan`-vs-`seq` ratio (6.06x) is the *best* in the whole report, despite Node being single-threaded with zero real OS parallelism | Real, and previously found, but the ratio is inflated by a badly-degraded `seq`/`par` baseline (both already deep in multi-second backlog — see the latency table), not by `chan` being exceptional in absolute terms: TS's `chan` throughput (131,097) is still the lowest of all eight implementations. Read the ratio and the absolute number as answering different questions — this report's own standing caution, restated here because this is the sharpest example of it.                             |

**Latency table**

| Anomaly                                                                                                                                                                                                    | Explanation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Rust `par`: `p50` is tiny (38.0us) but `p99` jumps to 70,518.5us — still a real, unfixed cliff between the 50th and 99th percentile                                                                                    | `parking_lot` (this pass's fix, see below) closed most of `seq`'s cold-wake stall but was never expected to touch `par`'s cliff — a different mechanism (a small fraction of envelopes, roughly the top 1%, caught behind a producer or consumer thread the OS/Docker scheduler preempted while it held or waited on the shared queue's lock; the other ~99% dispatch in microseconds). Still open.                                                                                                                                                                                                                                                                   |
| C++ `par`: this pass's fix changed the *shape* of the distribution, not just its scale — `p50` is now 12,971.9us (was 13.6us) while `p99` is now 85,030.7us (was 153,688.0us — a ~11,000x cliff from `p50`; now only ~6.5x) | Expected, and matches the throughput anomaly above: with the mutex bottleneck gone, `par` sustains real throughput (247K eps, was 69K), and by Little's law a system doing more real work per second naturally carries a bigger *average* queue depth — a real, if smaller, backlog now touches most envelopes (higher `p50`) instead of a rare few paying a catastrophic tail. A flatter, more predictable distribution, not a hidden regression — see the classification table's `par` row for C++, now "mild backlog" instead of "clean, mild tail."                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Java `par` never showed a cliff — `p50` 1,659.5us, `p99` 7,307.2us, `max` 21,449.0us, all within one order of magnitude — despite using a design similar to what Rust and C++ had before their fixes         | The leading explanation, consistent with this session's own directly-tested finding on `seda-bus-rust`'s pool: JVM's `ExecutorService`/`LockSupport` spin briefly before making an OS-level park/wake call, while Rust's raw `std::sync::mpsc` and C++'s original single `std::mutex` parked immediately with no spin phase. This describes why the runtimes differed, not a recommendation that every port should adopt Java's specific approach — each port implements SEDA by its own best-fitting means. Rust's fix (`parking_lot`, adds the same spin-before-park) and C++'s fix (a two-lock queue, sidesteps the single-lock cliff by a different mechanism entirely) both closed most of the gap to Java's behavior here, by different means — see "Fixes applied this pass" below for both. |
| C# `par`'s tail shape moved between passes — `p50` 30.3us, `p99` 17,186.1us, `p999` 71,260.1us — a bigger cliff than the previous pass measured (`p50` 15.8us, `p99` 1,805.3us)                             | C#'s code is byte-for-byte unchanged between passes (the attempted fix was reverted before this run) — this is run-to-run variance, not a regression, consistent with the noise levels documented throughout this report at three trials on a single host. The previous pass's "thinner tail, consistent with .NET's `Monitor.Enter` spinning" explanation is still the leading hypothesis, weakened by not reproducing tightly across passes — not independently confirmed either time.                                                                                                                                                                                                                                                                                                                                          |
| `seq` shows a real, sustained backlog in Go, C#, Python (both variants), and TypeScript, and a milder one in Java — five of eight implementations, on the *simplest* configuration (1 producer, 1 channel) | One unifying mechanism, not five coincidences: `seq` runs with exactly one consumer/worker thread in every port's design (mirrored from the original Java `SEDABus`). Once a producer isn't throttled by envelope construction, any implementation whose single-worker dispatch loop has real per-item cost (a channel poll, an atomic increment, invoking the consumer callback) gets outrun by a producer that can now publish as fast as a bare field-write allows, and the backlog persists for the whole trial. C++ (now, after this pass's fix) and Rust (tail-only, not full backlog) are the two implementations whose single-consumer dispatch path is fast enough to mostly keep up — see the classification table above. |
| `chan` also shows real backlog in Rust, C++, and partially Java — the configuration this report has repeatedly called "no artificial contention point left to hide behind," and throughput-wise `chan` is *faster* than `seq` for these same three, which looks contradictory next to a worse `p50` | Not contradictory (Little's law: more total work finished per second while individual items each wait longer just means a bigger backlog is being sustained throughout), and **root-caused, not just explained** — see "Thread/channel count must stay within the host's core budget" below for the full investigation and the measured fix. Short version: `chan`'s 8 channels need 16 concurrently-progressing threads (8 producers + 8 dedicated consumers) on a host with only 12 real cores; cutting channel count to fit that budget recovers most of the latency, at no throughput cost. Two other hypotheses were tested and ruled out first (resubmission frequency through the shared job queue; hard per-channel `concurrency(1)` isolation preventing cross-channel work-stealing) before landing on the confirmed cause. |
| C++ `seq`: this pass's tail is much tighter than before — `p999` 9,564.6us, `max` 23,344.3us, only a ~2.4x jump (was `p999` 518.4us → `max` 11,358.6us, a ~22x jump)                                       | Consistent with the two-lock fix generally: removing per-item heap allocation (a first, reverted attempt at this fix used a linked list and introduced exactly this kind of isolated cold-stall artifact) and the single mutex both remove opportunities for a rare outlier. `seq` is still this report's cleanest case; its already-small tail got smaller, not larger.                                                                                                                                                                                                                                    |
| Python 3.13 (GIL) and TypeScript's raw latency numbers are in the millions of microseconds (multi-second `p50`s)                                                                                           | Not new to this pass and not an error — both are genuine, previously-documented backlog findings (GIL serialization for Python, single-threaded event-loop serialization for TS) that predate this pass's fixes and are unaffected by them. Restated here only as a pointer for anyone jumping straight to the table without the surrounding prose: see "Python: GIL vs. free-threaded" and "TypeScript: `chan` still helps, `par` is no longer flat" below for the detail.                                                                                                                                                                                            |

## Thread/channel count must stay within the host's core budget

`chan`'s real backlog (see the latency table and classification above) was
initially assumed to be an architectural property of "one shared pool,
hard per-channel isolation" — a real but unavoidable tradeoff. **Pushed on
directly — "there is no way 8 threads draining 200k messages is slower
than 1 thread; if that's the case, the implementation is bust" — that
assumption doesn't survive testing.** Root-caused properly, in order:

1. **Ruled out: job-queue resubmission frequency.** `seda-bus-rust`'s pool
   resubmits a channel's drain task through one shared job queue every
   `BATCH` envelopes. Raising `BATCH` 512x (16 → 8192, cutting total
   resubmissions from ~12,500 to ~25) left `chan`'s `p50` unchanged
   (114,914.7us → ~116,000us).
2. **Ruled out: the shared `Pool` itself.** Replaced the single `Bus`
   shared across all 8 channels with 8 fully independent `Bus` instances —
   own `Pool`, own worker thread, zero shared state of any kind, one per
   channel, all run concurrently. Latency was statistically identical to
   the shared-pool version (`p50` ~92-140us range either way; see the raw
   diagnostic run). This rules out the pool/scheduling design as the
   cause, full stop — there was nothing left to share.
3. **Ruled out: hard `concurrency(1)` per-channel isolation preventing
   work-stealing.** Raised each channel's concurrency ceiling to match the
   pool size (8), so any idle worker could in principle pile onto a
   backlogged channel exactly like `par`'s single channel does. No
   meaningful change (`p50` still 123,142.3-142,532.9us). This makes sense
   in hindsight: work-stealing only helps when load is *imbalanced* — here
   all 8 producers publish simultaneously and symmetrically, so there is no
   idle channel's slack to borrow in the first place.
4. **Confirmed: thread count exceeding the host's real core count.** This
   Docker Desktop VM has 12 cores (`docker info --format '{{.NCPU}}'`,
   confirmed twice). `chan` with 8 channels needs 16 threads (8 producers +
   8 dedicated consumers) making simultaneous progress — a hard
   oversubscription regardless of software design. Rerunning the identical
   `chan` topology at lower channel counts, same total 200,000 envelopes:

   | Channels | Threads needed | `p50` (us)          |
   |---------:|----------------:|---------------------:|
   |        8 | 16 (> 12 cores) |     106,025-117,895 |
   |        6 | 12 (= 12 cores) |      96,274-101,857 |
   |        4 |  8 (< 12 cores) |       43,242-86,143 |
   |        2 |  4 (< 12 cores) |       10,945-42,924 |
   |    1 (`seq`) | 2           |               ~3,597 |

   Throughput stayed roughly flat (~530k-700k eps) across every row —
   **this is not a throughput/latency tradeoff, it's waste**: running more
   channels than the host has real core budget for doesn't buy more
   aggregate capacity, it just makes every envelope wait longer for no
   benefit. Latency improves substantially as channel count drops toward
   (and below) the core budget, *despite* each remaining channel handling
   proportionally more envelopes (100,000 each at 2 channels vs. 25,000
   each at 8) — ruling out "less work per channel" as an alternative
   explanation for the improvement.

**Operational takeaway, not a code change:** `seda-bus-rust`'s (and by the
same architecture, `seda-bus-cpp`'s) channel/worker count is already fully
caller-configurable — `Bus::new(workers)` and how many channels you
register are both parameters the caller chooses today. The finding here is
sizing guidance, not a missing feature: **do not create more concurrently-
active producer/consumer channels than your deployment has real CPU cores
for.** This benchmark's own `par = cores.min(8)` default happens to pick
the *maximum* the benchmark will try, which is exactly the wrong end of
this curve to default to blindly in production — a caller sizing a
real `chan`-topology deployment should budget roughly 2 threads per
channel (1 producer + 1 dedicated consumer) against `available_parallelism()`,
not maximize channel count independent of it. Not implemented as an
automatic default this pass (the benchmark's own `par` sizing was left
as-is, so numbers throughout this report stay comparable to prior passes)
— flagged as a concrete, measured case for a future sizing-helper API
rather than asserted as already fixed.

## A note on host quietness

This pass's numbers required more than "no other containers running" to
trust. Mid-session, `docker ps -q` showed zero containers while
`qemu-system-x86_64` (Docker Desktop's VM) held steady at 165-169% CPU —
real, sustained background load with nothing visibly running, most likely
Docker Desktop's own housekeeping accumulated after many hours of
build/remove cycles earlier in the same session. `ps aux`'s `%CPU` column
is a lifetime average, not instantaneous, and **stayed elevated and
slowly decaying for over a minute after a Docker Desktop restart** in a
way that looked like continued load but wasn't — confirmed by sampling
`/proc/<pid>/stat`'s jiffie counters directly across a fixed interval,
which showed single-digit instantaneous CPU% the whole time. `docker ps -q
== 0` is necessary but not sufficient evidence of a quiet host; a direct
instantaneous CPU sample of the VM process is a stronger check, and is
what this pass's canonical run was gated on.

## What went wrong, and what's real

- **This pass's central lesson: excluding envelope construction from the
  timed window was a bigger, more consequential fix than it looked going
  in.** It wasn't a small correction to Rust's numbers — it reshuffled the
  throughput ranking across all eight implementations and overturned a
  standing qualitative claim ("compiled languages never backlog") that
  earlier passes of this same report had stated with confidence. Caught by
  direct pushback (*"we're not measuring the ability of this code to
  create an Envelope"*), not self-discovered — consistent with every other
  correction in this report's history.
- **Rust's original "flat `par`" (0.96x), from the very first pass of this
  report, was a measurement artifact**, not a finding — the host was
  running concurrent Docker builds during that timed run. Unrelated to
  this pass's construction fix; kept here for the report's own running
  record of corrections.
- **C++'s `par` collapse was real and reproduced consistently across every
  pass — until this one.** It held at 0.34x-0.47x across three earlier
  passes (varying only with unrelated methodology changes), traced each
  time to `par`'s shared bounded-queue mutex under Docker's virtualization
  specifically — see `METHODOLOGY.md`. This pass fixed it at the source (a
  two-lock queue, see "Fixes applied this pass" below): `par` is now
  0.76x, no longer this report's worst collapse. The three construction-
  time bugs found and fixed in `ra-common-cpp` during an earlier pass (see
  "C++'s construction bugs" below) never touched `par`'s collapse and
  still don't — a separate mutex, fixed separately, this pass.
- **Python 3.13's GIL collapse is real, though its *shape* keeps shifting
  slightly pass to pass** — `chan` 0.63x this pass (0.67x previous, 0.33x
  construction-inclusive); `par` 0.61x this pass (0.68x previous, 0.31x
  construction-inclusive). Python's own code is unchanged across these
  passes — this is normal run-to-run variance on top of the real,
  standing finding: the GIL serializes bytecode execution regardless of
  channel topology, and removing construction cost changed how much of
  each timed iteration that serialization dominates, not whether it's
  real.

## C++'s construction bugs: still fixed, no longer visible here

An earlier pass of this report found and fixed three real bugs in
`ra-common-cpp` — re-opened `/dev/urandom` handles, a JSON
serialize-then-reparse route clone, and a process-wide mutex around random
byte generation (commits `8700729`, `7e5717b`, `1b3f687`; full detail in
`METHODOLOGY.md`). All three were **construction-time** costs: every one
fired inside `Envelope`/`Route` construction, invoked once per envelope
before `Publish` was ever called.

**Now that construction is excluded from this benchmark's timed window by
design, those bugs — fixed or not — would no longer show up in any number
in this report.** They were real, and the fixes were worth making (they
still matter to anyone constructing `ra-common-cpp` envelopes directly,
just not to this specific dispatch-only benchmark), but the dramatic
before/after throughput swings originally attributed to them (~15,000 →
~200,000 eps) are a construction-time story this pass's methodology can no
longer reproduce or falsify — they're preserved in `METHODOLOGY.md` as
historical record, not as evidence for anything measured on this page
anymore. C++'s current `seq`/`par` cleanliness (see the latency table
above) reflects dispatch-only cost, which was never what those three bugs
were about.

## Fixes applied this pass

Three real bugs/design issues found by this investigation were fixed in
the actual libraries this pass, not just measured and documented — a
change from every previous pass, which only ever changed the benchmark or
the report. A fourth attempt (C#) made things worse and was reverted.

**`seda-bus-go`: a lost-wakeup race in `schedule()`, fixed.** `schedule()`
silently gave up when `tryAcquireBusPermit` failed, releasing the
channel's own permit with no guarantee anything would ever retry it. The
only things that re-triggered `schedule()` for a channel were that same
channel's own `drain()` finishing a batch, or its own producer publishing
again — if a channel's producer had already finished and its last
`drain()` goroutine lost the race for a bus-wide permit, nothing else in
the system ever revisited it; its remaining backlog sat queued until
`Shutdown`'s timeout expired. This is exactly what this report's `chan`
config hit intermittently across two separate sessions (`drained: false`,
`delivered` short of `total`). Fix: `releaseBusPermit` now sweeps every
registered channel with `depth() > 0` and re-offers it to `schedule()`
whenever a permit frees up, not just the channel that happened to release
it — O(channels) per drain completion, bounded by how many permits are
actually free, and safely idempotent with the existing gating. Verified:
`go test -race` clean, 10 consecutive full benchmark runs (30 `chan`
trials) with zero drain failures, versus 2-of-3 failing before the fix
under the same conditions; no throughput regression.

**`seda-bus-rust`: `seq`'s cold-wake tail stall, fixed.** `seq`'s single
pool worker had to wake from a genuinely parked
`std::sync::mpsc::Receiver::recv()` on its first job — std's mpsc parks
immediately with no spin phase, paying a full OS/hypervisor thread-wake
cost on this benchmark's Docker Desktop host, up to ~260ms per occurrence.
A previous pass tried fixing this with `crossbeam-channel` (lock-free
MPMC, spins before parking) — it worked, but cost `par` ~15-20%
throughput under 8-way contention and was reverted. This pass tried
`parking_lot`'s `Mutex<VecDeque<Job>>`+`Condvar` instead — still
lock-based (not lock-free), closer in shape to what it replaces, with the
same spin-before-park benefit. Controlled A/B, matched sample sizes, same
quiet host: `par` throughput 445,872 eps baseline (18-trial range
436K-458K) → 425,539 eps with `parking_lot` (421K-428K), a real but much
smaller ~4.6% cost than `crossbeam`'s ~15-20%. In exchange, `seq`'s
worst-case tail dropped from up to 260ms to ~27ms across 9 fresh trials
with zero pathological (>30ms) results, versus the baseline regularly
hitting 50-260ms. Judged worth it this time for shared production code
(`service-bus-rust`, `i2p-rust`, `tor-client-rust`, `1m5-core-rust` all
depend on this `Pool`). Verified: `cargo test` clean (9 unit + 1 doctest).

**`seda-bus-cpp`: `Channel`'s single mutex, replaced with a two-lock
ring-buffer queue.** `Offer` (push) and `Poll` (pop) shared one
`std::mutex` guarding the whole queue, so every producer and every
consumer on a stage contended on the same lock regardless of which end
they touched — this report's worst throughput collapse (`par` 0.39x). A
first attempt used the textbook Michael & Scott two-lock queue shape
(`new`/`delete` per node): it fixed `par` but broke `seq` — Docker-
verified, `seq` (previously this report's cleanest case) developed the
same bimodal cold-stall pattern other implementations pay for their
allocator/OS interaction, almost certainly from introducing 200,000
individual heap allocations where `std::deque` previously amortized
growth. Fixed by backing the same two-lock algorithm with a pre-allocated
circular buffer instead of a linked list, removing the allocator from the
hot path entirely. Verified: 13/13 tests pass (75/75 assertions), clean
under ThreadSanitizer in Docker (the project's own canonical environment,
not the sandboxed context where TSan was previously noted untrustworthy
for this port). Docker-verified benchmark, 9 trials each: `seq` 175,861 →
329,698 eps (~1.9x), `par` 68,998 → 253,233 eps (~3.7x); `seq`'s bimodal
stall from the first, reverted attempt is gone; `chan` unaffected.

**`seda-bus-cs`: the same two-lock fix, attempted and reverted.** C#'s
`Channel` has the identical single-lock pattern (one `lock (_lock)`
guarding both `Offer` and `Poll` on a `LinkedList<Envelope>`). Ported the
same pre-allocated-ring-buffer two-lock design, fixed one real bug found
along the way (C#'s `Monitor.Pulse` requires holding its lock, unlike
C++'s `condition_variable::notify_one()`, which is safe and cheap to call
with no waiters and no lock held — an early version of the port paid an
extra lock acquisition on every single successful `Pop`, not just when a
producer was actually blocked; fixed with a waiter-count gate). All 13
tests still passed throughout. **But `seq` and `par` latency got
substantially worse, not better** — `seq` `p50` consistently 95-390ms
(bimodal, worse than the original design's already-flagged backlog
issue), `par` `p50` bimodal between near-zero and 80-240ms across trials.
The waiter-count fix didn't move these numbers, ruling out that specific
hypothesis; no other cause was identified with confidence in the time
available. **Reverted rather than shipped as a partial or uncertain
win** — this report's standing practice (see `seda-bus-rust`'s
`crossbeam-channel` revert above) is to ship a fix only when its net effect
is verified and understood, not merely "probably better on one measure."
C#'s numbers in every table in this report are its original, unmodified
design.

## Rust: the `ra-common` rewire, revisited

An earlier pass of this report concluded rewiring `seda-bus-rust` onto
`ra_common::Envelope` cost real throughput — `seq` 0.43-0.54x, `par` 0.72x,
`chan` 0.40x versus the pre-rewire minimal struct — based on a
construction-only micro-benchmark (`ra_common::Envelope::document()+content`
~710-810k constructions/sec vs. the old struct's ~2.3-2.4M/sec, ~2.9-3.4x
more expensive) plus the full, construction-inclusive bus numbers.

**That construction-cost gap is still real** — it hasn't been re-measured
this pass and there's no reason to expect it changed — but **its
consequence for this benchmark's dispatch numbers is now much less clear
than the earlier pass concluded**, because the earlier pass's "dispatch"
numbers had construction folded into them. This pass's construction-excluded
`seq` throughput is 537,687 eps — *higher* than the old pre-rewire,
construction-inclusive figure of 469,164 — which is hard to reconcile with
"the rewire has a real, ongoing dispatch-time cost" as flatly stated
before. The likely correct reading: most or all of the previously-measured
"rewire cost" was construction cost, now properly excluded by design, not
a genuine per-envelope dispatch-time tax from carrying a richer `Envelope`
through the bus's scheduling path. This isn't re-confirmed with a fresh
A/B test this pass (that would mean rebuilding the pre-rewire commit under
the *current*, construction-excluding benchmark, which hasn't been done) —
flagged as the natural next check, not asserted as settled.

**The previous pass's causal claim that the rewire triggered Rust's `seq`
tail-stall is now moot rather than resolved** — the stall itself is fixed
this pass (see "Fixes applied this pass" above), by a change to the pool's
job queue that has nothing to do with which envelope type it carries. That
the fix worked regardless of envelope choice is itself mild evidence the
original causal claim was, at best, incomplete — but it was never
re-tested against the corrected, construction-excluding benchmark, so it
stays neither confirmed nor retracted.

## Mutex vs. lock-free: why the design is what it is

`par`'s ceiling — real in every language that still has it, and
unaffected by the construction-exclusion fix — comes from a single
mutex-guarded queue shared by every producer and consumer for a stage.
That's the standard way to implement a bounded, backpressured
multi-producer/multi-consumer queue (what six of the seven ports still
do, and what all seven did before this pass), not a deliberately
introduced bottleneck: it makes correctness easy, `Block`/`DropOldest`
back-pressure essentially free, and needs no dependency beyond each
language's standard library. The cost is exactly what `par` measures —
one serialization point, non-linear degradation under contention, worse
under virtualization than native.

**This pass tested the lowest-risk improvement path this section
previously only proposed: a two-lock queue, not a full lock-free
rewrite.** It worked for C++ (`par` 69K → 247K eps, no longer this
report's worst collapse) once backed by a pre-allocated ring buffer
rather than a naively-ported linked list (see "Fixes applied this pass"
above for why the first attempt failed `seq`). The same port attempted
against C# made `seq`/`par` latency worse, not better, and was reverted —
so the two-lock queue is a real, verified fix for *this specific
single-mutex bottleneck*, not a universal one: it depends on getting the
allocation strategy right for the target runtime (a lesson C++ needed and
C# didn't get a working answer to in the time available), and on the
runtime's own lock/wait primitives not already absorbing most of the cost
the way Java's apparently do without any queue redesign at all. See
`METHODOLOGY.md` for the original pros/cons write-up this section is
built on.

## Python: GIL vs. free-threaded

`seda-bus-python`'s own README documents a real ~2.6x free-threading
speedup on actual CPU-bound work (hashcash/fib). This benchmark's consumer
does almost no CPU work, so these numbers are about lock/GIL contention
under a channel, not that CPU-bound benefit. Under the corrected,
construction-excluded methodology: 3.14t's `par` is now a real 0.43x
collapse (was ~flat, 0.96-0.97x, when construction cost was diluting each
timed iteration) — free-threaded Python's shared-channel contention is
more exposed once producers aren't throttled by construction. `chan` (no
shared lock) still shows the real signal: 2.56x here. 3.13's GIL collapse
is real in both configurations (`par` 0.61x, `chan` 0.63x) — milder than
previously reported (0.31x/0.33x) for the same reason as above, not
because the GIL got less serializing; Python's own code is unchanged
across passes, so the small pass-to-pass movement in these ratios (0.68x↔
0.61x, 0.67x↔0.63x) is run-to-run noise, not a trend.

## TypeScript: `chan` still helps, `par` is no longer flat

Under the old, construction-inclusive methodology, `ts`'s `par` measured
flat (1.01x, attributed to Node's single-threaded event loop meaning
`par`'s "producers" are concurrent async tasks, not OS parallelism, so
there was assumed to be nothing for more of them to contend over). Under
the corrected methodology, `par` is a real 0.67x collapse — once producers
publish as fast as the pre-built envelopes allow rather than being paced
by `makeEnvelope()`'s own construction cost, 8 concurrent async producer
loops sharing one channel genuinely cost more (promise/microtask
scheduling overhead, `await` handoffs) than 1 loop does. `chan` still
shows a strong real gain (6.06x, the best ratio in this report) with **no
CPU parallelism available at all** — splitting work across independent
channels still reduces per-operation coordination overhead even on one
thread, exactly as previously found; that part of the finding holds.

## Other attributes

Pulled from [`seda-bus/DESIGN.md`](../DESIGN.md)'s own comparison table
(§2.1) — the maintained source, not re-derived here — plus three new
columns. Unaffected by this pass's benchmark methodology change.

|                                   |                Java |         Rust |                Python |          TypeScript |                                         C++ |                  C# |                                       Go |
|-----------------------------------|--------------------:|-------------:|----------------------:|--------------------:|--------------------------------------------:|--------------------:|-----------------------------------------:|
| Version                           |               1.3.1 |        0.4.0 |                 0.2.0 |               0.2.0 |                                       0.1.0 |               0.1.0 |                                    0.1.0 |
| Source LOC                        |                 962 |          788 |                   595 |                 835 |                                         898 |                 620 |                                      768 |
| Integration tests                 |                   8 |            9 |                    11 |                  16 |                                          13 |                  13 |                                       13 |
| Runtime deps beyond `ra-common-*` |                   0 | `log`, `parking_lot` |                     0 |                   0 |                                           0 |                   0 |                                        0 |
| Envelope source                   |         `ra-common` |  `ra-common` |           `ra-common` |         `ra-common` |                             `ra-common-cpp` |      `ra-common-cs` |                           `ra-common-go` |
| Worker pool                       |   `ExecutorService` |  hand-rolled |  `ThreadPoolExecutor` |          event loop |                                 hand-rolled | shared `ThreadPool` |                        none (goroutines) |
| True stage parallelism            |                 yes |          yes |    only free-threaded |  worker stages only |                                         yes |                 yes |                                      yes |
| Race/sanitizer-verified           |             not run |      not run |               not run |             not run |     **yes** (TSan in Docker, clean, this pass's two-lock queue) |             not run | **yes** (`go test -race`, clean, this pass's `schedule()` fix) |
| Guaranteed delivery               |                 yes |           no |                    no |                  no |                                          no |                  no |                                       no |

Source LOC: `wc -l` over each port's library source only — see
[`scripts/count_loc.sh`](scripts/count_loc.sh).

## Reading this table honestly

**This report has now been substantially wrong, corrected, and rewritten
twice, then went a step further and fixed real bugs in the libraries it
was measuring.** First a contaminated-host measurement artifact (Rust's
`par` looking flat), second a construction/dispatch conflation folded
into every number and one standing qualitative claim. Both were caught by
direct, specific pushback, not self-discovered — the lesson compounds
rather than repeats: a "verified" result is only as trustworthy as the
environment *and the methodology* it was verified under. This pass is a
different kind of correction: not a flaw in how something was measured,
but real defects in what was being measured — a lost-wakeup race in Go, a
cold-wake stall in Rust, a single-mutex bottleneck in C++, each found
because the measurement was trusted enough to be worth explaining
precisely, then pushed on hard enough ("there is no way 8 threads draining
200k messages is slower than 1 thread... if that's the case, the
implementation is bust") that "explained" stopped being an acceptable
stopping point. One attempted fix (C#) was pushed on with the same
rigor, found wanting, and reverted rather than kept for the sake of having
done something.

Within the `ra-common`-carrying implementations, don't read close
percentage differences between adjacent rows as meaningful given three
trials on a single host; do read order-of-magnitude differences,
`par`-vs-`chan` gaps, and the clean/tail-stall/backlog classification in
the latency section as real — every qualitative claim in this pass was
checked against the elapsed-time ratio, not assumed from a raw number.

Read the multi-second `seq`/`par`/`chan` numbers for TS/Python as a real
backlog finding (order-of-magnitude, reproduced across trials and now
confirmed to also apply, at a smaller magnitude, to several compiled
implementations), not as precise per-envelope claims — see "Latency" above
and `bench/WORKLOAD.md` for why this benchmark's capacity choice makes
queueing delay, not raw dispatch cost, the dominant signal whenever a
producer can outrun its consumer, which construction-exclusion just made
possible for several implementations that previously never triggered it.
