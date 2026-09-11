# Results

Read [`METHODOLOGY.md`](METHODOLOGY.md) first — what's measured, and what
it deliberately doesn't mean. These numbers are directional (single
shared-tenancy Docker host, three trials), not benchmark-grade.

**Run date:** 2026-09-11. Raw data: [`results/raw/*.jsonl`](results/raw/),
[`results/summary.csv`](results/summary.csv). Regenerate with
`./scripts/build_and_run.sh && python3 scripts/aggregate.py`.

## Throughput

200,000 envelopes/trial, 3 trials/configuration, `seq` = 1 producer/
concurrency 1, `par` = 8 producers/concurrency 8. All numbers envelopes/sec.

| Implementation | seq mean | seq range | par mean | par range | par vs. seq |
|---|--:|---|--:|---|--:|
| Rust | 642,641 | 604,900–661,900 | 615,765 | 605,459–629,198 | 0.96x |
| Java | 333,161 | 231,570–433,538 | 504,766 | 481,967–526,969 | 1.52x |
| C# | 174,228 | 151,264–189,305 | 270,530 | 260,592–283,496 | 1.55x |
| Go | 148,110 | 132,848–165,915 | 260,332 | 248,170–270,672 | 1.76x |
| C++ | 135,769 | 119,472–165,546 | 45,263 | 42,996–47,874 | **0.33x** |
| Python 3.14t (free-threaded) | 33,795 | 33,390–34,200 | 35,446 | 35,248–35,616 | 1.05x |
| TypeScript (Node 22) | 16,783 | 16,584–16,952 | 16,714 | 16,519–16,925 | 1.00x |
| Python 3.13 (GIL) | 41,669 | 39,602–43,556 | 11,344 | 11,025–11,866 | **0.27x** |

Two results are bolded because they're the interesting ones, not because
they're wins:

- **C++'s `par` is 3x *slower* than its own `seq`** — not a language
  characteristic, a measured, reproducible lock-contention bottleneck (see
  below and `METHODOLOGY.md`). Before a separate bug fix (also below),
  C++'s `seq` itself was ~13,000-16,000 eps — 8-9x slower than what's shown
  here.
- **Python 3.13's `par` is 3.7x *slower* than its own `seq`** — this is
  `seda-bus-python`'s own documented GIL-contention effect
  (`seda-bus-python/README.md`), reproduced exactly here: under the GIL,
  adding worker threads to contended, lock-heavy code makes it slower, not
  faster.

## What this found: a real bug, not just numbers

The first C++ run measured ~15,000 eps sequential — 20-30x slower than
Rust for identical work between two hand-rolled-thread-pool, no-GC
implementations that should be structurally comparable. Traced to
`ra-common-cpp` re-opening `/dev/urandom` (`fopen`/`fread`/`fclose`) on
*every* random-byte call, and envelope construction calling it twice per
envelope — 400,000 file-open cycles per trial. Fixed upstream
(`ra-common-cpp` commit `8700729`): keep the file handle open for the
process, guarded by a mutex. **The table above is post-fix** — an 8-9x
improvement, putting C++ roughly where Rust/Go/C#'s design would predict.

That same mutex is *why* `par` is now the slow case: 8 producer threads
serializing through one shared, mutex-guarded `/dev/urandom` handle, and —
per `METHODOLOGY.md` — that contention is markedly more expensive under
Docker's virtualization than it was in a native (non-containerized) run on
the same machine (~120,000 eps `par`, roughly matching `seq`, natively vs.
~45,000 eps here). Not yet fixed; a good next step for `ra-common-cpp`
would be per-thread buffering or calling `getrandom()` directly instead of
sharing one `FILE*`.

## Python: GIL vs. free-threaded, and why this benchmark undersells it

`seda-bus-python` exists specifically to demonstrate free-threaded
CPython's value for CPU-bound staged work — and its own README shows a real
~2.6x speedup on an actual CPU-bound task (hashcash/fib) across a 12-core
pool. This benchmark's consumer does almost no CPU work (one atomic
increment), so there's very little for free-threading to parallelize:
3.14t's `par`/`seq` ratio here is only 1.05x, far short of that 2.6x. What
this benchmark *does* show clearly is the GIL cost on the other side:
3.13's `par` collapses to 0.27x of its own `seq` — 8 threads fighting over
the GIL on lock-heavy bus internals makes it slower than not using threads
at all, exactly `seda-bus-python`'s own documented effect, now reproduced
against an identical workload across all seven other implementations.

## TypeScript: no scaling by design, not a bug

`ts`'s `par`/`seq` ratio is 1.00x — expected, not a finding. Node runs
JavaScript on one thread; `par` here means 8 concurrently-awaited
async producer loops on that one event loop, not real parallelism (see
`seda-bus/DESIGN.md`'s "True stage parallelism" row: TS only gets real
parallelism for `worker`-configured stages, which this benchmark doesn't
use, since the workload is a shared in-memory counter no worker-thread
transport applies to).

## Other attributes

Pulled from [`seda-bus/DESIGN.md`](../DESIGN.md)'s own comparison table
(§2.1) — that document is the maintained source, not re-derived here — plus
three new columns from this benchmarking pass.

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

Source LOC: `wc -l` over each port's library source only (no tests, no
vendored dependencies, no build output) — see
[`scripts/count_loc.sh`](scripts/count_loc.sh) for exactly what's counted
per language.

## Reading this table honestly

Rust and Java's absolute numbers being 2-4x everyone else's is a real,
structural result (real OS-thread parallelism, no interpreter, no `/dev/
urandom` bottleneck) — but the gap between, say, Go and C# (both real
threads, both garbage-collected, both ~150-270k eps) is well within what
this benchmark's own noise floor (three trials, shared-tenancy host) can
distinguish confidently. Don't read three-digit percentage differences
between adjacent rows as meaningful; do read order-of-magnitude
differences (Rust/Java vs. TypeScript, `par` collapsing under Python's GIL
or C++'s mutex) as real.
