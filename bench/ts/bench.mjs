// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.
//
// Plain ESM importing seda-bus-ts's built dist/ output directly — no ts-node/
// tsx needed to *run* this, only to have built seda-bus-ts/ra-common-ts
// beforehand (`npm run build` in each, already done by the Dockerfile).
import os from "node:os";
import { SedaBus, Backpressure, makeEnvelope } from "../../../seda-bus-ts/dist/index.js";
import { envelopePayload } from "../../../seda-bus-ts/dist/envelope.js";

const TOTAL = 200_000;
const TRIALS = 3;

// Pre-building TOTAL envelopes is itself a burst of allocation right before
// the timed window starts; without a settle pause, a GC pass provoked by
// that burst can land inside the first few timed publishes instead (caught
// happening - inconsistently, across several languages - the first time
// this benchmark measured envelope construction separately from dispatch).
// Node doesn't expose a manual GC trigger without --expose-gc, so this is a
// settle delay only, not an explicit collection. See ../WORKLOAD.md.
const SETTLE_MS = 200;
const settle = () => new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// -- capacity curve (see ../WORKLOAD.md's "The capacity curve") ----------
const CAP_CAPACITY = 1024;
const CALIBRATION_MS = 2000;
const CALIBRATION_TRIALS = 2;
// The spec's pool-sizing formula (target_rate * duration * 1.5) only
// covers the paced sweep, which needs a known target rate. Calibration
// (Step 1) is deliberately unpaced/unbounded - its whole point is
// measuring a rate we don't know yet - so its own pool size is a
// generous fixed guess instead: comfortably above any realistic
// bounded-capacity (1024), Block-backpressured single-channel throughput
// this event-loop-based port could sustain (this report's own prior
// firehose numbers for `par`/`chan` topped out in the low hundreds of
// thousands eps on an *unbounded* queue - this is bounded, so lower still).
const CALIBRATION_POOL_PER_PRODUCER = 400_000;
const SWEEP_MS = 4000;
const SWEEP_TRIALS = 2;
const LOAD_FRACTIONS = [0.5, 1.0, 1.5];
const TICK_MS = 20;
const CAP_PUBLISH_TIMEOUT_MS = 30_000;
// Hard ceiling on one sweep trial's TOTAL pre-built pool, across all its
// producers combined - the rate-derived formula below grows unboundedly
// with target_rate_eps, which for a fast config at high load and several
// producers can ask for millions of envelopes per producer (an uncapped
// version of this same formula OOM-killed seda-bus-go's equivalent
// benchmark outright; capped here before ever running it).
const MAX_SWEEP_POOL_TOTAL = 400_000;

// nowUs: performance.now() has an arbitrary per-process origin (process
// start) - only ever diffed within this process. Already in fractional
// milliseconds; converted to microseconds at read-back. See
// ../WORKLOAD.md's "Latency" section.
function nowUs() {
  return performance.now() * 1000;
}

// Sorts (consumes) samples, computed AFTER the timed window closes so it
// never counts against throughput.
function computeLatencyStats(samples) {
  samples.sort((a, b) => a - b);
  const n = samples.length;
  const at = (p) => samples[Math.floor(p * (n - 1))];
  return { p50_us: at(0.5), p99_us: at(0.99), p999_us: at(0.999), max_us: samples[n - 1] };
}

// producers concurrently-awaited async loops, all publishing to ONE channel
// with concurrency=producers - the "seq"/"par" configs. Node is
// single-threaded, so "producers" here means concurrent event-loop tasks,
// not OS threads - see WORKLOAD.md / RESULTS.md.
async function runShared(config, producers) {
  const bus = new SedaBus({ concurrency: producers });
  let count = 0;
  const latencies = new Array(TOTAL).fill(0);
  bus.channel("bench", { capacity: TOTAL, concurrency: producers });
  bus.subscribe("bench", (env) => {
    const t1 = nowUs();
    const t0 = envelopePayload(env);
    latencies[count] = t1 - t0;
    count++;
    return true;
  });

  const perProducer = Math.floor(TOTAL / producers);
  const remainder = TOTAL - perProducer * producers;

  // Pre-build every envelope before the timed window starts - this
  // benchmark measures bus dispatch/queueing overhead, not envelope
  // construction cost. In production the producer already holds a
  // constructed envelope before it ever calls publish(). See
  // ../WORKLOAD.md.
  const perProducerEnvelopes = [];
  for (let p = 0; p < producers; p++) {
    const n = perProducer + (p === 0 ? remainder : 0);
    const envs = [];
    for (let i = 0; i < n; i++) envs.push(makeEnvelope("bench", 0));
    perProducerEnvelopes.push(envs);
  }

  await settle();
  const start = Date.now();
  const tasks = [];
  for (let p = 0; p < producers; p++) {
    const envs = perProducerEnvelopes[p];
    tasks.push(
      (async () => {
        for (const env of envs) {
          env.addContent(nowUs());
          await bus.publish(env, { timeoutMs: 5000 });
        }
      })(),
    );
  }
  await Promise.all(tasks);
  const drained = await bus.shutdown({ timeoutMs: 60_000 });
  const elapsedMs = Date.now() - start;

  return {
    language: "ts",
    config,
    trial: 0,
    producers,
    concurrency: producers,
    channels: 1,
    total: TOTAL,
    delivered: count,
    elapsed_ms: elapsedMs,
    throughput_eps: TOTAL / (elapsedMs / 1000),
    ...computeLatencyStats(latencies.slice(0, count)),
    drained,
  };
}

// producers concurrent tasks, each with its OWN channel and OWN dedicated
// counter - the "chan" config. No shared lock/counter between producers at
// all (though on Node's single thread there's no real parallelism to gain
// from that regardless - see RESULTS.md).
async function runIndependentChannels(producers) {
  const bus = new SedaBus({ concurrency: producers });
  const counts = new Array(producers).fill(0);
  const latencies = []; // one array per channel, never shared
  const perChannel = Math.floor(TOTAL / producers);
  const remainder = TOTAL - perChannel * producers;

  for (let c = 0; c < producers; c++) {
    const name = `bench${c}`;
    const n = perChannel + (c === 0 ? remainder : 0);
    latencies.push(new Array(n).fill(0));
    bus.channel(name, { capacity: n, concurrency: 1 });
    bus.subscribe(name, (env) => {
      const t1 = nowUs();
      const t0 = envelopePayload(env);
      latencies[c][counts[c]] = t1 - t0;
      counts[c]++; // only this channel's own drain touches it
      return true;
    });
  }

  // Pre-build every envelope before the timed window starts - see the
  // comment in runShared.
  const perChannelEnvelopes = [];
  for (let c = 0; c < producers; c++) {
    const name = `bench${c}`;
    const n = perChannel + (c === 0 ? remainder : 0);
    const envs = [];
    for (let i = 0; i < n; i++) envs.push(makeEnvelope(name, 0));
    perChannelEnvelopes.push(envs);
  }

  await settle();
  const start = Date.now();
  const tasks = [];
  for (let c = 0; c < producers; c++) {
    const envs = perChannelEnvelopes[c];
    tasks.push(
      (async () => {
        for (const env of envs) {
          env.addContent(nowUs());
          await bus.publish(env, { timeoutMs: 5000 });
        }
      })(),
    );
  }
  await Promise.all(tasks);
  const drained = await bus.shutdown({ timeoutMs: 60_000 });
  const elapsedMs = Date.now() - start;

  const delivered = counts.reduce((a, b) => a + b, 0);
  const allLatencies = latencies.flatMap((lat, c) => lat.slice(0, counts[c]));

  return {
    language: "ts",
    config: "chan",
    trial: 0,
    producers,
    concurrency: 1,
    channels: producers,
    total: TOTAL,
    delivered,
    elapsed_ms: elapsedMs,
    throughput_eps: TOTAL / (elapsedMs / 1000),
    ...computeLatencyStats(allLatencies),
    drained,
  };
}

// Tick-based rate limiter for one producer: publishes up to `perTick`
// envelopes back-to-back each tick, then sleeps the tick's remainder - or
// doesn't, if publishing already took the whole tick (Block backpressure
// is the bottleneck), which is exactly the signal this benchmark exists to
// show. targetPerProducerEps <= 0 means unpaced (calibration): publish as
// fast as possible until the pool or the deadline is exhausted.
async function paceProducer(bus, envs, targetPerProducerEps, tickMs, windowDeadline) {
  let i = 0;
  const tickSeconds = tickMs / 1000;
  const perTick = targetPerProducerEps > 0 ? Math.max(1, Math.round(targetPerProducerEps * tickSeconds)) : envs.length;
  while (performance.now() < windowDeadline && i < envs.length) {
    const tickStart = performance.now();
    const tickEnd = Math.min(i + perTick, envs.length);
    while (i < tickEnd && performance.now() < windowDeadline) {
      const env = envs[i++];
      env.addContent(nowUs());
      await bus.publish(env, { timeoutMs: CAP_PUBLISH_TIMEOUT_MS });
    }
    if (targetPerProducerEps > 0) {
      const tickElapsed = performance.now() - tickStart;
      if (tickElapsed < tickMs) await sleep(tickMs - tickElapsed);
    }
  }
  return i;
}

// One capacity-curve trial: `producers` concurrent tasks on ONE `bench`
// channel, capacity=1024, Block backpressure, concurrency=producers.
async function runCapacityTrial({ config, producers, loadFraction, targetRateEps, durationMs, poolPerProducer }) {
  const bus = new SedaBus({ concurrency: producers });
  let count = 0;
  const latencies = [];
  bus.channel("bench", { capacity: CAP_CAPACITY, concurrency: producers, backpressure: Backpressure.Block });
  bus.subscribe("bench", (env) => {
    const t1 = nowUs();
    const t0 = envelopePayload(env);
    latencies.push(t1 - t0);
    count++;
    return true;
  });

  const targetPerProducer = targetRateEps > 0 ? targetRateEps / producers : 0;

  // Pre-build every producer's envelope pool before the timed window
  // starts - construction stays out of scope, same as the firehose
  // configs. See ../WORKLOAD.md's capacity-curve Step 2 for the sweep's
  // pool-sizing formula; calibration's own pool size is a fixed constant
  // (see CALIBRATION_POOL_PER_PRODUCER's comment above).
  const perProducerEnvelopes = [];
  for (let p = 0; p < producers; p++) {
    const envs = [];
    for (let i = 0; i < poolPerProducer; i++) envs.push(makeEnvelope("bench", 0));
    perProducerEnvelopes.push(envs);
  }

  await settle();
  const start = performance.now();
  const windowDeadline = start + durationMs;
  const publishedCounts = await Promise.all(
    perProducerEnvelopes.map((envs) => paceProducer(bus, envs, targetPerProducer, TICK_MS, windowDeadline)),
  );
  const published = publishedCounts.reduce((a, b) => a + b, 0);
  const elapsedMs = performance.now() - start;

  const endOfWindowDepth = bus.stats()["bench"].depth;

  const drainStart = performance.now();
  const drained = await bus.shutdown({ timeoutMs: 60_000 });
  const drainTailMs = performance.now() - drainStart;

  return {
    language: "ts",
    config,
    trial: 0,
    producers,
    concurrency: producers,
    channels: 1,
    capacity: CAP_CAPACITY,
    load_fraction: loadFraction,
    target_rate_eps: Math.round(targetRateEps),
    total: published,
    delivered: count,
    elapsed_ms: Math.round(elapsedMs),
    throughput_eps: published / (elapsedMs / 1000),
    end_of_window_depth: endOfWindowDepth,
    drain_tail_ms: Math.round(drainTailMs),
    ...computeLatencyStats(latencies),
    drained,
  };
}

// Step 1 (calibrate) + Step 2 (sweep [0.5, 1.0, 1.5]) for one producer-count
// configuration ("cap1" or "cap8"). Emits every row itself (calibration
// rows carry load_fraction: 0).
async function runCapacityCurve(configName, producers) {
  let maxThroughputEps = 0;
  for (let trial = 1; trial <= CALIBRATION_TRIALS; trial++) {
    const r = await runCapacityTrial({
      config: configName,
      producers,
      loadFraction: 0,
      targetRateEps: 0,
      durationMs: CALIBRATION_MS,
      poolPerProducer: CALIBRATION_POOL_PER_PRODUCER,
    });
    r.trial = trial;
    console.log(JSON.stringify(r));
    if (r.throughput_eps > maxThroughputEps) maxThroughputEps = r.throughput_eps;
  }

  for (const loadFraction of LOAD_FRACTIONS) {
    const targetRateEps = maxThroughputEps * loadFraction;
    const targetPerProducer = targetRateEps / producers;
    const poolPerProducer = Math.min(
      Math.ceil(targetPerProducer * (SWEEP_MS / 1000) * 1.5),
      Math.floor(MAX_SWEEP_POOL_TOTAL / producers),
    );
    for (let trial = 1; trial <= SWEEP_TRIALS; trial++) {
      const r = await runCapacityTrial({
        config: configName,
        producers,
        loadFraction,
        targetRateEps,
        durationMs: SWEEP_MS,
        poolPerProducer,
      });
      r.trial = trial;
      console.log(JSON.stringify(r));
    }
  }
}

async function main() {
  const cores = typeof os.availableParallelism === "function" ? os.availableParallelism() : os.cpus().length;
  const par = Math.max(1, Math.min(8, cores));

  for (let trial = 1; trial <= TRIALS; trial++) {
    const r = await runShared("seq", 1);
    r.trial = trial;
    console.log(JSON.stringify(r));
  }
  for (let trial = 1; trial <= TRIALS; trial++) {
    const r = await runShared("par", par);
    r.trial = trial;
    console.log(JSON.stringify(r));
  }
  for (let trial = 1; trial <= TRIALS; trial++) {
    const r = await runIndependentChannels(par);
    r.trial = trial;
    console.log(JSON.stringify(r));
  }

  await runCapacityCurve("cap1", 1);
  await runCapacityCurve("cap8", par);
}

main();
