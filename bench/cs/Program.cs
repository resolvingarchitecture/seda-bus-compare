// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

using System.Diagnostics;
using System.Text.Json;
using Ra.SedaBus;
using static Ra.SedaBus.EnvelopeHelpers;

const int Total = 200_000;
const int Trials = 3;

var jsonOptions = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

Result RunOnce(string config, int producers)
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

    return new Result("cs", config, 0, producers, producers, Total, Interlocked.Read(ref count),
        sw.ElapsedMilliseconds, Total / sw.Elapsed.TotalSeconds, drained);
}

int par = Math.Min(8, Environment.ProcessorCount);
if (par < 1) par = 1;
var configs = new (string Name, int Producers)[] { ("seq", 1), ("par", par) };

foreach (var (name, p) in configs)
{
    for (int trial = 1; trial <= Trials; trial++)
    {
        var r = RunOnce(name, p) with { Trial = trial };
        Console.WriteLine(JsonSerializer.Serialize(r, jsonOptions));
    }
}

record Result(
    string Language,
    string Config,
    int Trial,
    int Producers,
    int Concurrency,
    int Total,
    long Delivered,
    long ElapsedMs,
    double ThroughputEps,
    bool Drained);
