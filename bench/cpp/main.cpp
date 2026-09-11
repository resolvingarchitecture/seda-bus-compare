// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "seda_bus/seda_bus.hpp"

using namespace ra::seda_bus;
using namespace std::chrono;

constexpr int kTotal = 200000;
constexpr int kTrials = 3;

// Monotonic-clock ticks in nanoseconds, arbitrary per-process origin - only
// ever diffed within this process. See ../WORKLOAD.md's "Latency" section.
inline int64_t NowNanos() { return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count(); }

struct LatencyStats {
    double p50_us, p99_us, p999_us, max_us;
};

// Sorts (consumes) `samples`, computed AFTER the timed window closes so it
// never counts against throughput.
LatencyStats ComputeLatencyStats(std::vector<double>& samples) {
    std::sort(samples.begin(), samples.end());
    size_t n = samples.size();
    auto at = [&](double p) { return samples[static_cast<size_t>(p * (n - 1))]; };
    return LatencyStats{at(0.50), at(0.99), at(0.999), samples[n - 1]};
}

struct RunResult {
    const char* config;
    int trial;
    int producers;
    int concurrency;
    int channels;
    int total;
    long delivered;
    long elapsed_ms;
    double throughput_eps;
    LatencyStats latency;
    bool drained;
};

void PrintResult(const RunResult& r) {
    std::printf(
        "{\"language\":\"cpp\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
        "\"channels\":%d,\"total\":%d,\"delivered\":%ld,\"elapsed_ms\":%ld,\"throughput_eps\":%f,"
        "\"p50_us\":%f,\"p99_us\":%f,\"p999_us\":%f,\"max_us\":%f,\"drained\":%s}\n",
        r.config, r.trial, r.producers, r.concurrency, r.channels, r.total, r.delivered, r.elapsed_ms,
        r.throughput_eps, r.latency.p50_us, r.latency.p99_us, r.latency.p999_us, r.latency.max_us,
        r.drained ? "true" : "false");
}

// producers threads, all publishing to ONE channel with concurrency=producers
// - the "seq"/"par" configs.
RunResult RunShared(const char* config, int producers) {
    Bus bus(producers);
    std::atomic<long> count{0};
    std::vector<double> latencies(kTotal);
    bus.Channel("bench", ChannelConfig{}.Capacity(kTotal).Concurrency(producers));
    bus.Subscribe("bench", [&count, &latencies](Envelope& env) {
        int64_t t1 = NowNanos();
        int64_t t0 = EnvelopePayload(env).get<int64_t>();
        long idx = count.fetch_add(1, std::memory_order_relaxed);
        latencies[idx] = (t1 - t0) / 1000.0;
        return true;
    });

    int per_producer = kTotal / producers;
    int remainder = kTotal - per_producer * producers;

    auto start = steady_clock::now();
    std::vector<std::thread> threads;
    for (int p = 0; p < producers; p++) {
        int n = per_producer + (p == 0 ? remainder : 0);
        threads.emplace_back([&bus, n] {
            for (int i = 0; i < n; i++) {
                bus.Publish(MakeEnvelope("bench", NowNanos()), milliseconds(5000));
            }
        });
    }
    for (auto& t : threads) t.join();
    bool drained = bus.Shutdown(milliseconds(60000));
    auto elapsed = steady_clock::now() - start;
    long elapsed_ms = duration_cast<milliseconds>(elapsed).count();
    double elapsed_s = duration_cast<duration<double>>(elapsed).count();
    long delivered = count.load();
    latencies.resize(delivered);

    return RunResult{config,       0,        producers,  producers, 1,
                      kTotal,      delivered, elapsed_ms, kTotal / elapsed_s,
                      ComputeLatencyStats(latencies), drained};
}

// producers threads, each with its OWN channel and OWN dedicated counter -
// the "chan" config. No shared lock/counter between producers at all.
RunResult RunIndependentChannels(int producers) {
    Bus bus(producers);
    std::vector<std::unique_ptr<std::atomic<long>>> counts;
    std::vector<std::vector<double>> latencies;  // one array per channel, never shared
    int per_channel = kTotal / producers;
    int remainder = kTotal - per_channel * producers;
    for (int i = 0; i < producers; i++) {
        counts.push_back(std::make_unique<std::atomic<long>>(0));
        latencies.emplace_back(per_channel + (i == 0 ? remainder : 0));
    }

    for (int c = 0; c < producers; c++) {
        std::string name = "bench" + std::to_string(c);
        int n = per_channel + (c == 0 ? remainder : 0);
        bus.Channel(name, ChannelConfig{}.Capacity(n).Concurrency(1));
        std::atomic<long>* count = counts[c].get();
        std::vector<double>* lat = &latencies[c];
        bus.Subscribe(name, [count, lat](Envelope& env) {
            int64_t t1 = NowNanos();
            int64_t t0 = EnvelopePayload(env).get<int64_t>();
            long idx = count->fetch_add(1, std::memory_order_relaxed);  // only this channel's own drain touches it
            (*lat)[idx] = (t1 - t0) / 1000.0;
            return true;
        });
    }

    auto start = steady_clock::now();
    std::vector<std::thread> threads;
    for (int c = 0; c < producers; c++) {
        std::string name = "bench" + std::to_string(c);
        int n = per_channel + (c == 0 ? remainder : 0);
        threads.emplace_back([&bus, name, n] {
            for (int i = 0; i < n; i++) {
                bus.Publish(MakeEnvelope(name, NowNanos()), milliseconds(5000));
            }
        });
    }
    for (auto& t : threads) t.join();
    bool drained = bus.Shutdown(milliseconds(60000));
    auto elapsed = steady_clock::now() - start;
    long elapsed_ms = duration_cast<milliseconds>(elapsed).count();
    double elapsed_s = duration_cast<duration<double>>(elapsed).count();

    long delivered = 0;
    std::vector<double> all_latencies;
    for (int c = 0; c < producers; c++) {
        long d = counts[c]->load(std::memory_order_relaxed);
        delivered += d;
        latencies[c].resize(d);
        all_latencies.insert(all_latencies.end(), latencies[c].begin(), latencies[c].end());
    }

    return RunResult{"chan",  0,        producers,  1, producers,
                      kTotal, delivered, elapsed_ms, kTotal / elapsed_s,
                      ComputeLatencyStats(all_latencies), drained};
}

int main() {
    unsigned cores = std::thread::hardware_concurrency();
    int par = static_cast<int>(cores == 0 ? 4 : cores);
    if (par > 8) par = 8;
    if (par < 1) par = 1;

    for (int trial = 1; trial <= kTrials; trial++) {
        RunResult r = RunShared("seq", 1);
        r.trial = trial;
        PrintResult(r);
    }
    for (int trial = 1; trial <= kTrials; trial++) {
        RunResult r = RunShared("par", par);
        r.trial = trial;
        PrintResult(r);
    }
    for (int trial = 1; trial <= kTrials; trial++) {
        RunResult r = RunIndependentChannels(par);
        r.trial = trial;
        PrintResult(r);
    }
}
