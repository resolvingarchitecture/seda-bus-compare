# The benchmark workload

One specification, implemented identically in all seven `seda-bus` ports.
Each `bench/<lang>/` program is a straight translation of this — same
numbers, same shape, no language-specific tuning.

Two benchmarks live here. **The capacity curve** (below) is the primary
one: a real bounded queue, a real backpressure policy, and a swept input
rate — it answers the question an adopter actually has, "what does this
bus do as load approaches and exceeds what it can sustain." **The firehose
configs** (`seq`/`par`/`chan`, further down) are a secondary,
diagnostic-only workload: capacity large enough that backpressure never
engages, all-out unthrottled publishing. That combination is useful for
isolating scheduler/dispatch overhead and finding lock-contention bugs
(it found several, this project's own history shows), but its absolute
latency numbers are not a capacity-planning signal — an unbounded queue
fed faster than its consumer drains it will show a growing backlog by
construction, in any correct implementation. Read the firehose section's
own note before drawing conclusions from it.

## The capacity curve (primary benchmark)

### What it measures

For one stage with a real, fixed capacity and `Block` backpressure: how
throughput, latency, and recovery time behave as the input rate moves from
comfortably below the stage's own measured sustained capacity, to right at
it, to past it. This is the production question a raw firehose can't
answer, because a firehose never lets backpressure engage at all.

### Setup, per producer-count configuration

Two producer-count configurations, mirroring `seq`/`par`'s contrast:

|                        | `cap1` | `cap8`                        |
|------------------------|--------|-------------------------------|
| Producer threads/tasks | 1      | `P = min(8, available cores)` |
| Channels               | 1      | 1 (shared)                    |
| Channel `Concurrency`  | 1      | `P`                           |

One channel, name `bench`, `Capacity: 1024`, `Backpressure: Block`,
`Delivery: PointToPoint`. A single consumer increments a counter and acks
`true` — same no-op consumer as the firehose configs. Publish with a
generous per-publish timeout (30s): long enough that no genuine Block wait
in this workload ever spuriously times out (worst case here is a few
seconds of queueing), short enough that a real deadlock still surfaces as a
failed run.

### Step 1 — calibrate this stage's own sustained capacity

Before sweeping load, measure what this specific stage (this capacity,
this concurrency, this language, this run) can actually sustain — don't
assume it, and don't reuse a number from a different config or a different
day's run.

- Publish as fast as possible (no pacing) for a fixed **2 second** window,
  same `Block`-backpressure channel as above.
- Run this **2 times**; `max_throughput_eps` = the **larger** of the two
  achieved throughputs (`published / elapsed_seconds`) — a conservative,
  achievable ceiling, not an average, since the sweep below treats it as
  "100%."
- Emit both calibration runs as their own output rows (`"load_fraction":
  0`, see the output contract) — they're a real, novel number in their own
  right: "sustained throughput of a real bounded, backpressured queue,"
  distinct from the firehose configs' unbounded-queue number.
- Fully drain and reset all counters (a fresh channel/bus instance is
  simplest) before moving to Step 2.
- **Pool sizing here has no target rate to derive from** (that's the whole
  point of calibrating), so use a fixed, generous guess instead — but size
  it as a TOTAL budget divided across this window's producers
  (`max(2000, total_budget / producers)`), not a flat per-producer
  constant. A flat constant sized for `cap1` (1 producer) becomes 8x too
  large for `cap8` (8 producers) and asks for the same unbounded-with-
  producer-count memory a naive sweep formula does (see Step 2's own
  pool-sizing note) - this OOM-crashed one implementation outright before
  being caught and fixed here.

### Step 2 — sweep the load

For `load_fraction` in **[0.5, 1.0, 1.5]**, **2 trials** each, on a fresh
channel/bus instance per trial:

1. `target_rate_eps = max_throughput_eps * load_fraction`, split evenly
   across the `P` producers (`target_rate_eps / P` per producer).
2. **Pre-build a pool of envelopes**, per producer, sized generously above
   what pacing is expected to need:
   `pool_size = ceil(target_rate_eps_per_producer * sweep_duration_seconds * 1.5)`
   (`sweep_duration_seconds = 4`) — same reason as the firehose configs:
   construction cost must never fall inside the timed window. **Then cap
   the TOTAL pool across all of a trial's producers combined at 400,000**
   (i.e. `min(pool_size, 400_000 / producers)` per producer) - found the
   hard way (an uncapped formula OOM-killed a benchmark process outright
   for `cap8` at `1.5x` load: 8 producers x an uncapped pool each is the
   real cost, not the per-producer number alone, and it climbed past
   3.5GB RSS and still rising before this cap existed). If a producer
   exhausts its (possibly capped) pool before the window ends, it simply
   stops publishing early rather than allocating mid-window - this means a
   trial's actual `elapsed_ms` can come in well under the nominal 4000ms
   for a fast implementation at high load, which is an accepted, visible
   limitation (report the real `elapsed_ms`, don't pad it) rather than a
   silently invalid run.
3. Settle (GC/explicit collection where the language has one, then a short
   pause — same as the firehose configs) before starting the clock.
4. **Each producer paces itself with a tick-based rate limiter**, not a
   naive per-publish sleep (unreliable at high rates and across languages
   with coarse sleep granularity):
   ```
   tick = 20ms
   per_tick = max(1, round(target_rate_eps_per_producer * tick_seconds))
   window_deadline = now() + sweep_duration_seconds
   while now() < window_deadline and pool not exhausted:
       tick_start = now()
       publish up to per_tick envelopes from the pool, back-to-back
         (each publish sets the payload timestamp immediately before
         calling Publish, same as the firehose configs)
       tick_elapsed = now() - tick_start
       if tick_elapsed < tick: sleep(tick - tick_elapsed)
       # else: don't sleep - this producer/the channel's Block wait is
       # already the bottleneck, and that's exactly the signal this
       # benchmark exists to show (achieved rate falling below target
       # rate once the stage is overloaded).
   ```
5. Record `end_of_window_depth` = the channel's `depth`/queue length at the
   instant the window ends (producers stop), **before** calling shutdown.
6. Call the bus's `shutdown(timeout)` (60s timeout, same as firehose) and
   time how long it takes to return `drained: true` — this is
   `drain_tail_ms`, the recovery time: how long it takes the stage to
   finish absorbing whatever backlog existed when producers stopped.
7. Compute latency percentiles over **every envelope delivered in this
   trial**, including ones delivered during the drain tail after the
   window closed — a real backlog above capacity doesn't stop mattering
   just because producers stopped publishing.

### Reading this data

- **`achieved_rate_eps` tracking `target_rate_eps` closely, `end_of_window_depth`
  staying low, `drain_tail_ms` near zero** — below or at sustained capacity,
  the stage is keeping up.
- **`achieved_rate_eps` falling short of `target_rate_eps`, `end_of_window_depth`
  growing, `drain_tail_ms` growing** — the stage is overloaded (expected
  and correct at `load_fraction: 1.5`; if it happens at `0.5` or `1.0` too,
  that's a real finding about this implementation, not a benchmark
  artifact).
- Latency percentiles are the direct, human-legible version of the same
  story: they should stay low (microseconds to low milliseconds) at `0.5`
  and mostly at `1.0`, and grow substantially by `1.5` as the backlog from
  Step 2's pacing accumulates.

## The firehose configs (secondary, diagnostic)

### What it measures

Bus/scheduler overhead in isolation from business logic: how fast can each
implementation move envelopes from `Publish` through one channel to a
consumer that does almost no work. This is **not** a measure of what a real
application would see (a real consumer does real work) — it isolates the
one thing this benchmark can compare fairly across seven very different
runtimes: the cost of the staging machinery itself.

**Envelope construction is deliberately excluded from the timed window.**
Every envelope is built (routing slip, headers, the works) in an untimed
warm-up phase before the clock starts; the timed producer loop only sets
the publish-time payload on an already-built envelope and calls `Publish`.
A real producer already holds a constructed envelope before it ever calls
the bus — building the envelope is the caller's cost, not the bus's, and
folding it into the timed window means measuring "how fast can this
language build a `ra-common`-shaped object," not "how fast is this bus."
This was not the original design: an earlier version of this benchmark
timed `make_envelope(...)` *and* `Publish` together in the same loop, which
silently mixed the two costs into every number in this report and
materially changed at least one finding (see `RESULTS.md`'s "Rust: the
`ra-common` rewire" for the concrete before/after). Caught directly ("we're
not measuring the ability of this code to create an Envelope"), not
self-discovered.

### The channel(s)

- `seq`/`par`: one channel, name `bench`. `chan`: `P` channels, named
  `bench0`..`bench{P-1}`, one per producer.
- `Delivery: PointToPoint`.
- `Capacity`: `TOTAL` (`seq`/`par`) or `TOTAL / P` per channel (`chan`) —
  large enough that back-pressure never engages during a run. This
  benchmark is about scheduling/dispatch throughput, not admission control.
- One consumer per channel: increments **that channel's own counter**,
  returns/acks `true`. No I/O, no allocation beyond what the language's
  increment requires. `chan`'s counters are independent per channel (an
  array/slice of them, one per producer) — not one shared counter behind
  one lock, which would silently reintroduce the exact contention `chan`
  exists to remove. (An earlier ad-hoc version of this check got this
  wrong for Python — a single shared counter across "independent" channels
  — and undermeasured the result; the per-channel counter here is the fix.)

### The three configurations

|                             | `seq` | `par`                         | `chan`                        |
|-----------------------------|-------|-------------------------------|--------------------------------|
| Producer threads/goroutines | 1     | `P = min(8, available cores)` | `P`                            |
| Channels                    | 1     | 1 (shared)                    | `P` (one per producer)         |
| Channel `Concurrency`       | 1     | `P` (all on the one channel)  | 1 each                         |

`par` and `chan` isolate two different things:

- **`par`** — `P` producers *and* up to `P` drain workers all sharing
  **one** channel: one bounded queue behind one lock. This is what "adding
  threads to a stage" means architecturally — more contenders for the same
  serialization point. Don't expect linear scaling here even from a
  correct, well-implemented bus; a single shared lock caps it well below
  `P`x, sometimes below 1x, once per-item work is small enough (see
  `METHODOLOGY.md`'s investigation of exactly this, prompted by a user
  catching that the first-pass numbers didn't hold up).
- **`chan`** — `P` producers, each with its own dedicated channel and
  dedicated consumer, no shared queue or lock between them at all. This is
  the configuration that answers "does more parallelism help," cleanly,
  because there's no artificial contention point left to hide the answer
  behind.

Both also surface the "True stage parallelism" row of `seda-bus/DESIGN.md`'s
comparison table as numbers, not just a yes/no — in particular, Python
under the GIL should show little-to-no scaling in either configuration,
exactly the effect `seda-bus-python`'s own README already describes
qualitatively.

### Per run

- `TOTAL = 200,000` envelopes, split as evenly as possible across the
  producer threads/goroutines (each publishes `TOTAL / P`, remainder on the
  first).
- Publish with a generous per-publish timeout (5s) — long enough that a
  slow implementation never spuriously fails a publish, short enough that a
  genuine deadlock surfaces as a failed run rather than hanging forever.
- Every envelope is fully constructed (routing included) in an untimed
  warm-up phase before the clock starts — one `Vec`/array/list per producer
  thread, handed off to that thread before `start := now()`. See "What it
  measures" above for why.
- Publish envelope payload: **a monotonic-clock timestamp, set on the
  already-built envelope immediately before `Publish`, in that language's
  own native representation** (e.g. `steady_clock::now()` ticks in C++,
  `nanoTime()` in Java, `perf_counter_ns()` in Python) via that port's
  `set_payload`/`SetPayload`/`add_content`-equivalent (a single field
  write, not a reconstruction) — see "Latency" below. Embedding the publish
  time and reading it back in the consumer is what latency measurement
  needs, and it's a small, equally-shaped cost in every language (one clock
  read on publish, one on delivery, one array write, one field write), so
  it doesn't bias throughput comparisons between languages even though it
  does add a small constant tax to all of them versus a version with no
  latency measurement at all.
- Timing window: wall-clock from immediately before the first `Publish`
  call to the bus reporting a full drain (`Shutdown` with a generous
  timeout — 60s — returning "drained"). A run whose `Shutdown` does not
  report a full drain is invalid and must be reported as such, not silently
  included in the results. Percentile computation (sorting the latency
  samples) happens **after** this window closes, so it never counts against
  throughput.
- **3 trials per configuration.** Report min / mean / max throughput
  (`TOTAL / elapsed_seconds`), not just one number — "means" plural, both
  senses: the method, and the arithmetic mean of repeated trials.

### Latency

Every delivered envelope's latency (consumer-invocation time minus the
publish-time embedded in its payload) is recorded into a preallocated,
per-run array — indexed by the same lock-free counter/index each
implementation already uses to count deliveries, so this adds no new
contention beyond what already exists. For `chan`, each channel keeps its
own array (same isolation principle as its own counter); percentiles are
computed over all channels' samples concatenated together, answering "what
latency does an envelope see in this configuration overall."

- **Clock:** each language's own monotonic clock (not wall-clock — avoids
  NTP-adjustment risk, irrelevant at this timescale but free to get right).
  Producer and consumer are the same process, so a raw tick/counter value
  with an arbitrary per-process origin is fine; it never needs to mean
  anything outside that process.
- **Unit:** converted to microseconds (`float`/`double`) at read-back or at
  aggregation time — always the unit in the output contract, regardless of
  what native precision the source clock has.
- **Percentiles:** `p50`, `p99`, `p999`, and `max`, computed by sorting that
  trial's (or, for `chan`, that trial's concatenated) sample array ascending
  and indexing at `floor(p * (n - 1))` — nearest-rank, no interpolation.
  This is directional, not measurement-grade (see `METHODOLOGY.md`'s
  limitations list); it's enough to see order-of-magnitude tail behavior
  and GC-pause-shaped spikes, not to make sub-2x latency claims.
- **This measures queueing delay, not just dispatch cost - by design, and
  that's important to read correctly.** `Capacity` is deliberately large
  enough that back-pressure never engages (see "The channel(s)" above), so
  whenever a producer publishes faster than its consumer(s) can drain, a
  backlog builds and later envelopes wait behind it. That's not a
  measurement artifact - it's the real, correct consequence of this
  benchmark's capacity choice, and it's *informative*: an implementation
  whose `p50` latency stays in the microseconds under a config means its
  consumer keeps pace with its producer(s); one whose `p50` balloons into
  the hundreds of milliseconds or seconds under the same config means its
  producer(s) are outrunning its consumer(s) and a real backlog is forming
  (see `RESULTS.md` for concrete cases). Don't read a large `p50` here as
  "this implementation is slow to process one envelope" - read it as "this
  implementation's consumer can't keep up with its producer at this
  concurrency," which is a different and equally real finding. **For what
  this same implementation does under a real, bounded capacity with actual
  backpressure engaged, see the capacity curve above** — that is the
  number to use for capacity planning; this section's is not.

## Output contract

Each `bench/<lang>` program prints one JSON line per trial to stdout, and
nothing else on stdout (logs/warnings go to stderr). `p50_us`/`p99_us`/
`p999_us`/`max_us` are that trial's latency percentiles in microseconds.
`total` is the number of envelopes actually published in that trial's
timed window (for the firehose configs this is always the fixed `200000`;
for the capacity curve it varies trial-to-trial with pacing). `throughput_eps`
is `total / elapsed_seconds` either way — "achieved rate" for the capacity
curve, raw dispatch throughput for the firehose configs.

Firehose configs (`config` is `seq`/`par`/`chan`; `channels` is 1 for
`seq`/`par`, `P` for `chan`):

```json
{"language":"go","config":"par","trial":1,"producers":8,"concurrency":8,"channels":1,"total":200000,"elapsed_ms":812,"throughput_eps":246305,"p50_us":12.4,"p99_us":88.7,"p999_us":210.3,"max_us":4102.6,"drained":true}
```

Capacity-curve configs (`config` is `cap1`/`cap8`) carry `delivered` (as in
the firehose configs — must equal `total` once `drained: true`, since
`Block` never drops) plus five additional fields: `capacity` (1024,
fixed), `load_fraction` (`0` for a Step-1 calibration row, else
`0.5`/`1.0`/`1.5`), `target_rate_eps` (`0` for calibration — there is no
target, it's unpaced), `end_of_window_depth` (the channel's queue length
the instant producers stopped, before `shutdown` was called), and
`drain_tail_ms` (how long `shutdown` then took to report fully drained):

```json
{"language":"go","config":"cap8","trial":1,"producers":8,"concurrency":8,"channels":1,"capacity":1024,"load_fraction":1.0,"target_rate_eps":180000,"total":719812,"delivered":719812,"elapsed_ms":4000,"throughput_eps":179953,"end_of_window_depth":112,"drain_tail_ms":8,"p50_us":41.2,"p99_us":390.5,"p999_us":812.0,"max_us":1503.2,"drained":true}
```

`scripts/aggregate.py` reads these lines (one file per language under
`results/raw/`) and produces `results/summary.csv` plus the tables in
`RESULTS.md`.

## What this deliberately does not measure

Envelope/message construction cost (excluded from the timed window in both
benchmarks — see "What it measures" above; it's measured separately, once,
as a dedicated construction-only micro-benchmark where that number matters
on its own), startup/JIT-warmup cost (the timing window starts after the
bus and channel are constructed), memory footprint, or anything under real
contention with other stages (multi-stage/routing-slip itineraries). The
capacity curve only exercises `Block` backpressure — `Reject`/`DropNewest`/
`DropOldest` behavior under sustained overload, retry, and dead-letter are
already covered functionally (pass/fail, not throughput) by each port's own
correctness suite (`seda-bus/CORRECTNESS_SUITE.md`), not by this benchmark.
See `METHODOLOGY.md` for the full list of limitations and why these
numbers should be read as directional, not authoritative.
