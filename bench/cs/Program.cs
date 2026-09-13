// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Ra.Common;
using Ra.SedaBus;
using static Ra.SedaBus.EnvelopeHelpers;

const int Total = 200_000;
const int Trials = 3;

// Pre-building Total envelopes is itself a burst of allocation right before
// the timed window starts; without a settle pause + explicit GC, a
// collection provoked by that burst can land inside the first few timed
// publishes instead (caught happening - inconsistently, across several
// languages - the first time this benchmark measured envelope construction
// separately from dispatch). See ../WORKLOAD.md.
const int SettleMs = 200;

void Settle()
{
    GC.Collect();
    GC.WaitForPendingFinalizers();
    Thread.Sleep(SettleMs);
}

var jsonOptions = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

// now_ticks: Stopwatch.GetTimestamp() has an arbitrary per-process origin -
// only ever diffed within this process. See ../WORKLOAD.md's "Latency"
// section.
double TicksToUs(long deltaTicks) => deltaTicks * 1_000_000.0 / Stopwatch.Frequency;

// Sorts (consumes) `samples`, computed AFTER the timed window closes so it
// never counts against throughput.
LatencyStats ComputeLatencyStats(double[] samples)
{
    Array.Sort(samples);
    int n = samples.Length;
    return new LatencyStats(
        samples[(int)(0.50 * (n - 1))],
        samples[(int)(0.99 * (n - 1))],
        samples[(int)(0.999 * (n - 1))],
        samples[n - 1]);
}

// producers threads, all publishing to ONE channel with concurrency=producers
// - the "seq"/"par" configs.
Result RunShared(string config, int producers)
{
    var bus = new Bus(producers);
    long count = 0;
    var latencies = new double[Total];
    bus.Channel("bench", new ChannelConfig().WithCapacity(Total).WithConcurrency(producers));
    bus.Subscribe("bench", env =>
    {
        long t1 = Stopwatch.GetTimestamp();
        long t0 = EnvelopePayload(env)!.GetValue<long>();
        long idx = Interlocked.Increment(ref count) - 1;
        latencies[idx] = TicksToUs(t1 - t0);
        return true;
    });

    var timeout = TimeSpan.FromSeconds(5);
    int perProducer = Total / producers;
    int remainder = Total - perProducer * producers;

    // Pre-build every envelope before the timed window starts - this
    // benchmark measures bus dispatch/queueing overhead, not envelope
    // construction cost. In production the producer already holds a
    // constructed envelope before it ever calls publish(). See
    // ../WORKLOAD.md.
    var perProducerEnvelopes = new List<Envelope>[producers];
    for (int p = 0; p < producers; p++)
    {
        int n = perProducer + (p == 0 ? remainder : 0);
        var envs = new List<Envelope>(n);
        for (int i = 0; i < n; i++) envs.Add(MakeEnvelope("bench", 0L));
        perProducerEnvelopes[p] = envs;
    }

    Settle();
    var sw = Stopwatch.StartNew();
    var threads = new List<Thread>();
    for (int p = 0; p < producers; p++)
    {
        var envs = perProducerEnvelopes[p];
        var t = new Thread(() =>
        {
            foreach (var env in envs)
            {
                SetPayload(env, Stopwatch.GetTimestamp());
                bus.Publish(env, timeout);
            }
        });
        threads.Add(t);
        t.Start();
    }
    foreach (var t in threads) t.Join();
    bool drained = bus.Shutdown(TimeSpan.FromSeconds(60));
    sw.Stop();
    long delivered = Interlocked.Read(ref count);
    var stats = ComputeLatencyStats(latencies[..(int)delivered]);

    return new Result("cs", config, 0, producers, producers, 1, Total, delivered,
        sw.ElapsedMilliseconds, Total / sw.Elapsed.TotalSeconds,
        stats.P50Us, stats.P99Us, stats.P999Us, stats.MaxUs, drained);
}

// producers threads, each with its OWN channel and OWN dedicated counter -
// the "chan" config. No shared lock/counter between producers at all.
Result RunIndependentChannels(int producers)
{
    var bus = new Bus(producers);
    var counts = new long[producers];
    var latencies = new double[producers][]; // one array per channel, never shared
    int perChannel = Total / producers;
    int remainder = Total - perChannel * producers;

    for (int c = 0; c < producers; c++)
    {
        string name = $"bench{c}";
        int n = perChannel + (c == 0 ? remainder : 0);
        latencies[c] = new double[n];
        bus.Channel(name, new ChannelConfig().WithCapacity(n).WithConcurrency(1));
        int idx = c;
        bus.Subscribe(name, env =>
        {
            long t1 = Stopwatch.GetTimestamp();
            long t0 = EnvelopePayload(env)!.GetValue<long>();
            latencies[idx][counts[idx]] = TicksToUs(t1 - t0); // safe: concurrency=1
            counts[idx]++; // only this channel's own drain touches it
            return true;
        });
    }

    // Pre-build every envelope before the timed window starts - see the
    // comment in RunShared.
    var perChannelEnvelopes = new List<Envelope>[producers];
    for (int c = 0; c < producers; c++)
    {
        string name = $"bench{c}";
        int n = perChannel + (c == 0 ? remainder : 0);
        var envs = new List<Envelope>(n);
        for (int i = 0; i < n; i++) envs.Add(MakeEnvelope(name, 0L));
        perChannelEnvelopes[c] = envs;
    }

    var timeout = TimeSpan.FromSeconds(5);
    Settle();
    var sw = Stopwatch.StartNew();
    var threads = new List<Thread>();
    for (int c = 0; c < producers; c++)
    {
        var envs = perChannelEnvelopes[c];
        var t = new Thread(() =>
        {
            foreach (var env in envs)
            {
                SetPayload(env, Stopwatch.GetTimestamp());
                bus.Publish(env, timeout);
            }
        });
        threads.Add(t);
        t.Start();
    }
    foreach (var t in threads) t.Join();
    bool drained = bus.Shutdown(TimeSpan.FromSeconds(60));
    sw.Stop();

    long delivered = counts.Sum();
    var allLatencies = new double[delivered];
    long pos = 0;
    for (int c = 0; c < producers; c++)
    {
        Array.Copy(latencies[c], 0, allLatencies, pos, counts[c]);
        pos += counts[c];
    }

    var stats = ComputeLatencyStats(allLatencies);
    return new Result("cs", "chan", 0, producers, 1, producers, Total, delivered,
        sw.ElapsedMilliseconds, Total / sw.Elapsed.TotalSeconds,
        stats.P50Us, stats.P99Us, stats.P999Us, stats.MaxUs, drained);
}

// -- capacity curve (primary benchmark) - see ../WORKLOAD.md ------------

const double SweepDurationSeconds = 4.0;
const double TickSeconds = 0.02;
// Hard ceiling on one sweep trial's TOTAL pre-built pool, across all its
// producers combined - the rate-derived formula below grows unboundedly
// with target_rate_eps, which for a fast config at high load and several
// producers can ask for millions of envelopes per producer (an uncapped
// version of this same formula OOM-killed seda-bus-go's equivalent
// benchmark outright; capped here before ever running it).
const int MaxSweepPoolTotal = 400_000;
const double CalibrationDurationSeconds = 2.0;
const int CalibrationTotalBudget = 1_500_000; // split across producers; see WORKLOAD.md Step 1

// Publishes as fast as possible (no pacing) for CalibrationDurationSeconds -
// WORKLOAD.md Step 1. load_fraction=0/target_rate_eps=0 mark this as a
// calibration row, not a sweep row.
CapResult RunCapacityCalibration(string config, int producers, int concurrency, int trial)
{
    var bus = new Bus(concurrency);
    long count = 0;
    int poolPerProducer = CalibrationTotalBudget / producers;
    var latencies = new double[poolPerProducer * producers + 4096];
    bus.Channel("bench", new ChannelConfig().WithCapacity(1024).WithConcurrency(concurrency).WithBackpressure(Backpressure.Block));
    bus.Subscribe("bench", env =>
    {
        long t1 = Stopwatch.GetTimestamp();
        long t0 = EnvelopePayload(env)!.GetValue<long>();
        long idx = Interlocked.Increment(ref count) - 1;
        if (idx < latencies.Length) latencies[idx] = TicksToUs(t1 - t0);
        return true;
    });

    var timeout = TimeSpan.FromSeconds(30);
    var pools = new List<Envelope>[producers];
    for (int p = 0; p < producers; p++)
    {
        var envs = new List<Envelope>(poolPerProducer);
        for (int i = 0; i < poolPerProducer; i++) envs.Add(MakeEnvelope("bench", 0L));
        pools[p] = envs;
    }

    Settle();
    var sw = Stopwatch.StartNew();
    var deadline = TimeSpan.FromSeconds(CalibrationDurationSeconds);
    long published = 0;
    var threads = new List<Thread>();
    for (int p = 0; p < producers; p++)
    {
        var envs = pools[p];
        threads.Add(new Thread(() =>
        {
            int i = 0;
            while (sw.Elapsed < deadline && i < envs.Count)
            {
                var env = envs[i++];
                SetPayload(env, Stopwatch.GetTimestamp());
                if (bus.Publish(env, timeout)) Interlocked.Increment(ref published);
            }
        }));
    }
    foreach (var t in threads) t.Start();
    foreach (var t in threads) t.Join();
    var windowElapsed = sw.Elapsed;
    int endOfWindowDepth = bus.GetStats().TryGetValue("bench", out var s0) ? s0.Depth : 0;
    var drainSw = Stopwatch.StartNew();
    bool drained = bus.Shutdown(TimeSpan.FromSeconds(60));
    long drainTailMs = drainSw.ElapsedMilliseconds;

    long delivered = Interlocked.Read(ref count);
    var stats = ComputeLatencyStats(latencies[..(int)Math.Min(delivered, latencies.Length)]);

    return new CapResult("cs", config, trial, producers, concurrency, 1, 1024, 0.0, 0.0,
        (int)published, delivered, (long)windowElapsed.TotalMilliseconds,
        published / windowElapsed.TotalSeconds, endOfWindowDepth, drainTailMs,
        stats.P50Us, stats.P99Us, stats.P999Us, stats.MaxUs, drained);
}

// Sweeps one load_fraction against maxThroughputEps (from calibration) -
// WORKLOAD.md Step 2. Each producer paces itself with a tick-based rate
// limiter rather than a naive per-publish sleep.
CapResult RunCapacitySweep(string config, int producers, int concurrency, double loadFraction, double maxThroughputEps, int trial)
{
    double targetRateEps = maxThroughputEps * loadFraction;
    double perProducerRate = targetRateEps / producers;
    int perTick = Math.Max(1, (int)Math.Round(perProducerRate * TickSeconds));
    int poolPerProducer = Math.Max(1, (int)Math.Ceiling(perProducerRate * SweepDurationSeconds * 1.5));
    poolPerProducer = Math.Min(poolPerProducer, MaxSweepPoolTotal / producers);

    var bus = new Bus(concurrency);
    long count = 0;
    var latencies = new double[poolPerProducer * producers + 4096];
    bus.Channel("bench", new ChannelConfig().WithCapacity(1024).WithConcurrency(concurrency).WithBackpressure(Backpressure.Block));
    bus.Subscribe("bench", env =>
    {
        long t1 = Stopwatch.GetTimestamp();
        long t0 = EnvelopePayload(env)!.GetValue<long>();
        long idx = Interlocked.Increment(ref count) - 1;
        if (idx < latencies.Length) latencies[idx] = TicksToUs(t1 - t0);
        return true;
    });

    var timeout = TimeSpan.FromSeconds(30);
    var pools = new List<Envelope>[producers];
    for (int p = 0; p < producers; p++)
    {
        var envs = new List<Envelope>(poolPerProducer);
        for (int i = 0; i < poolPerProducer; i++) envs.Add(MakeEnvelope("bench", 0L));
        pools[p] = envs;
    }

    Settle();
    var sw = Stopwatch.StartNew();
    var windowDeadline = TimeSpan.FromSeconds(SweepDurationSeconds);
    var tick = TimeSpan.FromSeconds(TickSeconds);
    long published = 0;
    var threads = new List<Thread>();
    for (int p = 0; p < producers; p++)
    {
        var envs = pools[p];
        threads.Add(new Thread(() =>
        {
            int i = 0;
            while (sw.Elapsed < windowDeadline && i < envs.Count)
            {
                var tickStart = sw.Elapsed;
                int n = 0;
                while (n < perTick && i < envs.Count && sw.Elapsed < windowDeadline)
                {
                    var env = envs[i++];
                    SetPayload(env, Stopwatch.GetTimestamp());
                    if (bus.Publish(env, timeout)) Interlocked.Increment(ref published);
                    n++;
                }
                var tickElapsed = sw.Elapsed - tickStart;
                var remaining = tick - tickElapsed;
                // else: don't sleep - Block's wait is already the bottleneck,
                // exactly the signal this benchmark exists to show. See
                // ../WORKLOAD.md's tick pseudocode.
                if (remaining > TimeSpan.Zero) Thread.Sleep(remaining);
            }
        }));
    }
    foreach (var t in threads) t.Start();
    foreach (var t in threads) t.Join();
    var windowElapsed = sw.Elapsed;
    int endOfWindowDepth = bus.GetStats().TryGetValue("bench", out var s1) ? s1.Depth : 0;
    var drainSw = Stopwatch.StartNew();
    bool drained = bus.Shutdown(TimeSpan.FromSeconds(60));
    long drainTailMs = drainSw.ElapsedMilliseconds;

    long delivered = Interlocked.Read(ref count);
    var stats = ComputeLatencyStats(latencies[..(int)Math.Min(delivered, latencies.Length)]);

    return new CapResult("cs", config, trial, producers, concurrency, 1, 1024, loadFraction, targetRateEps,
        (int)published, delivered, (long)windowElapsed.TotalMilliseconds,
        published / windowElapsed.TotalSeconds, endOfWindowDepth, drainTailMs,
        stats.P50Us, stats.P99Us, stats.P999Us, stats.MaxUs, drained);
}

void RunCapacityCurve(string config, int producers, int concurrency)
{
    double maxThroughputEps = 0;
    for (int trial = 1; trial <= 2; trial++)
    {
        var r = RunCapacityCalibration(config, producers, concurrency, trial);
        Console.WriteLine(JsonSerializer.Serialize(r, jsonOptions));
        maxThroughputEps = Math.Max(maxThroughputEps, r.ThroughputEps);
    }
    foreach (var loadFraction in new[] { 0.5, 1.0, 1.5 })
    {
        for (int trial = 1; trial <= 2; trial++)
        {
            var r = RunCapacitySweep(config, producers, concurrency, loadFraction, maxThroughputEps, trial);
            Console.WriteLine(JsonSerializer.Serialize(r, jsonOptions));
        }
    }
}

int par = Math.Min(8, Environment.ProcessorCount);
if (par < 1) par = 1;

for (int trial = 1; trial <= Trials; trial++)
{
    var r = RunShared("seq", 1) with { Trial = trial };
    Console.WriteLine(JsonSerializer.Serialize(r, jsonOptions));
}
for (int trial = 1; trial <= Trials; trial++)
{
    var r = RunShared("par", par) with { Trial = trial };
    Console.WriteLine(JsonSerializer.Serialize(r, jsonOptions));
}
for (int trial = 1; trial <= Trials; trial++)
{
    var r = RunIndependentChannels(par) with { Trial = trial };
    Console.WriteLine(JsonSerializer.Serialize(r, jsonOptions));
}

RunCapacityCurve("cap1", 1, 1);
RunCapacityCurve("cap8", par, par);

record LatencyStats(double P50Us, double P99Us, double P999Us, double MaxUs);

record Result(
    string Language,
    string Config,
    int Trial,
    int Producers,
    int Concurrency,
    int Channels,
    int Total,
    long Delivered,
    long ElapsedMs,
    double ThroughputEps,
    [property: JsonPropertyName("p50_us")] double P50Us,
    [property: JsonPropertyName("p99_us")] double P99Us,
    [property: JsonPropertyName("p999_us")] double P999Us,
    [property: JsonPropertyName("max_us")] double MaxUs,
    bool Drained);

record CapResult(
    string Language,
    string Config,
    int Trial,
    int Producers,
    int Concurrency,
    int Channels,
    int Capacity,
    [property: JsonPropertyName("load_fraction")] double LoadFraction,
    [property: JsonPropertyName("target_rate_eps")] double TargetRateEps,
    int Total,
    long Delivered,
    long ElapsedMs,
    double ThroughputEps,
    [property: JsonPropertyName("end_of_window_depth")] int EndOfWindowDepth,
    [property: JsonPropertyName("drain_tail_ms")] long DrainTailMs,
    [property: JsonPropertyName("p50_us")] double P50Us,
    [property: JsonPropertyName("p99_us")] double P99Us,
    [property: JsonPropertyName("p999_us")] double P999Us,
    [property: JsonPropertyName("max_us")] double MaxUs,
    bool Drained);
