//! Benchmark harness. See ../../WORKLOAD.md for the specification this
//! implements — every bench/<lang> program follows the same shape.

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
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
    channels: usize,
    total: usize,
    delivered: i64,
    elapsed_ms: u128,
    throughput_eps: f64,
    drained: bool,
}

fn print_result(r: &RunResult) {
    println!(
        "{{\"language\":\"rust\",\"config\":\"{}\",\"trial\":{},\"producers\":{},\"concurrency\":{},\"channels\":{},\"total\":{},\"delivered\":{},\"elapsed_ms\":{},\"throughput_eps\":{},\"drained\":{}}}",
        r.config, r.trial, r.producers, r.concurrency, r.channels, r.total, r.delivered, r.elapsed_ms, r.throughput_eps, r.drained
    );
}

/// producers threads, all publishing to ONE channel with concurrency=producers
/// - the "seq"/"par" configs.
fn run_shared(config: &'static str, producers: usize) -> RunResult {
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
        channels: 1,
        total: TOTAL,
        delivered: count.load(Ordering::Relaxed),
        elapsed_ms: elapsed.as_millis(),
        throughput_eps: TOTAL as f64 / elapsed.as_secs_f64(),
        drained,
    }
}

/// producers threads, each with its OWN channel and OWN dedicated counter -
/// the "chan" config. No shared lock/counter between producers at all.
fn run_independent_channels(producers: usize) -> RunResult {
    let bus = Bus::new(producers);
    let counts: Vec<Arc<AtomicU64>> = (0..producers).map(|_| Arc::new(AtomicU64::new(0))).collect();
    let per_channel = TOTAL / producers;
    let remainder = TOTAL - per_channel * producers;

    for c in 0..producers {
        let name = format!("bench{c}");
        let n = per_channel + if c == 0 { remainder } else { 0 };
        bus.channel(&name, ChannelConfig::default().capacity(n).concurrency(1));
        let count = Arc::clone(&counts[c]);
        bus.subscribe(&name, move |_env: &mut Envelope| {
            count.fetch_add(1, Ordering::Relaxed); // only this channel's own drain touches it
            true
        });
    }

    let start = Instant::now();
    let mut handles = Vec::new();
    for c in 0..producers {
        let name = format!("bench{c}");
        let n = per_channel + if c == 0 { remainder } else { 0 };
        let bus = bus.clone();
        handles.push(thread::spawn(move || {
            for i in 0..n {
                bus.publish(
                    Envelope::new(name.clone(), (i as u32).to_le_bytes().to_vec()),
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

    let delivered: u64 = counts.iter().map(|c| c.load(Ordering::Relaxed)).sum();

    RunResult {
        config: "chan",
        trial: 0,
        producers,
        concurrency: 1,
        channels: producers,
        total: TOTAL,
        delivered: delivered as i64,
        elapsed_ms: elapsed.as_millis(),
        throughput_eps: TOTAL as f64 / elapsed.as_secs_f64(),
        drained,
    }
}

fn main() {
    let cores = thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    let par = cores.min(8).max(1);

    for trial in 1..=TRIALS {
        let mut r = run_shared("seq", 1);
        r.trial = trial;
        print_result(&r);
    }
    for trial in 1..=TRIALS {
        let mut r = run_shared("par", par);
        r.trial = trial;
        print_result(&r);
    }
    for trial in 1..=TRIALS {
        let mut r = run_independent_channels(par);
        r.trial = trial;
        print_result(&r);
    }
}
