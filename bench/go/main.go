// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	sedabus "github.com/resolvingarchitecture/seda-bus-go"
)

const total = 200_000
const trials = 3

// refT is the arbitrary per-process origin for latency timestamps - only
// ever diffed within this process. time.Since uses the monotonic reading
// carried inside a time.Time obtained via time.Now(), so this stays
// wall-clock-jump-safe. See ../WORKLOAD.md's "Latency" section.
var refT = time.Now()

func nowNanos() int64 { return int64(time.Since(refT)) }

type latencyStats struct {
	P50Us, P99Us, P999Us, MaxUs float64
}

// computeLatencyStats sorts (consumes) samples, computed AFTER the timed
// window closes so it never counts against throughput.
func computeLatencyStats(samples []float64) latencyStats {
	sort.Float64s(samples)
	n := len(samples)
	at := func(p float64) float64 { return samples[int(p*float64(n-1))] }
	return latencyStats{at(0.50), at(0.99), at(0.999), samples[n-1]}
}

type result struct {
	Language      string  `json:"language"`
	Config        string  `json:"config"`
	Trial         int     `json:"trial"`
	Producers     int     `json:"producers"`
	Concurrency   int     `json:"concurrency"`
	Channels      int     `json:"channels"`
	Total         int     `json:"total"`
	Delivered     int64   `json:"delivered"`
	ElapsedMs     int64   `json:"elapsed_ms"`
	ThroughputEPS float64 `json:"throughput_eps"`
	P50Us         float64 `json:"p50_us"`
	P99Us         float64 `json:"p99_us"`
	P999Us        float64 `json:"p999_us"`
	MaxUs         float64 `json:"max_us"`
	Drained       bool    `json:"drained"`
}

// runShared: producers threads, all publishing to ONE channel with
// concurrency=producers - the "seq"/"par" configs.
func runShared(config string, producers int) result {
	bus := sedabus.NewBus(producers)
	var count atomic.Int64
	latencies := make([]float64, total)
	bus.Channel("bench", sedabus.NewChannelConfig().WithCapacity(total).WithConcurrency(producers))
	bus.Subscribe("bench", func(env *sedabus.Envelope) bool {
		t1 := nowNanos()
		t0 := sedabus.EnvelopePayload(env).(int64)
		idx := count.Add(1) - 1
		latencies[idx] = float64(t1-t0) / 1000.0
		return true
	})

	timeout := 5 * time.Second
	perProducer := total / producers
	remainder := total - perProducer*producers

	start := time.Now()
	var wg sync.WaitGroup
	for p := 0; p < producers; p++ {
		n := perProducer
		if p == 0 {
			n += remainder
		}
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for i := 0; i < n; i++ {
				bus.Publish(sedabus.MakeEnvelope("bench", nowNanos()), &timeout)
			}
		}(n)
	}
	wg.Wait()
	drained := bus.Shutdown(60 * time.Second)
	elapsed := time.Since(start)
	delivered := count.Load()
	stats := computeLatencyStats(latencies[:delivered])

	return result{
		Language: "go", Config: config, Producers: producers, Concurrency: producers, Channels: 1,
		Total: total, Delivered: delivered, ElapsedMs: elapsed.Milliseconds(),
		ThroughputEPS: float64(total) / elapsed.Seconds(),
		P50Us: stats.P50Us, P99Us: stats.P99Us, P999Us: stats.P999Us, MaxUs: stats.MaxUs,
		Drained: drained,
	}
}

// runIndependentChannels: producers threads, each with its OWN channel and
// OWN dedicated counter - the "chan" config. No shared lock/counter between
// producers at all.
func runIndependentChannels(producers int) result {
	bus := sedabus.NewBus(producers)
	counts := make([]int64, producers)
	latencies := make([][]float64, producers) // one slice per channel, never shared
	perChannel := total / producers
	remainder := total - perChannel*producers

	for c := 0; c < producers; c++ {
		name := fmt.Sprintf("bench%d", c)
		n := perChannel
		if c == 0 {
			n += remainder
		}
		latencies[c] = make([]float64, n)
		bus.Channel(name, sedabus.NewChannelConfig().WithCapacity(n).WithConcurrency(1))
		idx := c
		bus.Subscribe(name, func(env *sedabus.Envelope) bool {
			t1 := nowNanos()
			t0 := sedabus.EnvelopePayload(env).(int64)
			latencies[idx][counts[idx]] = float64(t1-t0) / 1000.0 // safe: concurrency=1
			counts[idx]++                                        // only this channel's own drain touches it
			return true
		})
	}

	timeout := 5 * time.Second
	start := time.Now()
	var wg sync.WaitGroup
	for c := 0; c < producers; c++ {
		name := fmt.Sprintf("bench%d", c)
		n := perChannel
		if c == 0 {
			n += remainder
		}
		wg.Add(1)
		go func(name string, n int) {
			defer wg.Done()
			for i := 0; i < n; i++ {
				bus.Publish(sedabus.MakeEnvelope(name, nowNanos()), &timeout)
			}
		}(name, n)
	}
	wg.Wait()
	drained := bus.Shutdown(60 * time.Second)
	elapsed := time.Since(start)

	var delivered int64
	var allLatencies []float64
	for c, cnt := range counts {
		delivered += cnt
		allLatencies = append(allLatencies, latencies[c][:cnt]...)
	}
	stats := computeLatencyStats(allLatencies)

	return result{
		Language: "go", Config: "chan", Producers: producers, Concurrency: 1, Channels: producers,
		Total: total, Delivered: delivered, ElapsedMs: elapsed.Milliseconds(),
		ThroughputEPS: float64(total) / elapsed.Seconds(),
		P50Us: stats.P50Us, P99Us: stats.P99Us, P999Us: stats.P999Us, MaxUs: stats.MaxUs,
		Drained: drained,
	}
}

func main() {
	par := runtime.NumCPU()
	if par > 8 {
		par = 8
	}
	if par < 1 {
		par = 1
	}

	enc := json.NewEncoder(os.Stdout)
	emit := func(r result) {
		if err := enc.Encode(r); err != nil {
			fmt.Fprintln(os.Stderr, err)
		}
	}

	for trial := 1; trial <= trials; trial++ {
		r := runShared("seq", 1)
		r.Trial = trial
		emit(r)
	}
	for trial := 1; trial <= trials; trial++ {
		r := runShared("par", par)
		r.Trial = trial
		emit(r)
	}
	for trial := 1; trial <= trials; trial++ {
		r := runIndependentChannels(par)
		r.Trial = trial
		emit(r)
	}
}
