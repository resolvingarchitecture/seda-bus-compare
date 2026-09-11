// Benchmark harness. See ../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	sedabus "github.com/resolvingarchitecture/seda-bus-go"
)

const total = 200_000
const trials = 3

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
	Drained       bool    `json:"drained"`
}

// runShared: producers threads, all publishing to ONE channel with
// concurrency=producers - the "seq"/"par" configs.
func runShared(config string, producers int) result {
	bus := sedabus.NewBus(producers)
	var count atomic.Int64
	bus.Channel("bench", sedabus.NewChannelConfig().WithCapacity(total).WithConcurrency(producers))
	bus.Subscribe("bench", func(_ *sedabus.Envelope) bool {
		count.Add(1)
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
				bus.Publish(sedabus.MakeEnvelope("bench", i), &timeout)
			}
		}(n)
	}
	wg.Wait()
	drained := bus.Shutdown(60 * time.Second)
	elapsed := time.Since(start)

	return result{
		Language: "go", Config: config, Producers: producers, Concurrency: producers, Channels: 1,
		Total: total, Delivered: count.Load(), ElapsedMs: elapsed.Milliseconds(),
		ThroughputEPS: float64(total) / elapsed.Seconds(), Drained: drained,
	}
}

// runIndependentChannels: producers threads, each with its OWN channel and
// OWN dedicated counter - the "chan" config. No shared lock/counter between
// producers at all.
func runIndependentChannels(producers int) result {
	bus := sedabus.NewBus(producers)
	counts := make([]int64, producers)
	perChannel := total / producers
	remainder := total - perChannel*producers

	for c := 0; c < producers; c++ {
		name := fmt.Sprintf("bench%d", c)
		n := perChannel
		if c == 0 {
			n += remainder
		}
		bus.Channel(name, sedabus.NewChannelConfig().WithCapacity(n).WithConcurrency(1))
		idx := c
		bus.Subscribe(name, func(_ *sedabus.Envelope) bool {
			counts[idx]++ // safe: concurrency=1, only this channel's own drain touches it
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
				bus.Publish(sedabus.MakeEnvelope(name, i), &timeout)
			}
		}(name, n)
	}
	wg.Wait()
	drained := bus.Shutdown(60 * time.Second)
	elapsed := time.Since(start)

	var delivered int64
	for _, c := range counts {
		delivered += c
	}

	return result{
		Language: "go", Config: "chan", Producers: producers, Concurrency: 1, Channels: producers,
		Total: total, Delivered: delivered, ElapsedMs: elapsed.Milliseconds(),
		ThroughputEPS: float64(total) / elapsed.Seconds(), Drained: drained,
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
