"""Benchmark harness. See ../WORKLOAD.md for the specification this
implements — every bench/<lang> program follows the same shape.

Imports seda_bus/ra_common directly off their src/ trees (sys.path, not an
installed package) so this needs no venv/build step — just python3.13+.
"""

import gc
import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "seda-bus-python", "src"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "..", "common", "ra-common-python", "src"))

from seda_bus import SEDABus, make_envelope  # noqa: E402
from seda_bus.envelope import envelope_payload, set_payload  # noqa: E402

TOTAL = 200_000
TRIALS = 3

# Pre-building TOTAL envelopes is itself a burst of allocation right before
# the timed window starts; without a settle pause + explicit GC, a
# collection provoked by that burst can land inside the first few timed
# publishes instead (caught happening - inconsistently, across several
# languages - the first time this benchmark measured envelope construction
# separately from dispatch). See ../WORKLOAD.md.
SETTLE_S = 0.2


def _settle() -> None:
    gc.collect()
    time.sleep(SETTLE_S)


def _version_fields() -> dict:
    return {
        "python_version": sys.version.split()[0],
        "gil_enabled": bool(getattr(sys, "_is_gil_enabled", lambda: True)()),
    }


def _now_ns() -> int:
    """Monotonic nanoseconds, arbitrary per-process origin - only ever
    diffed within this process. See ../WORKLOAD.md's "Latency" section."""
    return time.perf_counter_ns()


def _compute_latency_stats(samples: list[float]) -> dict:
    """Sorts (consumes) `samples`, computed AFTER the timed window closes so
    it never counts against throughput."""
    samples.sort()
    n = len(samples)
    return {
        "p50_us": samples[int(0.50 * (n - 1))],
        "p99_us": samples[int(0.99 * (n - 1))],
        "p999_us": samples[int(0.999 * (n - 1))],
        "max_us": samples[n - 1],
    }


def run_shared(config: str, producers: int) -> dict:
    """producers threads, all publishing to ONE channel with
    concurrency=producers - the "seq"/"par" configs."""
    bus = SEDABus(workers=producers)
    bus.start()
    count = {"n": 0}
    lock = threading.Lock()
    latencies: list[float] = [0.0] * TOTAL

    bus.channel("bench", capacity=TOTAL, concurrency=producers)

    def consumer(env):
        t1 = _now_ns()
        t0 = envelope_payload(env)
        with lock:
            idx = count["n"]
            count["n"] += 1
        latencies[idx] = (t1 - t0) / 1000.0
        return True

    bus.subscribe("bench", consumer)

    per_producer = TOTAL // producers
    remainder = TOTAL - per_producer * producers

    # Pre-build every envelope before the timed window starts - this
    # benchmark measures bus dispatch/queueing overhead, not envelope
    # construction cost. In production the producer already holds a
    # constructed envelope before it ever calls publish(). See
    # ../WORKLOAD.md.
    per_producer_envelopes = []
    for p in range(producers):
        n = per_producer + (remainder if p == 0 else 0)
        per_producer_envelopes.append([make_envelope("bench", 0) for _ in range(n)])

    def produce(envs):
        for env in envs:
            set_payload(env, _now_ns())
            bus.publish(env, timeout=5.0)

    _settle()
    start = time.perf_counter()
    threads = []
    for p in range(producers):
        t = threading.Thread(target=produce, args=(per_producer_envelopes[p],))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    drained = bus.shutdown(timeout=60.0)
    elapsed = time.perf_counter() - start
    delivered = count["n"]

    return {
        "language": "python",
        "config": config,
        "producers": producers,
        "concurrency": producers,
        "channels": 1,
        "total": TOTAL,
        "delivered": delivered,
        "elapsed_ms": round(elapsed * 1000),
        "throughput_eps": TOTAL / elapsed,
        **_compute_latency_stats(latencies[:delivered]),
        "drained": drained,
        **_version_fields(),
    }


def run_independent_channels(producers: int) -> dict:
    """producers threads, each with its OWN channel and OWN dedicated
    counter - the "chan" config. No shared lock/counter between producers
    at all (an earlier ad-hoc version of this shared one counter/lock
    across all "independent" channels by mistake and undermeasured the
    result - this is the fix)."""
    bus = SEDABus(workers=producers)
    bus.start()
    counts = [0] * producers
    locks = [threading.Lock() for _ in range(producers)]  # one per channel, never shared
    latencies: list[list[float]] = []  # one list per channel, never shared
    per_channel = TOTAL // producers
    remainder = TOTAL - per_channel * producers

    for c in range(producers):
        name = f"bench{c}"
        n = per_channel + (remainder if c == 0 else 0)
        latencies.append([0.0] * n)
        bus.channel(name, capacity=n, concurrency=1)

        def make_consumer(idx):
            def consumer(env):
                t1 = _now_ns()
                t0 = envelope_payload(env)
                with locks[idx]:  # uncontended: only this channel's own drain touches it
                    slot = counts[idx]
                    counts[idx] += 1
                latencies[idx][slot] = (t1 - t0) / 1000.0
                return True

            return consumer

        bus.subscribe(name, make_consumer(c))

    # Pre-build every envelope before the timed window starts - see the
    # comment in run_shared.
    per_channel_envelopes = []
    for c in range(producers):
        name = f"bench{c}"
        n = per_channel + (remainder if c == 0 else 0)
        per_channel_envelopes.append([make_envelope(name, 0) for _ in range(n)])

    def produce(envs):
        for env in envs:
            set_payload(env, _now_ns())
            bus.publish(env, timeout=5.0)

    _settle()
    start = time.perf_counter()
    threads = []
    for c in range(producers):
        t = threading.Thread(target=produce, args=(per_channel_envelopes[c],))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    drained = bus.shutdown(timeout=60.0)
    elapsed = time.perf_counter() - start

    all_latencies: list[float] = []
    for c in range(producers):
        all_latencies.extend(latencies[c][: counts[c]])

    return {
        "language": "python",
        "config": "chan",
        "producers": producers,
        "concurrency": 1,
        "channels": producers,
        "total": TOTAL,
        "delivered": sum(counts),
        "elapsed_ms": round(elapsed * 1000),
        "throughput_eps": TOTAL / elapsed,
        **_compute_latency_stats(all_latencies),
        "drained": drained,
        **_version_fields(),
    }


def main():
    cores = os.cpu_count() or 4
    par = max(1, min(8, cores))

    for trial in range(1, TRIALS + 1):
        r = run_shared("seq", 1)
        r["trial"] = trial
        print(json.dumps(r))
    for trial in range(1, TRIALS + 1):
        r = run_shared("par", par)
        r["trial"] = trial
        print(json.dumps(r))
    for trial in range(1, TRIALS + 1):
        r = run_independent_channels(par)
        r["trial"] = trial
        print(json.dumps(r))


if __name__ == "__main__":
    main()
