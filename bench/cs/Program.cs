// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

using System.Diagnostics;
using System.Text.Json;
using Ra.SedaBus;
using static Ra.SedaBus.EnvelopeHelpers;

const int Total = 200_000;
const int Trials = 3;

var jsonOptions = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

// producers threads, all publishing to ONE channel with concurrency=producers
// - the "seq"/"par" configs.
Result RunShared(string config, int producers)
{
    var bus = new Bus(producers);
    long count = 0;
    bus.Channel("bench", new ChannelConfig().WithCapacity(Total).WithConcurrency(producers));
    bus.Subscribe("bench", _ =>
    {
        Interlocked.Increment(ref count);
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
            for (int i = 0; i < n; i++) bus.Publish(MakeEnvelope("bench", i), timeout);
        });
        threads.Add(t);
        t.Start();
    }
    foreach (var t in threads) t.Join();
    bool drained = bus.Shutdown(TimeSpan.FromSeconds(60));
    sw.Stop();

    return new Result("cs", config, 0, producers, producers, 1, Total, Interlocked.Read(ref count),
        sw.ElapsedMilliseconds, Total / sw.Elapsed.TotalSeconds, drained);
}

// producers threads, each with its OWN channel and OWN dedicated counter -
// the "chan" config. No shared lock/counter between producers at all.
Result RunIndependentChannels(int producers)
{
    var bus = new Bus(producers);
    var counts = new long[producers];
    int perChannel = Total / producers;
    int remainder = Total - perChannel * producers;

    for (int c = 0; c < producers; c++)
    {
        string name = $"bench{c}";
        int n = perChannel + (c == 0 ? remainder : 0);
        bus.Channel(name, new ChannelConfig().WithCapacity(n).WithConcurrency(1));
        int idx = c;
        bus.Subscribe(name, _ =>
        {
            counts[idx]++; // safe: concurrency=1, only this channel's own drain touches it
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
            for (int i = 0; i < n; i++) bus.Publish(MakeEnvelope(name, i), timeout);
        });
        threads.Add(t);
        t.Start();
    }
    foreach (var t in threads) t.Join();
    bool drained = bus.Shutdown(TimeSpan.FromSeconds(60));
    sw.Stop();

    long delivered = counts.Sum();

    return new Result("cs", "chan", 0, producers, 1, producers, Total, delivered,
        sw.ElapsedMilliseconds, Total / sw.Elapsed.TotalSeconds, drained);
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
    bool Drained);
