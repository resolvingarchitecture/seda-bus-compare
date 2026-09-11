//! Benchmark harness. See ../../WORKLOAD.md for the specification this
//! implements — every bench/<lang> program follows the same shape.

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use seda_bus::{Bus, ChannelConfig, Envelope};

const TOTAL: usize = 200_000;
const TRIALS: usize = 3;

// now_nanos: monotonic ticks relative to an arbitrary per-process origin -
// only ever diffed within this process. See ../../WORKLOAD.md's "Latency"
// section.
fn now_nanos(ref_t: Instant) -> u64 {
    ref_t.elapsed().as_nanos() as u64
}

#[derive(Default, Clone, Copy)]
struct LatencyStats {
    p50_us: f64,
    p99_us: f64,
    p999_us: f64,
    max_us: f64,
}

// Sorts (consumes) `samples`, computed AFTER the timed window closes so it
// never counts against throughput.
fn compute_latency_stats(mut samples: Vec<f64>) -> LatencyStats {
    samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = samples.len();
    let at = |p: f64| samples[(p * (n - 1) as f64) as usize];
    LatencyStats {
        p50_us: at(0.50),
        p99_us: at(0.99),
        p999_us: at(0.999),
        max_us: samples[n - 1],
    }
}

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
    latency: LatencyStats,
    drained: bool,
}

fn print_result(r: &RunResult) {
    println!(
        "{{\"language\":\"rust\",\"config\":\"{}\",\"trial\":{},\"producers\":{},\"concurrency\":{},\"channels\":{},\"total\":{},\"delivered\":{},\"elapsed_ms\":{},\"throughput_eps\":{},\"p50_us\":{},\"p99_us\":{},\"p999_us\":{},\"max_us\":{},\"drained\":{}}}",
        r.config, r.trial, r.producers, r.concurrency, r.channels, r.total, r.delivered, r.elapsed_ms, r.throughput_eps,
        r.latency.p50_us, r.latency.p99_us, r.latency.p999_us, r.latency.max_us, r.drained
    );
}

/// producers threads, all publishing to ONE channel with concurrency=producers
/// - the "seq"/"par" configs.
fn run_shared(config: &'static str, producers: usize) -> RunResult {
    let bus = Bus::new(producers);
    let count = Arc::new(AtomicI64::new(0));
    // Raw nanosecond deltas, not f64 - no AtomicF64 in std, and each index is
    // written by exactly one delivery, so a relaxed atomic store is enough.
    let latencies: Arc<Vec<AtomicU64>> = Arc::new((0..TOTAL).map(|_| AtomicU64::new(0)).collect());
    let ref_t = Instant::now();
    bus.channel(
        "bench",
        ChannelConfig::default().capacity(TOTAL).concurrency(producers),
    );
    {
        let count = Arc::clone(&count);
        let latencies = Arc::clone(&latencies);
        bus.subscribe("bench", move |env: &mut Envelope| {
            let t1 = now_nanos(ref_t);
            let t0 = u64::from_le_bytes(env.payload[..8].try_into().unwrap());
            let idx = count.fetch_add(1, Ordering::Relaxed) as usize;
            latencies[idx].store(t1.saturating_sub(t0), Ordering::Relaxed);
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
            for _ in 0..n {
                bus.publish(
                    Envelope::new("bench", now_nanos(ref_t).to_le_bytes().to_vec()),
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
    let delivered = count.load(Ordering::Relaxed);
    let samples: Vec<f64> = latencies[..delivered as usize]
        .iter()
        .map(|a| a.load(Ordering::Relaxed) as f64 / 1000.0)
        .collect();

    RunResult {
        config,
        trial: 0,
        producers,
        concurrency: producers,
        channels: 1,
        total: TOTAL,
        delivered,
        elapsed_ms: elapsed.as_millis(),
        throughput_eps: TOTAL as f64 / elapsed.as_secs_f64(),
        latency: compute_latency_stats(samples),
        drained,
    }
}

/// producers threads, each with its OWN channel and OWN dedicated counter -
/// the "chan" config. No shared lock/counter between producers at all.
fn run_independent_channels(producers: usize) -> RunResult {
    let bus = Bus::new(producers);
    let counts: Vec<Arc<AtomicU64>> = (0..producers).map(|_| Arc::new(AtomicU64::new(0))).collect();
    let ref_t = Instant::now();
    let per_channel = TOTAL / producers;
    let remainder = TOTAL - per_channel * producers;
    // One latency vec per channel, never shared - same isolation as `counts`.
    let latencies: Vec<Arc<Vec<AtomicU64>>> = (0..producers)
        .map(|c| {
            let n = per_channel + if c == 0 { remainder } else { 0 };
            Arc::new((0..n).map(|_| AtomicU64::new(0)).collect())
        })
        .collect();

    for c in 0..producers {
        let name = format!("bench{c}");
        let n = per_channel + if c == 0 { remainder } else { 0 };
        bus.channel(&name, ChannelConfig::default().capacity(n).concurrency(1));
        let count = Arc::clone(&counts[c]);
        let lat = Arc::clone(&latencies[c]);
        bus.subscribe(&name, move |env: &mut Envelope| {
            let t1 = now_nanos(ref_t);
            let t0 = u64::from_le_bytes(env.payload[..8].try_into().unwrap());
            let idx = count.fetch_add(1, Ordering::Relaxed) as usize; // only this channel's own drain touches it
            lat[idx].store(t1.saturating_sub(t0), Ordering::Relaxed);
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
            for _ in 0..n {
                bus.publish(
                    Envelope::new(name.clone(), now_nanos(ref_t).to_le_bytes().to_vec()),
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

    let mut delivered: u64 = 0;
    let mut samples: Vec<f64> = Vec::with_capacity(TOTAL);
    for c in 0..producers {
        let d = counts[c].load(Ordering::Relaxed);
        delivered += d;
        samples.extend(latencies[c][..d as usize].iter().map(|a| a.load(Ordering::Relaxed) as f64 / 1000.0));
    }

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
        latency: compute_latency_stats(samples),
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
