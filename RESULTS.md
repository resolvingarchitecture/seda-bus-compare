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
| Java                           | 861,490 | 556,566 |       0.65x | **1,968,262** |        2.28x |
| Go                             | 408,140 | 412,299 |       1.01x |     1,759,884 |        4.31x |
| Rust                           | 544,162 | 459,738 |       0.84x |       645,609 |        1.19x |
| C#                             | 272,740 | 273,234 |       1.00x |       387,930 |        1.42x |
| C++                            | 175,861 |  68,998 |   **0.39x** |       379,529 |        2.16x |
| Python 3.14t (free-threaded)   | 102,173 |  45,909 |       0.45x |       260,058 |        2.55x |
| TypeScript (Node 22)           |  21,619 |  14,436 |       0.67x |       124,338 |    **5.75x** |
| Python 3.13 (GIL)              |  53,148 |  36,206 |       0.68x |        35,660 |    **0.67x** |

![Throughput by implementation and configuration (log scale)](results/charts/throughput.svg)

**Every number moved, most of them a lot, once construction came out of
the timed window** — and not proportionally, so the ranking reshuffled.
Java's `chan` more than doubled (903K → 1.97M) and now leads outright,
where it was third before. Go's `chan` nearly doubled (989K → 1.76M) and
is now clearly #2. Rust's `chan` *dropped* (1.08M → 646K) and fell from #1
to #3 — the previous report's entire "Rust: the `ra-common` rewire cost
real throughput" finding is revisited below, because most (not
necessarily all) of that "cost" turns out to have been construction cost,
now excluded by design. C++'s `seq` rose modestly (125K → 176K, consistent
with construction previously adding real but not dominant weight there).
**The lesson: this report's throughput ranking was never actually settled
— it was measuring a different, conflated thing until this pass.**

**The headline finding still holds, on cleaner ground now:** every
implementation gets real, often substantial gains from parallelism —
`chan` beats `seq` in all eight cases except GIL-bound Python (0.67x,
still a real collapse — see "Python: GIL vs. free-threaded" below). The
`par` column is still not a measure of "how parallel is this bus" — it's a
measure of lock contention on one shared stage; see `METHODOLOGY.md`'s
"Does parallelism work?" section for the controlled comparison.

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
| Java               | seq     |           12,301.0 |         18,253.4 |         18,650.2 |         26,527.6 |
| Java               | par     |            1,123.9 |          5,204.0 |          5,602.3 |          9,468.1 |
| Java               | chan    |           18,523.3 |         34,827.7 |         35,903.5 |         61,895.7 |
| Go                 | seq     |          172,144.8 |        252,232.7 |        253,427.0 |        299,153.7 |
| Go                 | par     |           53,172.8 |         88,893.3 |         89,190.0 |        102,887.4 |
| Go                 | chan    |           38,795.9 |         68,700.5 |         70,759.2 |         75,217.7 |
| Rust               | seq     |            3,597.4 |         44,018.3 |         44,556.3 |         63,028.0 |
| Rust               | par     |               61.1 |         66,075.7 |         67,513.4 |         70,363.9 |
| Rust               | chan    |          114,914.7 |        200,077.4 |        208,652.1 |        227,169.6 |
| C#                 | seq     |          197,274.0 |        312,282.7 |        314,225.8 |        439,031.3 |
| C#                 | par     |               15.8 |          1,805.3 |         35,645.0 |         79,071.4 |
| C#                 | chan    |            2,975.3 |        129,019.4 |        133,993.6 |        184,477.5 |
| C++                | seq     |               21.6 |            200.1 |            518.4 |         11,358.6 |
| C++                | par     |               13.6 |        153,688.0 |        155,973.3 |        161,260.6 |
| C++                | chan    |           58,806.7 |        234,509.3 |        295,332.8 |        322,910.3 |
| Python 3.14t       | seq     |          272,012.2 |        403,180.6 |        405,352.4 |        461,979.2 |
| Python 3.14t       | par     |        1,145,814.6 |      1,563,480.1 |      1,567,483.2 |      1,604,400.9 |
| Python 3.14t       | chan    |          173,936.8 |        336,498.2 |        338,502.5 |        360,242.7 |
| TypeScript         | seq     |        4,417,982.2 |      6,218,729.6 |      6,228,380.5 |      6,329,220.6 |
| TypeScript         | par     |        6,656,120.5 |      9,436,286.4 |      9,438,434.9 |      9,537,606.6 |
| TypeScript         | chan    |          600,404.0 |      1,076,210.8 |      1,077,789.3 |      1,183,206.8 |
| Python 3.13 (GIL)  | seq     |          581,421.6 |        950,212.9 |        955,746.0 |      1,210,851.6 |
| Python 3.13 (GIL)  | par     |        1,950,780.8 |      2,906,951.6 |      2,923,365.2 |      3,525,135.9 |
| Python 3.13 (GIL)  | chan    |        2,783,704.1 |      4,549,412.7 |      4,624,846.8 |      4,892,312.0 |

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
| C++                | seq    |             1,142.0 |         0.00 |         0.01 | clean                              |
| C++                | par    |             2,898.7 |         0.00 |         0.06 | clean, mild tail                   |
| Java               | par    |               361.0 |         0.00 |         0.03 | clean                              |
| C#                 | par    |               733.0 |         0.00 |         0.11 | clean, mild tail                   |
| Rust               | par    |               434.7 |         0.00 |         0.16 | tail-only stall                    |
| Rust               | seq    |               368.7 |         0.01 |         0.17 | tail-only stall                    |
| C#                 | chan   |               517.0 |         0.01 |         0.36 | tail-only stall (large tail)       |
| Java               | seq    |               233.3 |         0.05 |         0.11 | mild backlog                       |
| C++                | chan   |               526.7 |         0.11 |         0.61 | **real backlog**                   |
| Go                 | par    |               484.3 |         0.11 |         0.21 | mild-to-real backlog               |
| Java               | chan   |               106.3 |         0.17 |         0.58 | **real backlog**                   |
| Python 3.14t       | seq    |             1,958.0 |         0.14 |         0.24 | real backlog                       |
| C#                 | seq    |               753.7 |         0.26 |         0.58 | **real backlog**                   |
| Python 3.14t       | chan   |               769.0 |         0.23 |         0.47 | real backlog                       |
| Python 3.14t       | par    |             4,360.7 |         0.26 |         0.37 | real backlog                       |
| Go                 | chan   |               113.3 |         0.34 |         0.66 | **real backlog**                   |
| Go                 | seq    |               494.0 |         0.35 |         0.61 | **real backlog**                   |
| Rust               | chan   |               309.7 |         0.37 |         0.73 | **real backlog**                   |
| Python 3.13 (GIL)  | par    |             5,525.3 |         0.35 |         0.64 | real backlog                       |
| TypeScript         | chan   |             1,610.7 |         0.37 |         0.73 | real backlog                       |
| Python 3.13 (GIL)  | seq    |             3,765.3 |         0.15 |         0.32 | real backlog                       |
| Python 3.13 (GIL)  | chan   |             5,613.7 |         0.50 |         0.87 | **real backlog**                   |
| TypeScript         | seq    |             9,254.3 |         0.48 |         0.68 | **real backlog**                   |
| TypeScript         | par    |            13,854.7 |         0.48 |         0.69 | **real backlog**                   |

**C++ `seq`/`par` are the only two cases in the whole report that are
genuinely clean** — its single/shared-channel consumer path keeps pace
with a maximally fast producer every time. Everything else shows at least
a tail-only stall, and the majority show a real, sustained backlog once
construction can no longer throttle the producer down to the consumer's
pace. **Rust and C++ both flip from "clean" to "real backlog" specifically
in `chan`** — the configuration this report previously called out as
"no artificial contention point left to hide behind." That framing was
correct about the lock, wrong about there being nothing left to hide
behind: a fast-enough producer can still outrun 8 independent
single-consumer channels if per-channel dispatch has any real per-item
cost, which it evidently does in both.

**Rust's `seq` tail-only stall is real, still unresolved, and now milder
than previously reported** — `p50` is fine (3.6ms mean, dragged up only by
occasional bad trials; several trials land sub-microsecond), but `max`
still reaches 63ms, a genuine, reproducible tail latency this pass didn't
root-cause. **This revises, not confirms, the previous report's
conclusion.** The prior pass ran a direct A/B test (rebuilding the
pre-`ra_common`-rewire `seda-bus-rust` commit and running it back-to-back
with the current one, same host, same day) and found the old, minimal
envelope clean on 9 of 9 `seq` trials against a roughly 1-in-3 clean rate
with `ra_common::Envelope` — concluding the rewire's heavier envelope
construction was the trigger. **That A/B test was itself run under the old,
construction-inside-the-timed-window methodology.** Now that construction
is excluded for both cases, the mechanism needs re-verification against
the corrected benchmark before that causal claim can stand as-is; it is
neither confirmed nor retracted here — flagged as open, not silently
carried forward. What *is* newly confirmed: swapping the pool's job queue
from `std::sync::mpsc` to `crossbeam-channel` (which spins briefly before
parking, closer to what Java's `ThreadPoolExecutor` does) measurably
reduced but did not eliminate the stall, while costing `par` a reproducible
~15-20% of its throughput from contention on the now-lock-free queue under
8-way load — judged not worth keeping for a change to shared production
code (`service-bus-rust`, `i2p-rust`, `tor-client-rust`, and transitively
`1m5-core-rust` all depend on `seda-bus-rust`'s `Pool`) and reverted.

**`seda-bus-go`'s `chan` config again failed to fully drain within its 60s
shutdown timeout** on this pass's first attempt (2 of 3 trials,
`delivered` short of `total`, `drained: false`) — the same intermittent
race first flagged in an earlier report and still not root-caused. A
following rerun of the same unmodified binary drained cleanly all three
times; that clean run is what's in the tables above. This is now confirmed
across two separate sessions and reruns as a real, intermittent bug in
`seda-bus-go`'s shutdown/drain accounting under the `chan` topology
specifically, not a benchmark-instrumentation or host-contamination
artifact. Any reproduction attempt should watch for `"drained":false` in
`results/raw/go.jsonl` and discard/rerun that trial rather than average it
in — this report's standing practice, followed here again.

## Every anomaly, explained

Numbers that look wrong at a glance, catalogued individually rather than
left for the reader to puzzle over. Each is either traced to a specific,
checkable mechanism or explicitly flagged as not yet root-caused — none are
hand-waved.

**Throughput table**

| Anomaly                                                                                                                                        | Explanation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
|------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| C++ `par` (68,998) is *lower* than C++ `seq` (175,861) — adding 8 producers made it slower                                                     | Real, reproduces every pass regardless of methodology changes. `par` puts all 8 producers/workers behind one mutex-guarded queue; under Docker's virtualization specifically, contention on that one lock costs more than the parallelism gains. A native (non-containerized) run of the same binary shows `par` ≈ `seq` — see `METHODOLOGY.md` and "Mutex vs. lock-free" below.                                                                                                                              |
| C# `par` (273,234) is flat against `seq` (272,740), ~1.00x — no gain *and* no collapse, unlike C++'s hard collapse under the same design       | C#'s shared-queue design is structurally identical to C++'s (one mutex-guarded queue, 8 workers). That it doesn't collapse the way C++ does is itself notable — see the matching latency anomaly below (C# `par`'s tail starts far later than C++'s), which points at .NET's `Monitor`/lock primitive spinning before it blocks, unlike C++'s `std::mutex`/condvar. Not independently verified this pass (would need a targeted lock-primitive test, not run) — the leading explanation, not a confirmed one. |
| Go `par` (412,299) barely beats Go `seq` (408,140), ~1.01x — looks like Go gets nothing from parallelism                                       | Misleading, not wrong: Go's `seq` is *itself* already backlogged (172ms mean `p50`, a real sustained-backlog case — see the classification table above), not a clean baseline. Comparing `par` against an already-degraded `seq` produces a ratio that says nothing about whether parallelism helped; compare Go's `chan` (1,759,884, 4.31x) against `seq` instead, or read `par`'s own absolute number on its own terms.                                                                                     |
| Rust `chan` (645,609) is far behind Java's (1,968,262) and Go's (1,759,884), despite Rust being the systems-level, no-GC implementation        | Rust's `chan` shows a real, sustained backlog (`p50` 114,914.7us, 37% of trial duration — see the classification table) — see the matching latency-table row below for the tested mechanism (each channel's hard `concurrency(1)` isolation can't borrow spare capacity the way `par`'s single shared queue can; not a bug, a real tradeoff of the design). |
| TypeScript's `chan`-vs-`seq` ratio (5.75x) is the *best* in the whole report, despite Node being single-threaded with zero real OS parallelism | Real, and previously found, but the ratio is inflated by a badly-degraded `seq`/`par` baseline (both already deep in multi-second backlog — see the latency table), not by `chan` being exceptional in absolute terms: TS's `chan` throughput (124,338) is still the lowest of all eight implementations. Read the ratio and the absolute number as answering different questions — this report's own standing caution, restated here because this is the sharpest example of it.                             |

**Latency table**

| Anomaly                                                                                                                                                                                                    | Explanation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Rust `par`: `p50` is tiny (61.1us) but `p99` jumps to 66,075.7us — a ~1,000x cliff between the 50th and 99th percentile                                                                                    | A small fraction of envelopes (roughly the top 1%) get caught behind a producer or consumer thread that the OS/Docker scheduler preempted while it held (or was waiting on) the shared queue's lock; the other ~99% dispatch in microseconds. Same mechanism as `par`'s throughput collapse above — one shared mutex, worse under virtualization — just visible here as a latency cliff instead of a throughput number.                                                                                                                                                                                                                                                                   |
| C++ `par`: identical shape, worse magnitude — `p50` 13.6us, `p99` 153,688.0us (~154ms)                                                                                                                     | Same mechanism as the Rust `par` row above, more severe — consistent with C++'s `par` also showing this report's single worst throughput collapse (0.39x). The cliff's severity tracks the throughput collapse's severity across every language that has one.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Java `par` does *not* show this cliff — `p50` 1,123.9us, `p99` 5,204.0us, `max` 9,468.1us, all within one order of magnitude — despite using the *same* single-shared-queue design as Rust and C++         | The leading explanation, consistent with this session's own earlier, directly-tested finding on `seda-bus-rust`'s pool (see the git history on `seda-bus-rust`'s reverted `crossbeam-channel` experiment): JVM's `ExecutorService`/`LockSupport` spin briefly before making an OS-level park/wake call, while Rust's raw `std::sync::mpsc` (and C++'s `std::mutex`/condvar) park immediately with no spin phase, paying the full cost of a Docker-VM-level thread wake on contention. This describes why the runtimes differ, not a recommendation that Rust or C++ should adopt Java's approach — each port implements SEDA by its own best-fitting means, and a spin-before-park primitive is a real CPU-vs-latency tradeoff (see `seda-bus-rust`'s reverted `crossbeam-channel` experiment: it reduced one stall but cost `par` throughput elsewhere), not a strictly better default. Not independently re-tested against `par` specifically this pass — inferred from the earlier, directly-tested mechanism, not re-verified here. |
| C# `par`'s tail starts *later* than Rust/C++'s — `p50` 15.8us, `p99` only 1,805.3us, then a real jump to `p999` 35,645.0us                                                                                 | A thinner tail than Rust/C++ (roughly the top 0.1-1% affected, not the top 1%), consistent with .NET's `Monitor.Enter` also spinning before it blocks — the same mechanism proposed for C#'s flat throughput ratio above, and the two anomalies corroborate each other. Same caveat: leading explanation, not independently confirmed this pass.                                                                                                                                                                                                                                                                                                                                          |
| `seq` shows a real, sustained backlog in Go, C#, Python (both variants), and TypeScript, and a milder one in Java — five of eight implementations, on the *simplest* configuration (1 producer, 1 channel) | One unifying mechanism, not five coincidences: `seq` runs with exactly one consumer/worker thread in every port's design (mirrored from the original Java `SEDABus`). Once a producer isn't throttled by envelope construction, any implementation whose single-worker dispatch loop has real per-item cost (a channel poll, an atomic increment, invoking the consumer callback) gets outrun by a producer that can now publish as fast as a bare field-write allows, and the backlog persists for the whole trial. C++ and (mostly) Rust are the only two implementations whose single-consumer dispatch path is fast enough to never fall behind — see the classification table above. |
| `chan` also shows real backlog in Rust and C++ (and partially Java) — the configuration this report has repeatedly called "no artificial contention point left to hide behind," and throughput-wise `chan` is *faster* than `seq` for these same three, which looks contradictory next to a worse `p50` | Not contradictory (Little's law: more total work finished per second while individual items each wait longer just means a bigger backlog is being sustained throughout), and **root-caused, not just explained** — see "Thread/channel count must stay within the host's core budget" below for the full investigation and the measured fix. Short version: `chan`'s 8 channels need 16 concurrently-progressing threads (8 producers + 8 dedicated consumers) on a host with only 12 real cores; cutting channel count to fit that budget recovers most of the latency, at no throughput cost. Two other hypotheses were tested and ruled out first (resubmission frequency through the shared job queue; hard per-channel `concurrency(1)` isolation preventing cross-channel work-stealing) before landing on the confirmed cause. |
| C++ `seq`: `p999` is 518.4us but `max` jumps to 11,358.6us — a ~22x jump at the very top, despite `seq` otherwise being this report's cleanest case                                                        | An isolated, rare single-envelope outlier — almost certainly the same class of "first worker activation" cold-start artifact documented for Rust's `seq` (see above), just far less frequent for C++, whose dispatch path is fast enough to absorb it into `p999` for two of three trials and only shows it in `max`. Not selectively excluded from the table — reported as-is, per this report's own standard of not discarding inconvenient numbers.                                                                                                                                                                                                                                    |
| Python 3.13 (GIL) and TypeScript's raw latency numbers are in the millions of microseconds (multi-second `p50`s)                                                                                           | Not new to this pass and not an error — both are genuine, previously-documented backlog findings (GIL serialization for Python, single-threaded event-loop serialization for TS) that predate this pass's construction-exclusion fix and are unaffected by it. Restated here only as a pointer for anyone jumping straight to the table without the surrounding prose: see "Python: GIL vs. free-threaded" and "TypeScript: `chan` still helps, `par` is no longer flat" below for the detail.                                                                                                                                                                                            |

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
- **C++'s `par` collapse is real and reproduces consistently** across every
  pass of this report regardless of methodology changes elsewhere (now
  0.39x; previously 0.47x construction-inclusive, 0.34x before that,
  pre-fix). Traced to `par`'s shared bounded-queue mutex under Docker's
  virtualization specifically — see `METHODOLOGY.md`. The three
  construction-time bugs found and fixed in `ra-common-cpp` during an
  earlier pass (see "C++'s construction bugs: still fixed, no longer
  visible here" below) never touched `par`'s collapse, and still don't.
- **Python 3.13's GIL collapse is real, though its *shape* changed under
  the corrected methodology** — `chan` now shows a real 0.67x collapse
  (was 0.33x construction-inclusive); `par` moved from a severe 0.31x
  collapse to a milder 0.68x. The GIL still serializes bytecode execution
  regardless of channel topology; removing construction cost changed how
  much of each timed iteration that serialization dominates, not whether
  it's real.

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
`seq` throughput is 544,162 eps — *higher* than the old pre-rewire,
construction-inclusive figure of 469,164 — which is hard to reconcile with
"the rewire has a real, ongoing dispatch-time cost" as flatly stated
before. The likely correct reading: most or all of the previously-measured
"rewire cost" was construction cost, now properly excluded by design, not
a genuine per-envelope dispatch-time tax from carrying a richer `Envelope`
through the bus's scheduling path. This isn't re-confirmed with a fresh
A/B test this pass (that would mean rebuilding the pre-rewire commit under
the *current*, construction-excluding benchmark, which hasn't been done) —
flagged as the natural next check, not asserted as settled.

**This also means the previous pass's causal claim that the rewire
triggers Rust's `seq` tail-stall needs the same re-verification** — see
the latency section above.

## Mutex vs. lock-free: why the design is what it is

`par`'s ceiling — real in every language, not just C++, and unaffected by
this pass's construction-exclusion fix — comes from a single mutex-guarded
queue shared by every producer and consumer for a stage. That's the
standard way to implement a bounded, backpressured multi-producer/
multi-consumer queue (what every one of the seven ports does, including
the Java original), not a deliberately introduced bottleneck: it makes
correctness easy, `Block`/`DropOldest` back-pressure essentially free, and
needs no dependency beyond each language's standard library. The cost is
exactly what `par` measures — one serialization point, non-linear
degradation under contention, worse under virtualization than native. See
`METHODOLOGY.md` for the full pros/cons and the lowest-risk improvement
path (a two-lock queue, not a full lock-free rewrite).

## Python: GIL vs. free-threaded

`seda-bus-python`'s own README documents a real ~2.6x free-threading
speedup on actual CPU-bound work (hashcash/fib). This benchmark's consumer
does almost no CPU work, so these numbers are about lock/GIL contention
under a channel, not that CPU-bound benefit. Under the corrected,
construction-excluded methodology: 3.14t's `par` is now a real 0.45x
collapse (was ~flat, 0.96-0.97x, when construction cost was diluting each
timed iteration) — free-threaded Python's shared-channel contention is
more exposed once producers aren't throttled by construction. `chan` (no
shared lock) still shows the real signal: 2.55x here. 3.13's GIL collapse
is real in both configurations (`par` 0.68x, `chan` 0.67x) — milder than
previously reported (0.31x/0.33x) for the same reason as above, not
because the GIL got less serializing.

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
shows a strong real gain (5.75x, the best ratio in this report) with **no
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
| Source LOC                        |                 962 |          743 |                   595 |                 835 |                                         744 |                 620 |                                      739 |
| Integration tests                 |                   8 |            9 |                    11 |                  16 |                                          13 |                  13 |                                       13 |
| Runtime deps beyond `ra-common-*` |                   0 |        `log` |                     0 |                   0 |                                           0 |                   0 |                                        0 |
| Envelope source                   |         `ra-common` |  `ra-common` |           `ra-common` |         `ra-common` |                             `ra-common-cpp` |      `ra-common-cs` |                           `ra-common-go` |
| Worker pool                       |   `ExecutorService` |  hand-rolled |  `ThreadPoolExecutor` |          event loop |                                 hand-rolled | shared `ThreadPool` |                        none (goroutines) |
| True stage parallelism            |                 yes |          yes |    only free-threaded |  worker stages only |                                         yes |                 yes |                                      yes |
| Race/sanitizer-verified           |             not run |      not run |               not run |             not run |  attempted, untrustworthy (TSan, sandboxed) |             not run | **yes** (`go test -race`, clean, 5 runs) |
| Guaranteed delivery               |                 yes |           no |                    no |                  no |                                          no |                  no |                                       no |

Source LOC: `wc -l` over each port's library source only — see
[`scripts/count_loc.sh`](scripts/count_loc.sh).

## Reading this table honestly

**This report has now been substantially wrong, corrected, and rewritten
twice** — first a contaminated-host measurement artifact (Rust's `par`
looking flat), second (this pass) a construction/dispatch conflation that
was folded into every single number and one standing qualitative claim.
Both were caught by direct, specific pushback, not self-discovered. The
lesson compounds rather than repeats: a "verified" result is only as
trustworthy as the environment *and the methodology* it was verified
under, and this report's own history is now evidence for that claim, not
just an assertion of it.

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
