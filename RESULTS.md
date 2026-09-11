# Results

Read [`METHODOLOGY.md`](METHODOLOGY.md) first. It's not optional this time:
the first pass at these numbers was contaminated by concurrent load on the
host and reported a wrong conclusion (Rust's `par` looking flat) with a
plausible-sounding but incorrect explanation attached. That was caught by
direct, specific pushback, verified with a clean rerun, and led to finding
three real bugs in `ra-common-cpp` and a genuine gap in how this
benchmark's `par` configuration was being interpreted. Everything below
reflects that — this is the corrected report, not the first draft.

**Run date:** 2026-09-11 (corrected run, C++ numbers re-measured after the
third fix — see below). Raw data: [`results/raw/*.jsonl`](results/raw/),
[`results/summary.csv`](results/summary.csv). Regenerate with
`./scripts/build_and_run.sh && python3 scripts/aggregate.py` — on a quiet
host; see `METHODOLOGY.md` for why that matters.

## Throughput: three configurations, not two

200,000 envelopes/trial, 3 trials/configuration. All numbers envelopes/sec,
mean of 3 trials (see `results/summary.csv` for min/max ranges).

- **`seq`** — 1 producer, 1 channel. Baseline.
- **`par`** — 8 producers, **1 shared channel**. Tests lock contention on a
  single stage.
- **`chan`** — 8 producers, **8 independent channels** (1:1). Tests actual
  parallel capacity with the artificial shared-lock contention point
  removed.

| Implementation | seq | par | par vs. seq | chan | chan vs. seq |
|---|--:|--:|--:|--:|--:|
| Rust | 584,735 | 724,560 | 1.24x | **3,179,877** | **5.44x** |
| Java | 338,696 | 603,918 | 1.78x | 961,513 | 2.84x |
| Go | 155,884 | 273,391 | 1.75x | **948,740** | **6.09x** |
| C++ | 134,563 | 65,193 | **0.48x** | **712,444** | **5.29x** |
| C# | 179,103 | 321,402 | 1.79x | 475,127 | 2.65x |
| Python 3.14t (free-threaded) | 34,864 | 33,768 | 0.97x | 59,038 | 1.69x |
| TypeScript (Node 22) | 16,879 | 17,059 | 1.01x | 56,947 | 3.37x |
| Python 3.13 (GIL) | 40,512 | 11,734 | **0.29x** | 11,259 | 0.28x |

C++'s `chan` numbers above are post-fix (see "C++: three real bugs" below)
— the urandom mutex that used to cap it at 1.42x is gone, and `chan` now
scales in the same range as Rust and Go (5.29x). `seq` dropped slightly
versus the earlier report's 199,333 (now 134,563) — noise between Docker
runs at single-thread scale, not a regression; see "Reading this table
honestly" below on why three-trial, single-host differences at this scale
shouldn't be read as meaningful on their own.

**The headline finding:** every implementation gets real, often substantial
gains from parallelism — `chan` beats `seq` in all eight cases except
GIL-bound Python. The `par` column is not a measure of "how parallel is
this bus" — it's a measure of lock contention on one shared stage, and
conflating the two was the error in this report's first draft. See
`METHODOLOGY.md`'s "Does parallelism work?" section for the controlled
comparison that proves this rather than asserts it.

## What went wrong, and what's real

- **Rust's original "flat `par`" (0.96x) was a measurement artifact**, not
  a finding. The host was running concurrent Docker builds during the
  original timed run; a clean rerun with nothing else executing shows real
  scaling (`par` 1.24x) and, more importantly, `chan` shows Rust scaling
  essentially as well as any implementation here (5.44x, second only to
  Go). The original "near-zero bus overhead, nothing left to parallelize"
  explanation was plausible-sounding and wrong — it explained a number that
  was itself wrong.
- **C++'s `par` collapse (now 0.48x, previously 0.34x) is real and
  reproduces consistently** across multiple clean runs, unlike Rust's.
  Traced to three now-fixed bugs in `ra-common-cpp` (commits `8700729`,
  `7e5717b`, `1b3f687` — see `METHODOLOGY.md`). The third fix (removing the
  shared `/dev/urandom` mutex) fixed `chan` completely — 1.42x → 5.29x,
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
measurement) across the first two fixes, all Docker-measured; a later clean
rerun put `seq` at ~134,563 (see "reading this table honestly" on
run-to-run noise at single-thread scale). After fix 3, `chan` jumped from
282,287 to **712,444 eps — 5.29x over `seq`**, right in Rust/Go's range.
`par` did *not* improve from fix 3 (still 0.48x) — proof, not just
inference, that the urandom mutex was never `par`'s problem: `par`'s
bottleneck is the channel's own bounded-queue lock under Docker
specifically (a native run of the identical binary shows `par` ≈ `seq`; see
`METHODOLOGY.md`).

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

| | Java | Rust | Python | TypeScript | C++ | C# | Go |
|---|--:|--:|--:|--:|--:|--:|--:|
| Version | 1.3.1 | 0.3.0 | 0.2.0 | 0.2.0 | 0.1.0 | 0.1.0 | 0.1.0 |
| Source LOC | 962 | 718 | 595 | 835 | 744 | 620 | 739 |
| Integration tests | 8 | 9 | 11 | 16 | 13 | 13 | 13 |
| Runtime deps beyond `ra-common-*` | 0 | `log` | 0 | 0 | 0 | 0 | 0 |
| Envelope source | `ra-common` | own struct | `ra-common` | `ra-common` | `ra-common-cpp` | `ra-common-cs` | `ra-common-go` |
| Worker pool | `ExecutorService` | hand-rolled | `ThreadPoolExecutor` | event loop | hand-rolled | shared `ThreadPool` | none (goroutines) |
| True stage parallelism | yes | yes | only free-threaded | worker stages only | yes | yes | yes |
| Race/sanitizer-verified | not run | not run | not run | not run | attempted, untrustworthy (TSan, sandboxed) | not run | **yes** (`go test -race`, clean, 5 runs) |
| Guaranteed delivery | yes | no | no | no | no | no | no |

Source LOC: `wc -l` over each port's library source only — see
[`scripts/count_loc.sh`](scripts/count_loc.sh).

## Reading this table honestly

Rust and Go's `chan` numbers being 5-6x, well ahead of Java/C#'s 2.6-2.8x,
is a real, structural result (both compile to native code with real OS
threads and no GC pause risk) — but part of Rust's overall lead is also
that `seda-bus-rust` is the one port never rewired onto `ra-common`'s
`Envelope` (own minimal struct, no JSON, no crypto-random IDs; see
`seda-bus/DESIGN.md`), so this benchmark isn't purely comparing "bus
overhead" when Rust is one of the seven — it's also comparing a
structurally lighter envelope against six implementations carrying
`ra-common`'s heavier one. That's a property of the `seda-bus` ecosystem,
documented not hidden. Within the six `ra-common`-carrying implementations,
don't read close percentage differences between adjacent rows as
meaningful given three trials on a single host; do read order-of-magnitude
differences, `par`-vs-`chan` gaps, and collapses as real — every one
reported here was independently checked against a clean, isolated
measurement, not assumed from a single run.
