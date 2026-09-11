// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

#include <atomic>
#include <chrono>
#include <cstdio>
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
    int total;
    long delivered;
    long elapsed_ms;
    double throughput_eps;
    bool drained;
};

RunResult RunOnce(const char* config, int producers) {
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

    return RunResult{config, 0, producers, producers, kTotal, count.load(), elapsed_ms, kTotal / elapsed_s, drained};
}

int main() {
    unsigned cores = std::thread::hardware_concurrency();
    int par = static_cast<int>(cores == 0 ? 4 : cores);
    if (par > 8) par = 8;
    if (par < 1) par = 1;

    struct Cfg {
        const char* name;
        int producers;
    };
    Cfg configs[] = {{"seq", 1}, {"par", par}};

    for (auto& c : configs) {
        for (int trial = 1; trial <= kTrials; trial++) {
            RunResult r = RunOnce(c.name, c.producers);
            r.trial = trial;
            std::printf(
                "{\"language\":\"cpp\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
                "\"total\":%d,\"delivered\":%ld,\"elapsed_ms\":%ld,\"throughput_eps\":%f,\"drained\":%s}\n",
                r.config, r.trial, r.producers, r.concurrency, r.total, r.delivered, r.elapsed_ms, r.throughput_eps,
                r.drained ? "true" : "false");
        }
    }
}
