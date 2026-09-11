// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.
//
// Plain ESM importing seda-bus-ts's built dist/ output directly — no ts-node/
// tsx needed to *run* this, only to have built seda-bus-ts/ra-common-ts
// beforehand (`npm run build` in each, already done by the Dockerfile).
import os from "node:os";
import { SedaBus, makeEnvelope } from "../../../seda-bus-ts/dist/index.js";
import { envelopePayload } from "../../../seda-bus-ts/dist/envelope.js";

const TOTAL = 200_000;
const TRIALS = 3;

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

  const start = Date.now();
  const tasks = [];
  for (let p = 0; p < producers; p++) {
    const n = perProducer + (p === 0 ? remainder : 0);
    tasks.push(
      (async () => {
        for (let i = 0; i < n; i++) {
          await bus.publish(makeEnvelope("bench", nowUs()), { timeoutMs: 5000 });
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

  const start = Date.now();
  const tasks = [];
  for (let c = 0; c < producers; c++) {
    const name = `bench${c}`;
    const n = perChannel + (c === 0 ? remainder : 0);
    tasks.push(
      (async () => {
        for (let i = 0; i < n; i++) {
          await bus.publish(makeEnvelope(name, nowUs()), { timeoutMs: 5000 });
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
}

main();
