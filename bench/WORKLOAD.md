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
- Publish envelope payload: a plain integer (the producer's local counter).
  Not read back by the consumer — the consumer only counts.
- Timing window: wall-clock from immediately before the first `Publish`
  call to the bus reporting a full drain (`Shutdown` with a generous
  timeout — 60s — returning "drained"). A run whose `Shutdown` does not
  report a full drain is invalid and must be reported as such, not silently
  included in the results.
- **3 trials per configuration.** Report min / mean / max throughput
  (`TOTAL / elapsed_seconds`), not just one number — "means" plural, both
  senses: the method, and the arithmetic mean of repeated trials.

## Output contract

Each `bench/<lang>` program prints one JSON line per trial to stdout, and
nothing else on stdout (logs/warnings go to stderr). `channels` is 1 for
`seq`/`par`, `P` for `chan`:

```json
{"language":"go","config":"par","trial":1,"producers":8,"concurrency":8,"channels":1,"total":200000,"elapsed_ms":812,"throughput_eps":246305,"drained":true}
{"language":"go","config":"chan","trial":1,"producers":8,"concurrency":1,"channels":8,"total":200000,"elapsed_ms":110,"throughput_eps":1818181,"drained":true}
```

`scripts/aggregate.py` reads these lines (one file per language under
`results/raw/`) and produces `results/summary.csv` plus the tables in
`RESULTS.md`.

## What this deliberately does not measure

Startup/JIT-warmup cost (the timing window starts after the bus and channel
are constructed), memory footprint, latency percentiles (only aggregate
throughput), or anything under real contention with other stages —
multi-stage/routing-slip itineraries, back-pressure, retry, and dead-letter
are already covered functionally by each port's own test suite, not by this
benchmark. See `METHODOLOGY.md` for the full list of limitations and why
these numbers should be read as directional, not authoritative.
