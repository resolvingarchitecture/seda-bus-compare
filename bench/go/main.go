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
	Total         int     `json:"total"`
	Delivered     int64   `json:"delivered"`
	ElapsedMs     int64   `json:"elapsed_ms"`
	ThroughputEPS float64 `json:"throughput_eps"`
	Drained       bool    `json:"drained"`
}

func runOnce(config string, producers int) result {
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
		Language:      "go",
		Config:        config,
		Producers:     producers,
		Concurrency:   producers,
		Total:         total,
		Delivered:     count.Load(),
		ElapsedMs:     elapsed.Milliseconds(),
		ThroughputEPS: float64(total) / elapsed.Seconds(),
		Drained:       drained,
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

	configs := []struct {
		name string
		p    int
	}{
		{"seq", 1},
		{"par", par},
	}

	enc := json.NewEncoder(os.Stdout)
	for _, c := range configs {
		for trial := 1; trial <= trials; trial++ {
			r := runOnce(c.name, c.p)
			r.Trial = trial
			if err := enc.Encode(r); err != nil {
				fmt.Fprintln(os.Stderr, err)
			}
		}
	}
}
