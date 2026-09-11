//! Benchmark harness. See ../../WORKLOAD.md for the specification this
//! implements — every bench/<lang> program follows the same shape.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use seda_bus::{Bus, ChannelConfig, Envelope};

const TOTAL: usize = 200_000;
const TRIALS: usize = 3;

struct RunResult {
    config: &'static str,
    trial: usize,
    producers: usize,
    concurrency: usize,
    total: usize,
    delivered: i64,
    elapsed_ms: u128,
    throughput_eps: f64,
    drained: bool,
}

fn run_once(config: &'static str, producers: usize) -> RunResult {
    let bus = Bus::new(producers);
    let count = Arc::new(AtomicI64::new(0));
    bus.channel(
        "bench",
        ChannelConfig::default().capacity(TOTAL).concurrency(producers),
    );
    {
        let count = Arc::clone(&count);
        bus.subscribe("bench", move |_env: &mut Envelope| {
            count.fetch_add(1, Ordering::Relaxed);
            true
        });
    }

    let per_producer = TOTAL / producers;
    let remainder = TOTAL - per_producer * producers;

    let start = Instant::now();
    let mut handles = Vec::new();
    for p in 0..producers {
        let n = per_producer + if p == 0 { remainder } else { 0 };
        let bus = bus.clone();
        handles.push(thread::spawn(move || {
            for i in 0..n {
                bus.publish(
                    Envelope::new("bench", (i as u32).to_le_bytes().to_vec()),
                    Some(Duration::from_secs(5)),
                );
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    let drained = bus.shutdown(Duration::from_secs(60));
    let elapsed = start.elapsed();

    RunResult {
        config,
        trial: 0,
        producers,
        concurrency: producers,
        total: TOTAL,
        delivered: count.load(Ordering::Relaxed),
        elapsed_ms: elapsed.as_millis(),
        throughput_eps: TOTAL as f64 / elapsed.as_secs_f64(),
        drained,
    }
}

fn main() {
    let cores = thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    let par = cores.min(8).max(1);

    let configs: [(&'static str, usize); 2] = [("seq", 1), ("par", par)];

    for (name, producers) in configs {
        for trial in 1..=TRIALS {
            let mut r = run_once(name, producers);
            r.trial = trial;
            println!(
                "{{\"language\":\"rust\",\"config\":\"{}\",\"trial\":{},\"producers\":{},\"concurrency\":{},\"total\":{},\"delivered\":{},\"elapsed_ms\":{},\"throughput_eps\":{},\"drained\":{}}}",
                r.config, r.trial, r.producers, r.concurrency, r.total, r.delivered, r.elapsed_ms, r.throughput_eps, r.drained
            );
        }
    }
}
