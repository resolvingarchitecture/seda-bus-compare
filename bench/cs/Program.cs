// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Ra.SedaBus;
using static Ra.SedaBus.EnvelopeHelpers;

const int Total = 200_000;
const int Trials = 3;

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

    var sw = Stopwatch.StartNew();
    var threads = new List<Thread>();
    for (int p = 0; p < producers; p++)
    {
        int n = perProducer + (p == 0 ? remainder : 0);
        var t = new Thread(() =>
        {
            for (int i = 0; i < n; i++) bus.Publish(MakeEnvelope("bench", Stopwatch.GetTimestamp()), timeout);
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

    var timeout = TimeSpan.FromSeconds(5);
    var sw = Stopwatch.StartNew();
    var threads = new List<Thread>();
    for (int c = 0; c < producers; c++)
    {
        string name = $"bench{c}";
        int n = perChannel + (c == 0 ? remainder : 0);
        var t = new Thread(() =>
        {
            for (int i = 0; i < n; i++) bus.Publish(MakeEnvelope(name, Stopwatch.GetTimestamp()), timeout);
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
