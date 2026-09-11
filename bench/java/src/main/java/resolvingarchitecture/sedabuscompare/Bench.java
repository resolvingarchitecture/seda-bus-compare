package resolvingarchitecture.sedabuscompare;

// Benchmark harness. See ../../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

import ra.common.Envelope;
import ra.common.route.SimpleRoute;
import ra.common.service.ServiceLevel;
import ra.sedabus.SEDABus;

import java.util.Arrays;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicLongArray;

public class Bench {
    static final int TOTAL = 200_000;
    static final int TRIALS = 3;

    // now_nanos: System.nanoTime() has an arbitrary per-process origin - only
    // ever diffed within this process. See ../../WORKLOAD.md's "Latency" section.
    record LatencyStats(double p50Us, double p99Us, double p999Us, double maxUs) {
    }

    // Sorts (consumes) `samples`, computed AFTER the timed window closes so it
    // never counts against throughput.
    static LatencyStats computeLatencyStats(double[] samples) {
        Arrays.sort(samples);
        int n = samples.length;
        return new LatencyStats(
                samples[(int) (0.50 * (n - 1))],
                samples[(int) (0.99 * (n - 1))],
                samples[(int) (0.999 * (n - 1))],
                samples[n - 1]);
    }

    record Result(String language, String config, int producers, int concurrency, int channels, int total,
                   long delivered, long elapsedMs, double throughputEps, LatencyStats latency, boolean drained) {
    }

    static void print(Result r, int trial) {
        System.out.printf(
                "{\"language\":\"%s\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
                        + "\"channels\":%d,\"total\":%d,\"delivered\":%d,\"elapsed_ms\":%d,\"throughput_eps\":%f,"
                        + "\"p50_us\":%f,\"p99_us\":%f,\"p999_us\":%f,\"max_us\":%f,\"drained\":%s}%n",
                r.language(), r.config(), trial, r.producers(), r.concurrency(), r.channels(), r.total(),
                r.delivered(), r.elapsedMs(), r.throughputEps(),
                r.latency().p50Us(), r.latency().p99Us(), r.latency().p999Us(), r.latency().maxUs(), r.drained());
    }

    // producers threads, all publishing to ONE channel with concurrency=producers
    // - the "seq"/"par" configs.
    static Result runShared(String config, int producers) throws InterruptedException {
        SEDABus bus = new SEDABus();
        Properties props = new Properties();
        props.setProperty("ra.sedabus.pool.max", Integer.toString(producers));
        bus.start(props);

        AtomicLong count = new AtomicLong();
        double[] latencies = new double[TOTAL];
        // AtMostOnce: no disk-persistence path, matching the other six
        // in-memory-only ports (this benchmark isolates bus overhead, not I/O).
        bus.registerChannel("bench", TOTAL, ServiceLevel.AtMostOnce, null, false, producers);
        bus.registerAsynchConsumer("bench", envelope -> {
            long t1 = System.nanoTime();
            long t0 = (Long) envelope.getContent();
            int idx = (int) count.getAndIncrement();
            latencies[idx] = (t1 - t0) / 1000.0;
            return true;
        });

        int perProducer = TOTAL / producers;
        int remainder = TOTAL - perProducer * producers;

        long start = System.nanoTime();
        Thread[] threads = new Thread[producers];
        for (int p = 0; p < producers; p++) {
            int n = perProducer + (p == 0 ? remainder : 0);
            threads[p] = new Thread(() -> {
                for (int i = 0; i < n; i++) {
                    Envelope e = Envelope.documentFactory();
                    e.getDynamicRoutingSlip().addRoute(new SimpleRoute("bench", "RECEIVE"));
                    e.addContent(System.nanoTime());
                    while (!bus.publish(e)) {
                        // capacity == TOTAL, so this should never actually spin.
                    }
                }
            });
            threads[p].start();
        }
        for (Thread t : threads) t.join();
        boolean drained = bus.gracefulShutdown();
        long elapsedNs = System.nanoTime() - start;
        long delivered = count.get();

        double elapsedS = elapsedNs / 1e9;
        return new Result("java", config, producers, producers, 1, TOTAL, delivered, elapsedNs / 1_000_000,
                TOTAL / elapsedS, computeLatencyStats(Arrays.copyOf(latencies, (int) delivered)), drained);
    }

    // producers threads, each with its OWN channel and OWN dedicated counter -
    // the "chan" config. No shared lock/counter between producers at all.
    static Result runIndependentChannels(int producers) throws InterruptedException {
        SEDABus bus = new SEDABus();
        Properties props = new Properties();
        props.setProperty("ra.sedabus.pool.max", Integer.toString(producers));
        bus.start(props);

        AtomicLongArray counts = new AtomicLongArray(producers);
        double[][] latencies = new double[producers][]; // one array per channel, never shared
        int perChannel = TOTAL / producers;
        int remainder = TOTAL - perChannel * producers;

        for (int c = 0; c < producers; c++) {
            String name = "bench" + c;
            int n = perChannel + (c == 0 ? remainder : 0);
            latencies[c] = new double[n];
            bus.registerChannel(name, n, ServiceLevel.AtMostOnce, null, false, 1);
            int idx = c;
            bus.registerAsynchConsumer(name, envelope -> {
                long t1 = System.nanoTime();
                long t0 = (Long) envelope.getContent();
                int slot = (int) counts.getAndIncrement(idx); // safe: concurrency=1, only this channel's own drain touches it
                latencies[idx][slot] = (t1 - t0) / 1000.0;
                return true;
            });
        }

        long start = System.nanoTime();
        Thread[] threads = new Thread[producers];
        for (int c = 0; c < producers; c++) {
            String name = "bench" + c;
            int n = perChannel + (c == 0 ? remainder : 0);
            threads[c] = new Thread(() -> {
                for (int i = 0; i < n; i++) {
                    Envelope e = Envelope.documentFactory();
                    e.getDynamicRoutingSlip().addRoute(new SimpleRoute(name, "RECEIVE"));
                    e.addContent(System.nanoTime());
                    while (!bus.publish(e)) {
                        // capacity == n, so this should never actually spin.
                    }
                }
            });
            threads[c].start();
        }
        for (Thread t : threads) t.join();
        boolean drained = bus.gracefulShutdown();
        long elapsedNs = System.nanoTime() - start;

        long delivered = 0;
        int totalSamples = 0;
        for (int c = 0; c < producers; c++) {
            delivered += counts.get(c);
            totalSamples += (int) counts.get(c);
        }
        double[] allLatencies = new double[totalSamples];
        int pos = 0;
        for (int c = 0; c < producers; c++) {
            int d = (int) counts.get(c);
            System.arraycopy(latencies[c], 0, allLatencies, pos, d);
            pos += d;
        }

        double elapsedS = elapsedNs / 1e9;
        return new Result("java", "chan", producers, 1, producers, TOTAL, delivered, elapsedNs / 1_000_000,
                TOTAL / elapsedS, computeLatencyStats(allLatencies), drained);
    }

    public static void main(String[] args) throws Exception {
        int cores = Runtime.getRuntime().availableProcessors();
        int par = Math.max(1, Math.min(8, cores));

        for (int trial = 1; trial <= TRIALS; trial++) print(runShared("seq", 1), trial);
        for (int trial = 1; trial <= TRIALS; trial++) print(runShared("par", par), trial);
        for (int trial = 1; trial <= TRIALS; trial++) print(runIndependentChannels(par), trial);
    }
}
