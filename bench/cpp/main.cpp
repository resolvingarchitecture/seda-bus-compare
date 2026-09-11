// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

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
    bool drained;
};

void PrintResult(const RunResult& r) {
    std::printf(
        "{\"language\":\"cpp\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
        "\"channels\":%d,\"total\":%d,\"delivered\":%ld,\"elapsed_ms\":%ld,\"throughput_eps\":%f,\"drained\":%s}\n",
        r.config, r.trial, r.producers, r.concurrency, r.channels, r.total, r.delivered, r.elapsed_ms,
        r.throughput_eps, r.drained ? "true" : "false");
}

// producers threads, all publishing to ONE channel with concurrency=producers
// - the "seq"/"par" configs.
RunResult RunShared(const char* config, int producers) {
    Bus bus(producers);
    std::atomic<long> count{0};
    bus.Channel("bench", ChannelConfig{}.Capacity(kTotal).Concurrency(producers));
    bus.Subscribe("bench", [&count](Envelope&) {
        count.fetch_add(1, std::memory_order_relaxed);
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
                bus.Publish(MakeEnvelope("bench", i), milliseconds(5000));
            }
        });
    }
    for (auto& t : threads) t.join();
    bool drained = bus.Shutdown(milliseconds(60000));
    auto elapsed = steady_clock::now() - start;
    long elapsed_ms = duration_cast<milliseconds>(elapsed).count();
    double elapsed_s = duration_cast<duration<double>>(elapsed).count();

    return RunResult{config, 0, producers, producers, 1, kTotal, count.load(), elapsed_ms, kTotal / elapsed_s, drained};
}

// producers threads, each with its OWN channel and OWN dedicated counter -
// the "chan" config. No shared lock/counter between producers at all.
RunResult RunIndependentChannels(int producers) {
    Bus bus(producers);
    std::vector<std::unique_ptr<std::atomic<long>>> counts;
    for (int i = 0; i < producers; i++) counts.push_back(std::make_unique<std::atomic<long>>(0));

    int per_channel = kTotal / producers;
    int remainder = kTotal - per_channel * producers;

    for (int c = 0; c < producers; c++) {
        std::string name = "bench" + std::to_string(c);
        int n = per_channel + (c == 0 ? remainder : 0);
        bus.Channel(name, ChannelConfig{}.Capacity(n).Concurrency(1));
        std::atomic<long>* count = counts[c].get();
        bus.Subscribe(name, [count](Envelope&) {
            count->fetch_add(1, std::memory_order_relaxed);  // only this channel's own drain touches it
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
                bus.Publish(MakeEnvelope(name, i), milliseconds(5000));
            }
        });
    }
    for (auto& t : threads) t.join();
    bool drained = bus.Shutdown(milliseconds(60000));
    auto elapsed = steady_clock::now() - start;
    long elapsed_ms = duration_cast<milliseconds>(elapsed).count();
    double elapsed_s = duration_cast<duration<double>>(elapsed).count();

    long delivered = 0;
    for (auto& c : counts) delivered += c->load(std::memory_order_relaxed);

    return RunResult{"chan", 0, producers, 1, producers, kTotal, delivered, elapsed_ms, kTotal / elapsed_s, drained};
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
