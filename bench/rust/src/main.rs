//! Benchmark harness. See ../../WORKLOAD.md for the specification this
//! implements — every bench/<lang> program follows the same shape.

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use seda_bus::ra_common::serde_json::Value;
use seda_bus::{envelope_payload, make_envelope, set_payload, Backpressure, Bus, ChannelConfig, Envelope};

const TOTAL: usize = 200_000;
const TRIALS: usize = 3;

// -- capacity curve (primary benchmark; see ../../WORKLOAD.md) -----------

const CAP_CAPACITY: usize = 1024;
const CAP_CALIBRATION_SECS: f64 = 2.0;
const CAP_CALIBRATION_TRIALS: usize = 2;
const CAP_SWEEP_SECS: f64 = 4.0;
const CAP_SWEEP_TRIALS: usize = 2;
const CAP_LOAD_FRACTIONS: [f64; 3] = [0.5, 1.0, 1.5];
const CAP_TICK_MS: u64 = 20;
// Hard ceiling on one window's TOTAL pre-built envelope pool, across all
// its producers combined - see run_capacity_window's own comment on why.
const CAP_MAX_POOL_TOTAL: usize = 400_000;

// Pre-building TOTAL envelopes is itself a burst of allocation right before
// the timed window starts; without a settle pause the allocator/OS memory
// state that burst leaves behind can bleed into the first few timed
// publishes (this was caught happening - inconsistently, across several
// languages - the first time this benchmark measured envelope construction
// separately from dispatch). See ../../WORKLOAD.md.
const SETTLE: Duration = Duration::from_millis(200);

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
            let t0 = envelope_payload(env).and_then(Value::as_u64).unwrap();
            let idx = count.fetch_add(1, Ordering::Relaxed) as usize;
            latencies[idx].store(t1.saturating_sub(t0), Ordering::Relaxed);
            true
        });
    }

    let per_producer = TOTAL / producers;
    let remainder = TOTAL - per_producer * producers;

    // Pre-build every envelope before the timed window starts - this
    // benchmark measures bus dispatch/queueing overhead, not envelope
    // construction cost. In production the producer already holds a
    // constructed envelope before it ever calls publish(); construction
    // isn't part of what the bus does. See ../../WORKLOAD.md.
    let mut per_producer_envelopes: Vec<Vec<Envelope>> = (0..producers)
        .map(|p| {
            let n = per_producer + if p == 0 { remainder } else { 0 };
            (0..n)
                .map(|_| make_envelope("bench", Some(Value::from(0u64)), []))
                .collect()
        })
        .collect();

    thread::sleep(SETTLE);
    let start = Instant::now();
    let mut handles = Vec::new();
    for p in 0..producers {
        let envs = std::mem::take(&mut per_producer_envelopes[p]);
        let bus = bus.clone();
        handles.push(thread::spawn(move || {
            for mut env in envs {
                set_payload(&mut env, Value::from(now_nanos(ref_t)));
                bus.publish(env, Some(Duration::from_secs(5)));
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
            let t0 = envelope_payload(env).and_then(Value::as_u64).unwrap();
            let idx = count.fetch_add(1, Ordering::Relaxed) as usize; // only this channel's own drain touches it
            lat[idx].store(t1.saturating_sub(t0), Ordering::Relaxed);
            true
        });
    }

    // Pre-build every envelope before the timed window starts - see the
    // comment in `run_shared`.
    let mut per_channel_envelopes: Vec<Vec<Envelope>> = (0..producers)
        .map(|c| {
            let name = format!("bench{c}");
            let n = per_channel + if c == 0 { remainder } else { 0 };
            (0..n)
                .map(|_| make_envelope(name.clone(), Some(Value::from(0u64)), []))
                .collect()
        })
        .collect();

    thread::sleep(SETTLE);
    let start = Instant::now();
    let mut handles = Vec::new();
    for c in 0..producers {
        let envs = std::mem::take(&mut per_channel_envelopes[c]);
        let bus = bus.clone();
        handles.push(thread::spawn(move || {
            for mut env in envs {
                set_payload(&mut env, Value::from(now_nanos(ref_t)));
                bus.publish(env, Some(Duration::from_secs(5)));
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

struct CapResult {
    config: &'static str,
    trial: usize,
    producers: usize,
    concurrency: usize,
    channels: usize,
    capacity: usize,
    load_fraction: f64,
    target_rate_eps: f64,
    total: usize,
    delivered: i64,
    elapsed_ms: u128,
    throughput_eps: f64,
    end_of_window_depth: usize,
    drain_tail_ms: u128,
    latency: LatencyStats,
    drained: bool,
}

fn print_cap_result(r: &CapResult) {
    println!(
        "{{\"language\":\"rust\",\"config\":\"{}\",\"trial\":{},\"producers\":{},\"concurrency\":{},\"channels\":{},\"capacity\":{},\"load_fraction\":{},\"target_rate_eps\":{},\"total\":{},\"delivered\":{},\"elapsed_ms\":{},\"throughput_eps\":{},\"end_of_window_depth\":{},\"drain_tail_ms\":{},\"p50_us\":{},\"p99_us\":{},\"p999_us\":{},\"max_us\":{},\"drained\":{}}}",
        r.config, r.trial, r.producers, r.concurrency, r.channels, r.capacity, r.load_fraction, r.target_rate_eps,
        r.total, r.delivered, r.elapsed_ms, r.throughput_eps, r.end_of_window_depth, r.drain_tail_ms,
        r.latency.p50_us, r.latency.p99_us, r.latency.p999_us, r.latency.max_us, r.drained
    );
}

struct CapWindowRaw {
    published: usize,
    delivered: i64,
    elapsed_ms: u128,
    end_of_window_depth: usize,
    drain_tail_ms: u128,
    samples: Vec<f64>,
    drained: bool,
}

/// One timed window: `producers` threads publishing to a single, really
/// bounded (`CAP_CAPACITY`), `Block`-backpressured channel. `target_rate_eps`
/// is `None` for an unpaced firehose burst (calibration), or `Some(rate)` for
/// a tick-paced sweep at that aggregate rate. See ../../WORKLOAD.md's
/// "capacity curve" section for the exact algorithm this implements.
fn run_capacity_window(producers: usize, target_rate_eps: Option<f64>, duration_secs: f64) -> CapWindowRaw {
    let bus = Bus::new(producers);
    let count = Arc::new(AtomicI64::new(0));
    let ref_t = Instant::now();
    bus.channel(
        "bench",
        ChannelConfig::default()
            .capacity(CAP_CAPACITY)
            .concurrency(producers)
            .backpressure(Backpressure::Block),
    );

    let per_producer_rate = target_rate_eps.map(|r| r / producers as f64);
    // Pool sizing: generous headroom above what pacing (or, for calibration,
    // an assumed generous upper bound) is expected to need, so construction
    // never falls inside the timed window - but capped at a fixed TOTAL
    // across all of this window's producers combined
    // (CAP_MAX_POOL_TOTAL), not left to grow unboundedly with the target
    // rate. An uncapped version of this formula (and an even larger one for
    // the unpaced calibration branch, assuming a flat 2M eps ceiling
    // regardless of producer count) asked for millions of full Envelope
    // structs per producer for a fast config at high load/producer count -
    // this OOM-killed seda-bus-go's equivalent benchmark outright before it
    // was capped there; fixed the same way here before ever running it. If
    // a producer exhausts its (possibly capped) pool before the window
    // ends, it just stops publishing early - see run_capacity_curve's
    // handling of a trial's real (possibly short) elapsed_ms.
    let pool_per_producer: usize = match per_producer_rate {
        Some(r) => ((r * duration_secs * 1.5).ceil() as usize).max(16),
        None => {
            let max_assumed_aggregate_eps = 2_000_000.0;
            ((max_assumed_aggregate_eps / producers as f64 * duration_secs * 1.5).ceil() as usize).max(16)
        }
    };
    let pool_per_producer = pool_per_producer.min(CAP_MAX_POOL_TOTAL / producers);

    let latencies: Arc<Vec<AtomicU64>> =
        Arc::new((0..pool_per_producer * producers).map(|_| AtomicU64::new(0)).collect());
    {
        let count = Arc::clone(&count);
        let latencies = Arc::clone(&latencies);
        bus.subscribe("bench", move |env: &mut Envelope| {
            let t1 = now_nanos(ref_t);
            let t0 = envelope_payload(env).and_then(Value::as_u64).unwrap();
            let idx = count.fetch_add(1, Ordering::Relaxed) as usize;
            latencies[idx].store(t1.saturating_sub(t0), Ordering::Relaxed);
            true
        });
    }

    // Pre-build every producer's pool before the timed window starts - same
    // reason as the firehose configs (construction cost excluded).
    let mut per_producer_envelopes: Vec<Vec<Envelope>> = (0..producers)
        .map(|_| {
            (0..pool_per_producer)
                .map(|_| make_envelope("bench", Some(Value::from(0u64)), []))
                .collect()
        })
        .collect();

    thread::sleep(SETTLE);

    let published = Arc::new(AtomicI64::new(0));
    let start = Instant::now();
    let deadline = start + Duration::from_secs_f64(duration_secs);
    let tick = Duration::from_millis(CAP_TICK_MS);

    let mut handles = Vec::new();
    for p in 0..producers {
        let envs = std::mem::take(&mut per_producer_envelopes[p]);
        let bus = bus.clone();
        let published = Arc::clone(&published);
        let per_tick: Option<usize> = per_producer_rate
            .map(|r| ((r * (CAP_TICK_MS as f64 / 1000.0)).round() as usize).max(1));
        handles.push(thread::spawn(move || {
            let mut it = envs.into_iter();
            loop {
                if Instant::now() >= deadline {
                    break;
                }
                let tick_start = Instant::now();
                // Unpaced (calibration): publish in modest batches so the
                // deadline is still checked responsively, not the whole pool
                // in one shot.
                let batch = per_tick.unwrap_or(4096);
                let mut n = 0;
                while n < batch {
                    match it.next() {
                        Some(mut env) => {
                            set_payload(&mut env, Value::from(now_nanos(ref_t)));
                            if bus.publish(env, Some(Duration::from_secs(30))) {
                                published.fetch_add(1, Ordering::Relaxed);
                            }
                            n += 1;
                        }
                        None => break, // pool exhausted
                    }
                }
                if n == 0 {
                    break; // pool exhausted, nothing left to publish
                }
                if per_tick.is_some() {
                    let tick_elapsed = tick_start.elapsed();
                    if tick_elapsed < tick {
                        thread::sleep(tick - tick_elapsed);
                    }
                    // else: don't sleep - the channel's Block wait is already
                    // the bottleneck, which is exactly the signal this
                    // benchmark exists to show.
                }
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    let window_elapsed = start.elapsed();
    let end_of_window_depth = bus.get_stats().get("bench").map(|s| s.depth).unwrap_or(0);

    let drain_start = Instant::now();
    let drained = bus.shutdown(Duration::from_secs(60));
    let drain_tail_ms = drain_start.elapsed().as_millis();

    let published_n = published.load(Ordering::Relaxed) as usize;
    let delivered = count.load(Ordering::Relaxed);
    let samples: Vec<f64> = latencies[..delivered as usize]
        .iter()
        .map(|a| a.load(Ordering::Relaxed) as f64 / 1000.0)
        .collect();

    CapWindowRaw {
        published: published_n,
        delivered,
        elapsed_ms: window_elapsed.as_millis(),
        end_of_window_depth,
        drain_tail_ms,
        samples,
        drained,
    }
}

/// Step 1 (calibrate) then Step 2 (sweep [0.5, 1.0, 1.5] x target rate),
/// printing every row per ../../WORKLOAD.md's output contract.
fn run_capacity_curve(config: &'static str, producers: usize) {
    let mut max_throughput_eps = 0.0f64;
    for trial in 1..=CAP_CALIBRATION_TRIALS {
        let w = run_capacity_window(producers, None, CAP_CALIBRATION_SECS);
        let throughput_eps = w.published as f64 / (w.elapsed_ms as f64 / 1000.0);
        if throughput_eps > max_throughput_eps {
            max_throughput_eps = throughput_eps;
        }
        print_cap_result(&CapResult {
            config,
            trial,
            producers,
            concurrency: producers,
            channels: 1,
            capacity: CAP_CAPACITY,
            load_fraction: 0.0,
            target_rate_eps: 0.0,
            total: w.published,
            delivered: w.delivered,
            elapsed_ms: w.elapsed_ms,
            throughput_eps,
            end_of_window_depth: w.end_of_window_depth,
            drain_tail_ms: w.drain_tail_ms,
            latency: compute_latency_stats(w.samples),
            drained: w.drained,
        });
    }

    for &load_fraction in CAP_LOAD_FRACTIONS.iter() {
        let target_rate_eps = max_throughput_eps * load_fraction;
        for trial in 1..=CAP_SWEEP_TRIALS {
            let w = run_capacity_window(producers, Some(target_rate_eps), CAP_SWEEP_SECS);
            let throughput_eps = w.published as f64 / (w.elapsed_ms as f64 / 1000.0);
            print_cap_result(&CapResult {
                config,
                trial,
                producers,
                concurrency: producers,
                channels: 1,
                capacity: CAP_CAPACITY,
                load_fraction,
                target_rate_eps,
                total: w.published,
                delivered: w.delivered,
                elapsed_ms: w.elapsed_ms,
                throughput_eps,
                end_of_window_depth: w.end_of_window_depth,
                drain_tail_ms: w.drain_tail_ms,
                latency: compute_latency_stats(w.samples),
                drained: w.drained,
            });
        }
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

    run_capacity_curve("cap1", 1);
    run_capacity_curve("cap8", par);
}
