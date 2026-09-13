// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "seda_bus/seda_bus.hpp"

using namespace ra::seda_bus;
using namespace std::chrono;

constexpr int kTotal = 200000;
constexpr int kTrials = 3;

// Pre-building kTotal envelopes is itself a burst of allocation right
// before the timed window starts; without a settle pause the allocator/OS
// memory state that burst leaves behind can bleed into the first few timed
// publishes (caught happening - inconsistently, across several languages -
// the first time this benchmark measured envelope construction separately
// from dispatch). See ../WORKLOAD.md.
constexpr auto kSettle = milliseconds(200);

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

    // Pre-build every envelope before the timed window starts - this
    // benchmark measures bus dispatch/queueing overhead, not envelope
    // construction cost. In production the producer already holds a
    // constructed envelope before it ever calls publish(). See
    // ../WORKLOAD.md.
    std::vector<std::vector<Envelope>> per_producer_envelopes;
    for (int p = 0; p < producers; p++) {
        int n = per_producer + (p == 0 ? remainder : 0);
        std::vector<Envelope> envs;
        envs.reserve(n);
        for (int i = 0; i < n; i++) envs.push_back(MakeEnvelope("bench", static_cast<int64_t>(0)));
        per_producer_envelopes.push_back(std::move(envs));
    }

    std::this_thread::sleep_for(kSettle);
    auto start = steady_clock::now();
    std::vector<std::thread> threads;
    for (int p = 0; p < producers; p++) {
        threads.emplace_back([&bus, envs = std::move(per_producer_envelopes[p])]() mutable {
            for (auto& env : envs) {
                SetPayload(env, NowNanos());
                bus.Publish(std::move(env), milliseconds(5000));
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

    // Pre-build every envelope before the timed window starts - see the
    // comment in RunShared.
    std::vector<std::vector<Envelope>> per_channel_envelopes;
    for (int c = 0; c < producers; c++) {
        std::string name = "bench" + std::to_string(c);
        int n = per_channel + (c == 0 ? remainder : 0);
        std::vector<Envelope> envs;
        envs.reserve(n);
        for (int i = 0; i < n; i++) envs.push_back(MakeEnvelope(name, static_cast<int64_t>(0)));
        per_channel_envelopes.push_back(std::move(envs));
    }

    std::this_thread::sleep_for(kSettle);
    auto start = steady_clock::now();
    std::vector<std::thread> threads;
    for (int c = 0; c < producers; c++) {
        threads.emplace_back([&bus, envs = std::move(per_channel_envelopes[c])]() mutable {
            for (auto& env : envs) {
                SetPayload(env, NowNanos());
                bus.Publish(std::move(env), milliseconds(5000));
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

// -- capacity curve (primary benchmark; see ../WORKLOAD.md) --------------

constexpr int kCapCapacity = 1024;
constexpr auto kCalibrationDuration = milliseconds(2000);
constexpr int kCalibrationTrials = 2;
constexpr auto kSweepDuration = milliseconds(4000);
constexpr int kSweepTrials = 2;
constexpr auto kTick = milliseconds(20);
const double kLoadFractions[] = {0.5, 1.0, 1.5};
constexpr auto kCapPublishTimeout = milliseconds(30000);

// Hard ceiling on one window's TOTAL pre-built envelope pool, across all
// its producers combined. The rate-derived formula below grows unboundedly
// with target_rate_eps, which for a fast config at high load and several
// producers can ask for millions of Envelopes per producer - an uncapped
// version of this same formula OOM-killed seda-bus-go's equivalent
// benchmark outright (measured climbing past 3.5GB RSS and still rising);
// capped here before ever running it. If a producer exhausts its (possibly
// capped) pool before the window ends, it just stops publishing early - a
// trial's real elapsed_ms can then come in well under the nominal window,
// an accepted, visible limitation rather than a silently invalid run.
constexpr int kMaxPoolTotal = 400000;

struct CapResult {
    const char* config;
    int trial;
    int producers;
    int concurrency;
    int capacity;
    double load_fraction;
    double target_rate_eps;
    int total;
    long delivered;
    long elapsed_ms;
    double throughput_eps;
    int end_of_window_depth;
    long drain_tail_ms;
    LatencyStats latency;
    bool drained;
};

void PrintCapResult(const CapResult& r) {
    std::printf(
        "{\"language\":\"cpp\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
        "\"channels\":1,\"capacity\":%d,\"load_fraction\":%g,\"target_rate_eps\":%f,\"total\":%d,"
        "\"delivered\":%ld,\"elapsed_ms\":%ld,\"throughput_eps\":%f,\"end_of_window_depth\":%d,"
        "\"drain_tail_ms\":%ld,\"p50_us\":%f,\"p99_us\":%f,\"p999_us\":%f,\"max_us\":%f,\"drained\":%s}\n",
        r.config, r.trial, r.producers, r.concurrency, r.capacity, r.load_fraction, r.target_rate_eps, r.total,
        r.delivered, r.elapsed_ms, r.throughput_eps, r.end_of_window_depth, r.drain_tail_ms, r.latency.p50_us,
        r.latency.p99_us, r.latency.p999_us, r.latency.max_us, r.drained ? "true" : "false");
}

struct CapWindowRaw {
    int published;
    long delivered;
    long elapsed_ms;
    int end_of_window_depth;
    long drain_tail_ms;
    std::vector<double> samples;
    bool drained;
};

// One timed window: `producers` threads publishing to a single, really
// bounded (kCapCapacity), Block-backpressured channel. `target_rate_eps`
// is 0.0 for an unpaced firehose burst (calibration), or > 0.0 for a
// tick-paced sweep at that aggregate rate. See ../WORKLOAD.md's "capacity
// curve" section for the exact algorithm this implements.
CapWindowRaw RunCapacityWindow(int producers, double target_rate_eps, milliseconds window_duration) {
    Bus bus(producers);
    std::atomic<long> count{0};
    std::mutex latencies_mu;
    std::vector<double> latencies;

    bus.Channel("bench", ChannelConfig{}.Capacity(kCapCapacity).Concurrency(producers).SetBackpressure(Backpressure::Block));
    bus.Subscribe("bench", [&count, &latencies, &latencies_mu](Envelope& env) {
        int64_t t1 = NowNanos();
        int64_t t0 = EnvelopePayload(env).get<int64_t>();
        {
            std::lock_guard<std::mutex> lock(latencies_mu);
            latencies.push_back((t1 - t0) / 1000.0);
        }
        count.fetch_add(1, std::memory_order_relaxed);
        return true;
    });

    double per_producer_rate = target_rate_eps > 0.0 ? target_rate_eps / producers : 0.0;
    double duration_s = duration_cast<duration<double>>(window_duration).count();
    // Calibration (target_rate_eps == 0) has no prior rate estimate to size
    // a pool against - assume a generous ceiling instead.
    double sizing_rate = per_producer_rate > 0.0 ? per_producer_rate : (2000000.0 / producers);
    int pool_per_producer = static_cast<int>(std::ceil(sizing_rate * duration_s * 1.5));
    if (pool_per_producer < 16) pool_per_producer = 16;
    int max_per_producer = kMaxPoolTotal / producers;
    if (pool_per_producer > max_per_producer) pool_per_producer = max_per_producer;

    std::vector<std::vector<Envelope>> pools;
    for (int p = 0; p < producers; p++) {
        std::vector<Envelope> envs;
        envs.reserve(pool_per_producer);
        for (int i = 0; i < pool_per_producer; i++) envs.push_back(MakeEnvelope("bench", static_cast<int64_t>(0)));
        pools.push_back(std::move(envs));
    }

    std::this_thread::sleep_for(kSettle);
    auto start = steady_clock::now();
    auto deadline = start + window_duration;
    std::atomic<int> published{0};

    std::vector<std::thread> threads;
    for (int p = 0; p < producers; p++) {
        int per_tick = per_producer_rate > 0.0
                           ? std::max(1, static_cast<int>(std::round(per_producer_rate * duration_cast<duration<double>>(kTick).count())))
                           : 4096;
        threads.emplace_back([&bus, &published, envs = std::move(pools[p]), deadline, per_tick]() mutable {
            size_t idx = 0;
            while (steady_clock::now() < deadline && idx < envs.size()) {
                auto tick_start = steady_clock::now();
                int n = 0;
                while (n < per_tick && idx < envs.size() && steady_clock::now() < deadline) {
                    SetPayload(envs[idx], NowNanos());
                    if (bus.Publish(std::move(envs[idx]), kCapPublishTimeout)) {
                        published.fetch_add(1, std::memory_order_relaxed);
                    }
                    idx++;
                    n++;
                }
                if (n == 0) break;  // pool exhausted
                auto tick_elapsed = steady_clock::now() - tick_start;
                if (tick_elapsed < kTick) {
                    std::this_thread::sleep_for(kTick - tick_elapsed);
                }
                // else: don't sleep - the channel's Block wait is already
                // the bottleneck, which is exactly the signal this
                // benchmark exists to show.
            }
        });
    }
    for (auto& t : threads) t.join();
    auto window_elapsed = steady_clock::now() - start;
    long window_elapsed_ms = duration_cast<milliseconds>(window_elapsed).count();

    int end_of_window_depth = 0;
    auto stats_map = bus.GetStats();
    auto it = stats_map.find("bench");
    if (it != stats_map.end()) end_of_window_depth = static_cast<int>(it->second.depth);

    auto drain_start = steady_clock::now();
    bool drained = bus.Shutdown(milliseconds(60000));
    long drain_tail_ms = duration_cast<milliseconds>(steady_clock::now() - drain_start).count();

    return CapWindowRaw{published.load(), count.load(), window_elapsed_ms, end_of_window_depth,
                         drain_tail_ms,    std::move(latencies),           drained};
}

// Step 1 (calibrate) then Step 2 (sweep [0.5, 1.0, 1.5] x target rate),
// printing every row per ../WORKLOAD.md's output contract.
void RunCapacityCurve(const char* config, int producers) {
    double max_throughput_eps = 0.0;
    for (int trial = 1; trial <= kCalibrationTrials; trial++) {
        CapWindowRaw w = RunCapacityWindow(producers, 0.0, kCalibrationDuration);
        double throughput_eps = w.published / (w.elapsed_ms / 1000.0);
        if (throughput_eps > max_throughput_eps) max_throughput_eps = throughput_eps;
        PrintCapResult(CapResult{config, trial, producers, producers, kCapCapacity, 0.0, 0.0, w.published,
                                  w.delivered, w.elapsed_ms, throughput_eps, w.end_of_window_depth, w.drain_tail_ms,
                                  ComputeLatencyStats(w.samples), w.drained});
    }

    for (double load_fraction : kLoadFractions) {
        double target_rate_eps = max_throughput_eps * load_fraction;
        for (int trial = 1; trial <= kSweepTrials; trial++) {
            CapWindowRaw w = RunCapacityWindow(producers, target_rate_eps, kSweepDuration);
            double throughput_eps = w.published / (w.elapsed_ms / 1000.0);
            PrintCapResult(CapResult{config, trial, producers, producers, kCapCapacity, load_fraction,
                                      target_rate_eps, w.published, w.delivered, w.elapsed_ms, throughput_eps,
                                      w.end_of_window_depth, w.drain_tail_ms, ComputeLatencyStats(w.samples),
                                      w.drained});
        }
    }
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

    RunCapacityCurve("cap1", 1);
    RunCapacityCurve("cap8", par);
}
