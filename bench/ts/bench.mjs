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

async function runOnce(config, producers) {
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
    total: TOTAL,
    delivered: count,
    elapsed_ms: elapsedMs,
    throughput_eps: TOTAL / (elapsedMs / 1000),
    drained,
  };
}

async function main() {
  const cores = typeof os.availableParallelism === "function" ? os.availableParallelism() : os.cpus().length;
  const par = Math.max(1, Math.min(8, cores));
  const configs = [
    ["seq", 1],
    ["par", par],
  ];

  for (const [name, producers] of configs) {
    for (let trial = 1; trial <= TRIALS; trial++) {
      const r = await runOnce(name, producers);
      r.trial = trial;
      console.log(JSON.stringify(r));
    }
  }
}

main();
