"""Benchmark harness. See ../WORKLOAD.md for the specification this
implements — every bench/<lang> program follows the same shape.

Imports seda_bus/ra_common directly off their src/ trees (sys.path, not an
installed package) so this needs no venv/build step — just python3.13+.
"""

import gc
import json
import math
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "seda-bus-python", "src"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "..", "common", "ra-common-python", "src"))

from seda_bus import Backpressure, SEDABus, make_envelope  # noqa: E402
from seda_bus.envelope import envelope_payload, set_payload  # noqa: E402

TOTAL = 200_000
TRIALS = 3

# -- capacity curve (primary benchmark; see ../WORKLOAD.md) --------------
CAP_CAPACITY = 1024
CALIBRATION_DURATION_S = 2.0
CALIBRATION_TRIALS = 2
# Generous vs. this port's own known throughput ceiling (RESULTS.md: well
# under 200k eps total even at its fastest, free-threaded, config) - a
# calibration burst has no prior rate estimate to size a pool against, so
# this is a fixed constant rather than the sweep's rate-derived formula.
CALIBRATION_POOL_PER_PRODUCER = 200_000
SWEEP_DURATION_S = 4.0
SWEEP_TRIALS = 2
LOAD_FRACTIONS = (0.5, 1.0, 1.5)
TICK_S = 0.02
# Hard ceiling on one sweep trial's TOTAL pre-built pool, across all its
# producers combined - the rate-derived formula below grows unboundedly
# with target_rate_eps, which for a fast config at high load and several
# producers can ask for millions of Envelopes per producer (an uncapped
# version of this same formula OOM-killed seda-bus-go's equivalent
# benchmark outright; capped here before ever running it).
MAX_SWEEP_POOL_TOTAL = 400_000

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


def _make_capacity_bus(producers: int):
    """Fresh bus + one 'bench' channel: real Capacity=1024, Backpressure=Block
    - the opposite of run_shared's "capacity large enough to never engage"
    choice. See ../WORKLOAD.md's "The capacity curve" section."""
    bus = SEDABus(workers=producers)
    bus.start()
    lock = threading.Lock()
    latencies: list[float] = []
    delivered = {"n": 0}

    def consumer(env):
        t1 = _now_ns()
        t0 = envelope_payload(env)
        with lock:
            latencies.append((t1 - t0) / 1000.0)
            delivered["n"] += 1
        return True

    bus.channel(
        "bench",
        capacity=CAP_CAPACITY,
        concurrency=producers,
        backpressure=Backpressure.BLOCK,
    )
    bus.subscribe("bench", consumer)
    return bus, latencies, delivered


def _run_timed_window(producers: int, duration_s: float, pool_per_producer: list[int], per_tick) -> dict:
    """Shared machinery for both Step 1 (calibration, per_tick=None -> publish
    flat-out) and Step 2 (sweep, per_tick(producer_idx) -> ticked pacing)."""
    bus, latencies, delivered = _make_capacity_bus(producers)
    pools = [[make_envelope("bench", 0) for _ in range(pool_per_producer[p])] for p in range(producers)]
    published = [0] * producers

    def produce_flatout(idx, envs, deadline):
        n = len(envs)
        i = 0
        while i < n and time.perf_counter() < deadline:
            set_payload(envs[i], _now_ns())
            bus.publish(envs[i], timeout=30.0)
            i += 1
        published[idx] = i

    def produce_ticked(idx, envs, deadline, tick_size):
        n = len(envs)
        i = 0
        while i < n and time.perf_counter() < deadline:
            tick_start = time.perf_counter()
            batch_end = min(i + tick_size, n)
            while i < batch_end and time.perf_counter() < deadline:
                set_payload(envs[i], _now_ns())
                bus.publish(envs[i], timeout=30.0)
                i += 1
            tick_elapsed = time.perf_counter() - tick_start
            if tick_elapsed < TICK_S:
                time.sleep(TICK_S - tick_elapsed)
        published[idx] = i

    _settle()
    start = time.perf_counter()
    deadline = start + duration_s
    threads = []
    for p in range(producers):
        if per_tick is None:
            t = threading.Thread(target=produce_flatout, args=(p, pools[p], deadline))
        else:
            t = threading.Thread(target=produce_ticked, args=(p, pools[p], deadline, per_tick[p]))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    window_elapsed = time.perf_counter() - start
    end_of_window_depth = bus.stats()["bench"]["depth"]

    drain_start = time.perf_counter()
    drained = bus.shutdown(timeout=60.0)
    drain_tail_ms = round((time.perf_counter() - drain_start) * 1000)

    total_published = sum(published)
    return {
        "total": total_published,
        "delivered": delivered["n"],
        "elapsed_s": window_elapsed,
        "throughput_eps": total_published / window_elapsed,
        "end_of_window_depth": end_of_window_depth,
        "drain_tail_ms": drain_tail_ms,
        "drained": drained,
        "latencies": latencies or [0.0],
    }


def _capacity_row(config: str, producers: int, trial: int, load_fraction: float, target_rate_eps: float, r: dict) -> dict:
    return {
        "language": "python",
        "config": config,
        "trial": trial,
        "producers": producers,
        "concurrency": producers,
        "channels": 1,
        "capacity": CAP_CAPACITY,
        "load_fraction": load_fraction,
        "target_rate_eps": round(target_rate_eps),
        "total": r["total"],
        "delivered": r["delivered"],
        "elapsed_ms": round(r["elapsed_s"] * 1000),
        "throughput_eps": r["throughput_eps"],
        "end_of_window_depth": r["end_of_window_depth"],
        "drain_tail_ms": r["drain_tail_ms"],
        **_compute_latency_stats(r["latencies"]),
        "drained": r["drained"],
        **_version_fields(),
    }


def run_capacity_curve(config: str, producers: int) -> list[dict]:
    """Step 1 (calibrate this stage's own sustained capacity) then Step 2
    (sweep load_fraction in [0.5, 1.0, 1.5]) - see ../WORKLOAD.md."""
    rows = []

    calib_results = []
    # CALIBRATION_POOL_PER_PRODUCER is sized for producers=1 (cap1); for
    # cap8 the same flat constant per producer means 8x the total
    # allocation - divide by producers so the TOTAL across a trial's
    # producers stays bounded, matching every other language's
    # already-producer-count-aware calibration sizing (this hasn't crashed
    # Python yet, only found and fixed after it crashed Node's much
    # tighter default heap limit for the identical flat-constant pattern,
    # but the same unbounded-with-producer-count risk applies here too).
    calib_pool_per_producer = max(2_000, CALIBRATION_POOL_PER_PRODUCER // producers)
    calib_pool = [calib_pool_per_producer] * producers
    for trial in range(1, CALIBRATION_TRIALS + 1):
        r = _run_timed_window(producers, CALIBRATION_DURATION_S, calib_pool, per_tick=None)
        calib_results.append(r)
        rows.append(_capacity_row(config, producers, trial, 0.0, 0.0, r))

    max_throughput_eps = max(r["throughput_eps"] for r in calib_results)

    for load_fraction in LOAD_FRACTIONS:
        target_rate_eps = max_throughput_eps * load_fraction
        rate_per_producer = target_rate_eps / producers
        pool_size = max(1, math.ceil(rate_per_producer * SWEEP_DURATION_S * 1.5))
        pool_size = min(pool_size, MAX_SWEEP_POOL_TOTAL // producers)
        pool = [pool_size] * producers
        per_tick = [max(1, round(rate_per_producer * TICK_S))] * producers
        for trial in range(1, SWEEP_TRIALS + 1):
            r = _run_timed_window(producers, SWEEP_DURATION_S, pool, per_tick=per_tick)
            rows.append(_capacity_row(config, producers, trial, load_fraction, target_rate_eps, r))

    return rows


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

    for r in run_capacity_curve("cap1", 1):
        print(json.dumps(r))
    for r in run_capacity_curve("cap8", par):
        print(json.dumps(r))


if __name__ == "__main__":
    main()
