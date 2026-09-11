// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.
//
// Plain ESM importing seda-bus-ts's built dist/ output directly — no ts-node/
// tsx needed to *run* this, only to have built seda-bus-ts/ra-common-ts
// beforehand (`npm run build` in each, already done by the Dockerfile).
import os from "node:os";
import { SedaBus, makeEnvelope } from "../../../seda-bus-ts/dist/index.js";

const TOTAL = 200_000;
const TRIALS = 3;

// producers concurrently-awaited async loops, all publishing to ONE channel
// with concurrency=producers - the "seq"/"par" configs. Node is
// single-threaded, so "producers" here means concurrent event-loop tasks,
// not OS threads - see WORKLOAD.md / RESULTS.md.
async function runShared(config, producers) {
  const bus = new SedaBus({ concurrency: producers });
  let count = 0;
  bus.channel("bench", { capacity: TOTAL, concurrency: producers });
  bus.subscribe("bench", () => {
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
          await bus.publish(makeEnvelope("bench", i), { timeoutMs: 5000 });
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
  const perChannel = Math.floor(TOTAL / producers);
  const remainder = TOTAL - perChannel * producers;

  for (let c = 0; c < producers; c++) {
    const name = `bench${c}`;
    const n = perChannel + (c === 0 ? remainder : 0);
    bus.channel(name, { capacity: n, concurrency: 1 });
    bus.subscribe(name, () => {
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
          await bus.publish(makeEnvelope(name, i), { timeoutMs: 5000 });
        }
      })(),
    );
  }
  await Promise.all(tasks);
  const drained = await bus.shutdown({ timeoutMs: 60_000 });
  const elapsedMs = Date.now() - start;

  const delivered = counts.reduce((a, b) => a + b, 0);

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
