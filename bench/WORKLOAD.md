# The benchmark workload

One specification, implemented identically in all seven `seda-bus` ports.
Each `bench/<lang>/` program is a straight translation of this — same
numbers, same shape, no language-specific tuning.

## What it measures

Bus/scheduler overhead in isolation from business logic: how fast can each
implementation move envelopes from `Publish` through one channel to a
consumer that does almost no work. This is **not** a measure of what a real
application would see (a real consumer does real work) — it isolates the
one thing this benchmark can compare fairly across seven very different
runtimes: the cost of the staging machinery itself.

## The channel(s)

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

## The three configurations

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

## Per run

- `TOTAL = 200,000` envelopes, split as evenly as possible across the
  producer threads/goroutines (each publishes `TOTAL / P`, remainder on the
  first).
- Publish with a generous per-publish timeout (5s) — long enough that a
  slow implementation never spuriously fails a publish, short enough that a
  genuine deadlock surfaces as a failed run rather than hanging forever.
- Publish envelope payload: **a monotonic-clock timestamp, taken
  immediately before `Publish`, in that language's own native
  representation** (e.g. `steady_clock::now()` ticks in C++, `nanoTime()`
  in Java, `perf_counter_ns()` in Python) — see "Latency" below. This
  replaced an earlier version of this spec where the payload was a plain
  unread integer; embedding the publish time and reading it back in the
  consumer is what latency measurement needs, and it's a small, equally-
  shaped cost in every language (one clock read on publish, one on
  delivery, one array write), so it doesn't bias throughput comparisons
  between languages even though it does add a small constant tax to all of
  them versus the pre-latency numbers.
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

## Latency

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
  concurrency," which is a different and equally real finding.

## Output contract

Each `bench/<lang>` program prints one JSON line per trial to stdout, and
nothing else on stdout (logs/warnings go to stderr). `channels` is 1 for
`seq`/`par`, `P` for `chan`. `p50_us`/`p99_us`/`p999_us`/`max_us` are that
trial's latency percentiles in microseconds:

```json
{"language":"go","config":"par","trial":1,"producers":8,"concurrency":8,"channels":1,"total":200000,"elapsed_ms":812,"throughput_eps":246305,"p50_us":12.4,"p99_us":88.7,"p999_us":210.3,"max_us":4102.6,"drained":true}
{"language":"go","config":"chan","trial":1,"producers":8,"concurrency":1,"channels":8,"total":200000,"elapsed_ms":110,"throughput_eps":1818181,"p50_us":3.1,"p99_us":19.5,"p999_us":41.2,"max_us":980.0,"drained":true}
```

`scripts/aggregate.py` reads these lines (one file per language under
`results/raw/`) and produces `results/summary.csv` plus the tables in
`RESULTS.md`.

## What this deliberately does not measure

Startup/JIT-warmup cost (the timing window starts after the bus and channel
are constructed), memory footprint, or anything under real contention with
other stages — multi-stage/routing-slip itineraries, back-pressure, retry,
and dead-letter are already covered functionally by each port's own test
suite, not by this benchmark. See `METHODOLOGY.md` for the full list of
limitations and why these numbers should be read as directional, not
authoritative.
