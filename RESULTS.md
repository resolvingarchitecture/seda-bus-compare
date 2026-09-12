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

| Implementation               |     seq |     par | par vs. seq |          chan | chan vs. seq |
|-------------------------------|--------:|--------:|------------:|--------------:|-------------:|
| Java                          | 861,490 | 556,566 |       0.65x | **1,968,262** |        2.28x |
| Go                            | 408,140 | 412,299 |       1.01x |     1,759,884 |        4.31x |
| Rust                          | 544,162 | 459,738 |       0.84x |       645,609 |        1.19x |
| C#                            | 272,740 | 273,234 |       1.00x |       387,930 |        1.42x |
| C++                           | 175,861 |  68,998 |   **0.39x** |       379,529 |        2.16x |
| Python 3.14t (free-threaded)  | 102,173 |  45,909 |       0.45x |       260,058 |        2.55x |
| TypeScript (Node 22)          |  21,619 |  14,436 |       0.67x |       124,338 |    **5.75x** |
| Python 3.13 (GIL)             |  53,148 |  36,206 |       0.68x |        35,660 |    **0.67x** |

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

| Implementation    | Config | mean elapsed (ms) | p50/elapsed | max/elapsed | Read as                          |
|-------------------|--------|-------------------:|------------:|------------:|-----------------------------------|
| C++                | seq    |             1,142.0 |        0.00 |        0.01 | clean                             |
| C++                | par    |             2,898.7 |        0.00 |        0.06 | clean, mild tail                  |
| Java               | par    |               361.0 |        0.00 |        0.03 | clean                             |
| C#                 | par    |               733.0 |        0.00 |        0.11 | clean, mild tail                  |
| Rust               | par    |               434.7 |        0.00 |        0.16 | tail-only stall                   |
| Rust               | seq    |               368.7 |        0.01 |        0.17 | tail-only stall                   |
| C#                 | chan   |               517.0 |        0.01 |        0.36 | tail-only stall (large tail)      |
| Java               | seq    |               233.3 |        0.05 |        0.11 | mild backlog                      |
| C++                | chan   |               526.7 |        0.11 |        0.61 | **real backlog**                  |
| Go                 | par    |               484.3 |        0.11 |        0.21 | mild-to-real backlog              |
| Java               | chan   |               106.3 |        0.17 |        0.58 | **real backlog**                  |
| Python 3.14t       | seq    |             1,958.0 |        0.14 |        0.24 | real backlog                      |
| C#                 | seq    |               753.7 |        0.26 |        0.58 | **real backlog**                  |
| Python 3.14t       | chan   |               769.0 |        0.23 |        0.47 | real backlog                      |
| Python 3.14t       | par    |             4,360.7 |        0.26 |        0.37 | real backlog                      |
| Go                 | chan   |               113.3 |        0.34 |        0.66 | **real backlog**                  |
| Go                 | seq    |               494.0 |        0.35 |        0.61 | **real backlog**                  |
| Rust               | chan   |               309.7 |        0.37 |        0.73 | **real backlog**                  |
| Python 3.13 (GIL)  | par    |             5,525.3 |        0.35 |        0.64 | real backlog                      |
| TypeScript         | chan   |             1,610.7 |        0.37 |        0.73 | real backlog                      |
| Python 3.13 (GIL)  | seq    |             3,765.3 |        0.15 |        0.32 | real backlog                      |
| Python 3.13 (GIL)  | chan   |             5,613.7 |        0.50 |        0.87 | **real backlog**                  |
| TypeScript         | seq    |             9,254.3 |        0.48 |        0.68 | **real backlog**                  |
| TypeScript         | par    |            13,854.7 |        0.48 |        0.69 | **real backlog**                  |

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

|                                   |              Java |        Rust |               Python |         TypeScript |                                        C++ |                  C# |                                       Go |
|-----------------------------------|-------------------:|------------:|---------------------:|-------------------:|-------------------------------------------:|--------------------:|-----------------------------------------:|
| Version                           |               1.3.1 |       0.4.0 |                0.2.0 |              0.2.0 |                                      0.1.0 |               0.1.0 |                                    0.1.0 |
| Source LOC                        |                 962 |         743 |                  595 |                835 |                                        744 |                 620 |                                      739 |
| Integration tests                 |                   8 |           9 |                   11 |                 16 |                                         13 |                  13 |                                       13 |
| Runtime deps beyond `ra-common-*` |                   0 |       `log` |                    0 |                  0 |                                          0 |                   0 |                                        0 |
| Envelope source                   |         `ra-common` | `ra-common` |          `ra-common` |        `ra-common` |                            `ra-common-cpp` |      `ra-common-cs` |                           `ra-common-go` |
| Worker pool                       |  `ExecutorService` | hand-rolled | `ThreadPoolExecutor` |         event loop |                                hand-rolled | shared `ThreadPool` |                        none (goroutines) |
| True stage parallelism            |                 yes |         yes |   only free-threaded | worker stages only |                                        yes |                 yes |                                      yes |
| Race/sanitizer-verified           |             not run |     not run |              not run |            not run | attempted, untrustworthy (TSan, sandboxed) |             not run | **yes** (`go test -race`, clean, 5 runs) |
| Guaranteed delivery               |                 yes |          no |                    no |                 no |                                          no |                  no |                                       no |

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
