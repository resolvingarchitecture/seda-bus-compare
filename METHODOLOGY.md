# Methodology

How the numbers in `RESULTS.md` were produced, what went wrong twice along
the way, and — more importantly — what these numbers don't mean. This
document is as much a record of getting challenged and checking as it is a
spec, because the checking is what makes the numbers worth trusting.

## The workload

See [`bench/WORKLOAD.md`](bench/WORKLOAD.md) for the full specification.
Short version: 200,000 envelopes per trial, 3 trials per configuration, in
three configurations:

- **`seq`** — 1 producer, 1 channel, concurrency 1. The baseline.
- **`par`** — `P = min(8, cores)` producers, all publishing to **one**
  shared channel with concurrency `P`. Tests what "adding threads to a
  stage" means architecturally: more contenders for one lock.
- **`chan`** — `P` producers, each with its **own** dedicated channel and
  consumer, no shared queue between them. Tests what parallelism can
  actually achieve once the shared serialization point is removed.

`par` and `chan` exist as two separate configurations, not one, because
they answer different questions — see "Does parallelism work?" below for
why that distinction turned out to be the whole point of this report.

## Reproducing this

```sh
./setup.sh                    # verifies the 14 sibling repos this depends on are present
./scripts/build_and_run.sh    # builds all 8 Docker images (7 languages + Python's
                               # free-threaded variant), runs each, writes
                               # results/raw/<lang>.jsonl
python3 scripts/aggregate.py  # writes results/summary.csv, prints the RESULTS.md table
```

Every benchmark runs inside a Docker container with a pinned base image
(see each `bench/<lang>/Dockerfile`), not on whatever happens to be
installed on the machine running this — `ra-common-*`/`seda-bus-*` are only
resolvable as monorepo-relative source, not a package registry, so
"reproducible" has to mean "the same toolchain version building the same
source."

## A contaminated first run, and how it was caught

The first full run reported Rust's `par` as flat (0.96x of `seq`) and
explained that with a plausible-sounding theory: bus overhead near zero for
Rust, a single shared mutex, nothing left to parallelize. **That
explanation was wrong**, and it was wrong because the underlying number was
wrong — not because the reasoning about mutexes was bad reasoning in the
abstract.

The user pushed back directly: *"if we push to 1000 threads, you're trying
to tell me we get lower throughput?"* and *"you're using 8 threads, you
should get close to 8x throughput otherwise your code is shit."* Rather
than defend the original number, it got checked: a scaling test at
1/8/32/128/512/1000 producer threads, run natively, showed throughput
*increasing* from 1 to 8 threads (261,602 → 732,119 eps) — flatly
contradicting the "official" report of 0.96x. Something was wrong with the
measurement, not the theory.

The actual cause: this machine runs other things. Two unrelated containers
(`meridian-infra-*`, not part of this project) were running during the
original benchmark pass, and — more significantly — **this session was
itself running multiple concurrent Docker builds while the "official"
timed runs executed**, from building and testing seven other language
images in parallel to keep the overall task moving. An 8-thread benchmark
is far more sensitive to competing load than a 1-thread one: it needs 8
free cores to show its real behavior, and a busy host quietly serializes
what should be parallel work. A clean, isolated re-run of the *exact same
binary* — nothing else running — showed real scaling (seq ~625k → par
~724k, ~1.16x), not the flat result originally reported.

**Every number in `RESULTS.md` is from a rerun with nothing else executing
concurrently on the host**, confirmed via `uptime` and `docker ps` before
each run. This is why the methodology doc spends this much space on a
process failure: the failure is more instructive than the fix. A benchmark
number that can't survive "did anything else touch the CPU while this
ran?" isn't a number yet.

## C++: three real bugs, found and fixed

The first (contaminated) C++ run measured ~15,000 eps sequential — 20-30x
slower than Rust for identical work between two hand-rolled-thread-pool,
no-GC implementations that should be structurally comparable. Investigated
rather than accepted, and it took three separate fixes:

1. **`ra-common-cpp` was re-opening `/dev/urandom`**
   (`fopen`/`fread`/`fclose`) on *every* random-byte call, and envelope
   construction calls it twice per envelope — 400,000 file-open cycles per
   trial. Every other language's random source is a single syscall, or
   (Python's non-crypto `random`) none at all. **Fixed: commit `8700729`**
   — keep the file handle open for the process, mutex-guarded.
2. That still left C++ ~4.7x behind Rust. An isolated micro-benchmark —
   envelope construction alone, no bus at all — hit 1.19M eps, ~9x faster
   than the full publish-through-bus number at the time, pointing straight
   at the bus path. `Envelope::GetRoute()`/`Ratchet()` turned out to clone
   the current route by **serializing it to a JSON tree and immediately
   re-parsing that tree back**, just to get an independently-owned
   pointer — every other port's `GetRoute()` just copies a reference, since
   only C++'s `unique_ptr` ownership model needs an independent clone at
   all. **Fixed: commit `7e5717b`** — a proper virtual `Route::Clone()`.
3. Fix 1 kept the `/dev/urandom` handle open for the process but put a
   single `std::mutex` around it, shared by every thread regardless of
   which channel it published to. That's invisible in `par` (already
   serialized on the channel's own queue lock) but directly visible in
   `chan` (independent channels, *no* shared queue lock at all) — `chan`
   still only reached 1.42x over `seq`, far below Rust/Go's 5-6x, and
   nothing about independent channels should cap scaling that hard.
   **Fixed: commit `1b3f687`** — replaced the shared `FILE*`+mutex with
   `getentropy(2)`, a direct syscall with no shared file descriptor or
   handle, so no lock is needed at all; each thread just calls it.

All three fixes are real, verified, and reflected in every C++ number in
`RESULTS.md`. Fix 3's effect was immediate and large: `chan` went from
282,287 eps (1.42x) to 712,444 eps (**5.29x**) — right in Rust/Go's range,
confirming the shared mutex, not anything structural about C++ or its
queue implementation, was the ceiling.

That result also settles what fix 3 was *not* responsible for: `par`
stayed collapsed (0.48x, statistically the same as the 0.34x measured
before the fix) even after the urandom mutex was gone. Since `chan` has no
shared queue and improved 3.7x from this fix while `par` (which does have
a shared queue) didn't move, the urandom mutex was never `par`'s problem —
`par`'s bottleneck is the channel's own bounded-queue lock, and
specifically its behavior under Docker: a native (non-containerized) run
of the identical post-fix binary shows `par` essentially matching `seq`
(no collapse at all), the same native/Docker gap noted in the first round
of C++ fixes. That's now the closed, fully-explained version of the C++
story — no open items left on this pass.

## Does parallelism work? Yes — once you check what "parallel" means here

The sharpest challenge, and the one that shaped this report the most:
*"why are you using a shared mutex? why are you purposely creating a
bottleneck?"* and *"adding threads does not improve throughput unless
adding channels equal to those threads."*

The direct answer: a mutex-guarded bounded queue isn't a deliberately
introduced bottleneck — it's the standard, conventional way to make a
*single shared stage* (the whole point of SEDA: many producers, one bounded
admission-controlled queue) safe for concurrent access, and it's what all
seven ports do, including the original Java. `par` tests exactly that
architecture: `P` producers **and** up to `P` drain workers all contending
for the same lock. No implementation of "one shared lock, near-zero work
per item" scales linearly with thread count — that's textbook lock
contention, true in any language.

That claim was tested, not just asserted. A controlled comparison —
1 thread/1 channel, vs. 8 threads sharing 1 channel, vs. 8 threads with 8
independent channels (no shared lock at all) — settles it:

| | 1 thread, 1 channel | 8 threads, 1 shared channel | 8 threads, 8 independent channels |
|---|--:|--:|--:|
| Rust (native) | 244,066 eps | 747,582 eps (3.06x) | 1,939,836 eps (**7.95x**) |
| Python 3.13, GIL (native) | 30,676 eps | 18,475 eps (0.60x) | 21,115 eps (0.69x) |
| Python 3.14t, free-threaded (native) | 36,599 eps | 37,982 eps (1.04x) | 124,578 eps (**3.40x**) |

Independent channels get close to the linear scaling a naive "8 threads,
8x throughput" intuition expects; a shared channel does not, for any
language, because it's testing a different thing (lock contention, not
parallel capacity). Neither number is "wrong" — they're answers to two
different questions, and the mistake in the first draft of this report was
not making that distinction clear rather than running the comparison at
all.

This is now a permanent third configuration (`chan`) in the benchmark, not
a one-off scratch test — see `bench/WORKLOAD.md` and the full seven-language
`chan` results in `RESULTS.md`.

### Mutex vs. lock-free: the actual tradeoff, not just "mutexes are bad"

A mutex-guarded queue is the right *default*, not a mistake:

- **Correctness is straightforward** — hold the lock, check/mutate state,
  release. Lock-free structures need careful memory-ordering reasoning
  (ABA problems, safe reclamation) that's notoriously easy to get subtly
  wrong.
- **`Block` back-pressure and `DropOldest` come for free** — a producer
  sleeping until there's room, or evicting the oldest entry atomically, are
  natural fits for "hold the lock, do it." Both are materially harder to
  implement correctly lock-free.
- **Available everywhere, no dependencies** — every one of the seven
  languages has a mutex+condvar/monitor in its standard library; a solid
  lock-free MPMC queue generally isn't stdlib anywhere used here.
- **Cheap when uncontended** — for any real workload where per-envelope
  processing takes microseconds or more (the common case), the lock is a
  small fraction of total time. This benchmark's near-zero-work design is
  the pathological case built specifically to surface lock cost, not the
  representative one.

The cons are exactly what `par` measures: one serialization point for the
whole stage, non-linear (sometimes negative) degradation under contention,
and — confirmed this session — meaningfully worse behavior under
virtualization than native. The lowest-risk improvement, if `par`-style
scaling on a single shared stage matters for a real use case, is a
**two-lock queue** (separate put/take locks, as Java's
`LinkedBlockingQueue` does) rather than going fully lock-free — it directly
targets "producers and consumers both fighting over one lock" with far less
correctness risk than hand-rolled lock-free code. Not implemented in any
port here; a scoped follow-up, not a claim about what the current numbers
already show.

## Python: GIL vs. free-threaded, verified with the same rigor

`seda-bus-python` exists to demonstrate free-threaded CPython's value for
CPU-bound staged work, and its own README shows a real ~2.6x speedup on an
actual CPU-bound task (hashcash/fib). This benchmark's consumer does almost
no CPU work, so `par` (one shared channel) shows only 0.97x for 3.14t —
not because free-threading doesn't work, but because there's essentially
nothing to parallelize once bus overhead dominates and the shared lock caps
what's left. `chan` (independent channels, no shared lock) shows real
gains — 1.69x in the Docker run, 3.40x in an isolated native check — closer
to what free-threading should deliver once the artificial contention point
is removed.

One thing corrected mid-investigation: an earlier ad-hoc verification
script for `chan` had its own bug — the "independent" channels' consumer
closures accidentally captured one shared `lock`/`count` instead of one
each, silently reintroducing exactly the contention `chan` exists to
remove, and undermeasuring the result. Fixed in `bench/python/bench.py`
(each channel's consumer now closes over its own counter and its own lock,
never shared) — the numbers in `RESULTS.md` are from the fixed version.

3.13's GIL cost is real and reproduces in both configurations (`par` 0.29x,
`chan` 0.28x) — under the GIL, only one thread executes Python bytecode at
a time, so more threads contending for that single execution slot on
already lock-heavy bus code makes things slower, not faster, regardless of
channel topology. This matches `seda-bus-python`'s own documented GIL
finding.

## TypeScript: `chan` helps even with no real OS threads

`ts`'s `par` is flat (1.01x) — expected: Node runs JavaScript on one
thread, so `par`'s "8 producers" are concurrently-awaited async tasks on
that one event loop, not OS-level parallelism (`seda-bus/DESIGN.md`'s "True
stage parallelism" row: TS only gets real parallelism for
`worker`-configured stages, not used here). What's more interesting: `chan`
still shows a real 3.37x gain despite there being no CPU parallelism to
gain at all. That's not a contradiction — a single heavily-contended
channel has real per-operation coordination overhead (permit-checking,
promise/microtask scheduling under concurrent `await`s) beyond just
"waiting for a lock," and splitting work across independent channels
reduces that overhead even on one thread.

## Known limitations (by design, not oversight)

- **No latency percentiles**, only aggregate throughput. A slow-tail
  envelope is invisible here.
- **No multi-stage/routing-slip itineraries, no back-pressure, no
  retry/dead-letter path.** Those are covered functionally (not for
  performance) by each port's own test suite.
- **Three trials, single machine.** Enough to see real, order-of-magnitude
  effects (this document exists because that's exactly what it took to
  catch a contaminated run and three real bugs), not enough for statistical
  rigor on small differences between structurally similar implementations.
- **The Java/`common` version-skew workaround.** `seda-bus-java`'s
  `pom.xml` depends on `resolvingarchitecture:common:1.2.0`, but
  `ra-common-java`'s own `pom.xml` is currently at `1.3.2`.
  `bench/java/Dockerfile` installs the built jar under both its real
  version and a `1.2.0` alias rather than editing either repo — a real,
  observed piece of ecosystem drift, out of scope to fix here.
- **No official free-threaded Python Docker image exists** (checked:
  `python:3.14t-slim` and similar tags don't resolve on Docker Hub), so
  `bench/python/Dockerfile.freethreaded` builds CPython 3.14.0 from source
  with `--disable-gil` — slow (multi-minute) but a real reproduction path,
  not a workaround that skips the variant.

## "Other attributes"

`RESULTS.md`'s attributes table pulls most of its columns directly from
[`seda-bus/DESIGN.md`](../DESIGN.md)'s own comparison table (§2.1) — that
document is the maintained source of truth for design decisions; this
report doesn't re-derive it. New columns: source lines of code (`wc -l`
over each port's library source, excluding tests/vendored
dependencies/build output — see `scripts/count_loc.sh`), integration test
count, and whether the *implementation itself* (not this benchmark) has
been verified under a race detector/sanitizer — `seda-bus-go` is the only
one with a clean, trustworthy sanitizer run (`go test -race`, five repeated
runs); `seda-bus-cpp`'s ThreadSanitizer attempt could not be trusted in the
sandbox it was built in.
