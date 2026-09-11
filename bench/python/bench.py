"""Benchmark harness. See ../WORKLOAD.md for the specification this
implements — every bench/<lang> program follows the same shape.

Imports seda_bus/ra_common directly off their src/ trees (sys.path, not an
installed package) so this needs no venv/build step — just python3.13+.
"""

import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "seda-bus-python", "src"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "..", "common", "ra-common-python", "src"))

from seda_bus import SEDABus, make_envelope  # noqa: E402

TOTAL = 200_000
TRIALS = 3


def run_once(config: str, producers: int) -> dict:
    bus = SEDABus(workers=producers)
    bus.start()
    count = {"n": 0}
    lock = threading.Lock()

    bus.channel("bench", capacity=TOTAL, concurrency=producers)

    def consumer(_env):
        with lock:
            count["n"] += 1
        return True

    bus.subscribe("bench", consumer)

    per_producer = TOTAL // producers
    remainder = TOTAL - per_producer * producers

    def produce(n):
        for i in range(n):
            bus.publish(make_envelope("bench", i), timeout=5.0)

    start = time.perf_counter()
    threads = []
    for p in range(producers):
        n = per_producer + (remainder if p == 0 else 0)
        t = threading.Thread(target=produce, args=(n,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    drained = bus.shutdown(timeout=60.0)
    elapsed = time.perf_counter() - start

    return {
        "language": "python",
        "config": config,
        "producers": producers,
        "concurrency": producers,
        "total": TOTAL,
        "delivered": count["n"],
        "elapsed_ms": round(elapsed * 1000),
        "throughput_eps": TOTAL / elapsed,
        "drained": drained,
        "python_version": sys.version.split()[0],
        "gil_enabled": bool(getattr(sys, "_is_gil_enabled", lambda: True)()),
    }


def main():
    cores = os.cpu_count() or 4
    par = max(1, min(8, cores))
    configs = [("seq", 1), ("par", par)]

    for name, producers in configs:
        for trial in range(1, TRIALS + 1):
            r = run_once(name, producers)
            r["trial"] = trial
            print(json.dumps(r))


if __name__ == "__main__":
    main()
