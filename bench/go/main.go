// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.
package main

import (
	"encoding/json"
	"fmt"
	"math"
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

// Capacity-curve constants - see ../WORKLOAD.md's "The capacity curve"
// section for the full protocol these implement.
const capacityCurveCapacity = 1024
const capacityCurveTimeout = 30 * time.Second
const calibrationDuration = 2 * time.Second
const calibrationTrials = 2

// Generous upper bound for a calibrationDuration firehose burst under a
// real (if larger-than-sweep) capacity; if a producer somehow exhausts its
// share, it just stops early rather than allocating mid-window.
const calibrationPoolTotal = 1_000_000
const sweepDuration = 4 * time.Second
const sweepTrials = 2
const tick = 20 * time.Millisecond

// Hard ceiling on the TOTAL pre-built envelope pool for one sweep trial,
// across all its producers combined. The formula in ../WORKLOAD.md
// (target_rate_per_producer * duration * 1.5) grows unboundedly with
// target_rate_eps - harmless for a slow implementation, but for a fast one
// at 1.5x load with 8 producers it asks for millions of full Envelope
// structs *per producer*, multiplied by 8 - measured climbing past 3.5GB
// RSS and still rising before this cap was tightened from a (still too
// generous) per-producer-only cap. A per-producer cap alone doesn't bound
// the real cost, which is the sum across all of a trial's producers.
// Capping trades a shorter-than-4s effective sweep window for configs fast
// enough to exhaust it (the loop already stops early on pool exhaustion)
// against never crashing the run - a real limitation, documented rather
// than hidden: see any trial whose elapsed_ms is well under 4000.
const maxSweepPoolTotal = 400_000

var loadFractions = []float64{0.5, 1.0, 1.5}

// Pre-building `total` envelopes is itself a burst of allocation right
// before the timed window starts; without a settle pause + explicit GC, a
// collection provoked by that burst can land inside the first few timed
// publishes instead (caught happening - inconsistently, across several
// languages - the first time this benchmark measured envelope construction
// separately from dispatch). See ../WORKLOAD.md.
const settle = 200 * time.Millisecond

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

	// Capacity-curve only (cap1/cap8) - zero-valued and present for
	// seq/par/chan rows, per ../WORKLOAD.md's output contract.
	Capacity         int     `json:"capacity"`
	LoadFraction     float64 `json:"load_fraction"`
	TargetRateEPS    float64 `json:"target_rate_eps"`
	EndOfWindowDepth int     `json:"end_of_window_depth"`
	DrainTailMs      int64   `json:"drain_tail_ms"`
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

	// Pre-build every envelope before the timed window starts - this
	// benchmark measures bus dispatch/queueing overhead, not envelope
	// construction cost. In production the producer already holds a
	// constructed envelope before it ever calls publish(). See
	// ../WORKLOAD.md.
	perProducerEnvelopes := make([][]*sedabus.Envelope, producers)
	for p := 0; p < producers; p++ {
		n := perProducer
		if p == 0 {
			n += remainder
		}
		envs := make([]*sedabus.Envelope, n)
		for i := 0; i < n; i++ {
			envs[i] = sedabus.MakeEnvelope("bench", int64(0))
		}
		perProducerEnvelopes[p] = envs
	}

	runtime.GC()
	time.Sleep(settle)
	start := time.Now()
	var wg sync.WaitGroup
	for p := 0; p < producers; p++ {
		wg.Add(1)
		go func(envs []*sedabus.Envelope) {
			defer wg.Done()
			for _, env := range envs {
				sedabus.SetPayload(env, nowNanos())
				bus.Publish(env, &timeout)
			}
		}(perProducerEnvelopes[p])
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

	// Pre-build every envelope before the timed window starts - see the
	// comment in runShared.
	perChannelEnvelopes := make([][]*sedabus.Envelope, producers)
	for c := 0; c < producers; c++ {
		name := fmt.Sprintf("bench%d", c)
		n := perChannel
		if c == 0 {
			n += remainder
		}
		envs := make([]*sedabus.Envelope, n)
		for i := 0; i < n; i++ {
			envs[i] = sedabus.MakeEnvelope(name, int64(0))
		}
		perChannelEnvelopes[c] = envs
	}

	timeout := 5 * time.Second
	runtime.GC()
	time.Sleep(settle)
	start := time.Now()
	var wg sync.WaitGroup
	for c := 0; c < producers; c++ {
		wg.Add(1)
		go func(envs []*sedabus.Envelope) {
			defer wg.Done()
			for _, env := range envs {
				sedabus.SetPayload(env, nowNanos())
				bus.Publish(env, &timeout)
			}
		}(perChannelEnvelopes[c])
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

// capChannelState is the per-run harness state for a capacity-curve
// "bench" channel: a delivered counter plus every delivered envelope's
// latency, appended under a mutex (rare relative to Publish's own
// concurrency - this channel's own contention isn't what's under test).
type capChannelState struct {
	mu        sync.Mutex
	latencies []float64
	delivered atomic.Int64
}

// newCapacityCurveBus builds a fresh bus + "bench" channel with a real,
// fixed capacity and Block backpressure - see ../WORKLOAD.md's "The
// capacity curve" section. A fresh bus per trial, not a shared/reset one,
// per the spec.
func newCapacityCurveBus(producers int) (*sedabus.Bus, *capChannelState) {
	bus := sedabus.NewBus(producers)
	st := &capChannelState{latencies: make([]float64, 0, 4096)}
	bus.Channel("bench", sedabus.NewChannelConfig().
		WithCapacity(capacityCurveCapacity).
		WithConcurrency(producers).
		WithBackpressure(sedabus.Block))
	bus.Subscribe("bench", func(env *sedabus.Envelope) bool {
		t1 := nowNanos()
		t0 := sedabus.EnvelopePayload(env).(int64)
		st.mu.Lock()
		st.latencies = append(st.latencies, float64(t1-t0)/1000.0)
		st.mu.Unlock()
		st.delivered.Add(1)
		return true
	})
	return bus, st
}

// buildEnvelopePool pre-builds n envelopes for one producer - construction
// must never fall inside a timed window. See ../WORKLOAD.md.
func buildEnvelopePool(n int) []*sedabus.Envelope {
	envs := make([]*sedabus.Envelope, n)
	for i := range envs {
		envs[i] = sedabus.MakeEnvelope("bench", int64(0))
	}
	return envs
}

// calibrateCapacityCurve is Step 1 of ../WORKLOAD.md's capacity curve:
// measure this specific stage's own sustained throughput under a real,
// bounded, Block-backpressured queue - "100%" for the sweep below. Returns
// the max of calibrationTrials runs (a conservative, achievable ceiling,
// not an average) plus the emitted rows for both runs.
func calibrateCapacityCurve(config string, producers int) (float64, []result) {
	rows := make([]result, 0, calibrationTrials)
	maxEPS := 0.0
	timeoutDur := capacityCurveTimeout

	for trial := 1; trial <= calibrationTrials; trial++ {
		bus, st := newCapacityCurveBus(producers)

		perProducer := calibrationPoolTotal / producers
		pools := make([][]*sedabus.Envelope, producers)
		for p := 0; p < producers; p++ {
			pools[p] = buildEnvelopePool(perProducer)
		}

		runtime.GC()
		time.Sleep(settle)
		start := time.Now()
		deadline := start.Add(calibrationDuration)
		var wg sync.WaitGroup
		var published atomic.Int64
		for p := 0; p < producers; p++ {
			wg.Add(1)
			go func(envs []*sedabus.Envelope) {
				defer wg.Done()
				for _, env := range envs {
					if time.Now().After(deadline) {
						return
					}
					sedabus.SetPayload(env, nowNanos())
					bus.Publish(env, &timeoutDur)
					published.Add(1)
				}
			}(pools[p])
		}
		wg.Wait()
		windowElapsed := time.Since(start)
		endDepth := bus.GetStats()["bench"].Depth
		drainStart := time.Now()
		drained := bus.Shutdown(60 * time.Second)
		drainTail := time.Since(drainStart)

		stats := computeLatencyStats(st.latencies)
		eps := float64(published.Load()) / windowElapsed.Seconds()
		if eps > maxEPS {
			maxEPS = eps
		}

		rows = append(rows, result{
			Language: "go", Config: config, Trial: trial, Producers: producers,
			Concurrency: producers, Channels: 1, Capacity: capacityCurveCapacity,
			LoadFraction: 0, TargetRateEPS: 0,
			Total: int(published.Load()), Delivered: st.delivered.Load(),
			ElapsedMs: windowElapsed.Milliseconds(), ThroughputEPS: eps,
			EndOfWindowDepth: endDepth, DrainTailMs: drainTail.Milliseconds(),
			P50Us: stats.P50Us, P99Us: stats.P99Us, P999Us: stats.P999Us, MaxUs: stats.MaxUs,
			Drained: drained,
		})
	}
	return maxEPS, rows
}

// sweepCapacityCurve is Step 2 of ../WORKLOAD.md's capacity curve: pace
// publishing at target_rate_eps (maxThroughputEPS * loadFraction) for
// sweepDuration via a tick-based rate limiter, then drain, recording how
// the stage behaved relative to that target.
func sweepCapacityCurve(config string, producers int, loadFraction float64, maxThroughputEPS float64, trial int) result {
	targetRateEPS := maxThroughputEPS * loadFraction
	targetPerProducer := targetRateEPS / float64(producers)
	timeoutDur := capacityCurveTimeout

	bus, st := newCapacityCurveBus(producers)

	poolSize := int(math.Ceil(targetPerProducer * sweepDuration.Seconds() * 1.5))
	if poolSize < 1 {
		poolSize = 1
	}
	if maxPerProducer := maxSweepPoolTotal / producers; poolSize > maxPerProducer {
		poolSize = maxPerProducer
	}
	pools := make([][]*sedabus.Envelope, producers)
	for p := 0; p < producers; p++ {
		pools[p] = buildEnvelopePool(poolSize)
	}

	perTick := int(math.Round(targetPerProducer * tick.Seconds()))
	if perTick < 1 {
		perTick = 1
	}

	runtime.GC()
	time.Sleep(settle)
	start := time.Now()
	deadline := start.Add(sweepDuration)
	var wg sync.WaitGroup
	var published atomic.Int64
	for p := 0; p < producers; p++ {
		wg.Add(1)
		go func(envs []*sedabus.Envelope) {
			defer wg.Done()
			idx := 0
			for time.Now().Before(deadline) && idx < len(envs) {
				tickStart := time.Now()
				for i := 0; i < perTick && idx < len(envs) && time.Now().Before(deadline); i++ {
					env := envs[idx]
					idx++
					sedabus.SetPayload(env, nowNanos())
					bus.Publish(env, &timeoutDur)
					published.Add(1)
				}
				tickElapsed := time.Since(tickStart)
				if tickElapsed < tick {
					time.Sleep(tick - tickElapsed)
				}
				// else: don't sleep - the channel's Block wait is already
				// the bottleneck, and achieved rate falling below target
				// is exactly the signal this benchmark exists to show.
			}
		}(pools[p])
	}
	wg.Wait()
	windowElapsed := time.Since(start)
	endDepth := bus.GetStats()["bench"].Depth
	drainStart := time.Now()
	drained := bus.Shutdown(60 * time.Second)
	drainTail := time.Since(drainStart)

	stats := computeLatencyStats(st.latencies)

	return result{
		Language: "go", Config: config, Trial: trial, Producers: producers,
		Concurrency: producers, Channels: 1, Capacity: capacityCurveCapacity,
		LoadFraction: loadFraction, TargetRateEPS: targetRateEPS,
		Total: int(published.Load()), Delivered: st.delivered.Load(),
		ElapsedMs:        windowElapsed.Milliseconds(),
		ThroughputEPS:    float64(published.Load()) / windowElapsed.Seconds(),
		EndOfWindowDepth: endDepth, DrainTailMs: drainTail.Milliseconds(),
		P50Us: stats.P50Us, P99Us: stats.P99Us, P999Us: stats.P999Us, MaxUs: stats.MaxUs,
		Drained: drained,
	}
}

// runCapacityCurve implements the full protocol in ../WORKLOAD.md's "The
// capacity curve" section for one producer-count configuration ("cap1" or
// "cap8"): calibrate this stage's own sustained throughput, then sweep
// load fractions relative to it.
func runCapacityCurve(config string, producers int) []result {
	maxEPS, rows := calibrateCapacityCurve(config, producers)
	for _, lf := range loadFractions {
		for trial := 1; trial <= sweepTrials; trial++ {
			rows = append(rows, sweepCapacityCurve(config, producers, lf, maxEPS, trial))
		}
	}
	return rows
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

	for _, r := range runCapacityCurve("cap1", 1) {
		emit(r)
	}
	for _, r := range runCapacityCurve("cap8", par) {
		emit(r)
	}
}
