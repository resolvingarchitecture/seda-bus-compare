package resolvingarchitecture.sedabuscompare;

// Benchmark harness. See ../../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

import ra.common.Envelope;
import ra.common.messaging.MessageChannel;
import ra.common.route.SimpleRoute;
import ra.common.service.ServiceLevel;
import ra.sedabus.Backpressure;
import ra.sedabus.SEDABus;

import java.util.Arrays;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicLongArray;

public class Bench {
    static final int TOTAL = 200_000;
    static final int TRIALS = 3;

    // Pre-building TOTAL envelopes is itself a burst of allocation right
    // before the timed window starts; without a settle pause + explicit GC,
    // a collection provoked by that burst can land inside the first few
    // timed publishes instead (caught happening - inconsistently, across
    // several languages - the first time this benchmark measured envelope
    // construction separately from dispatch). See ../../WORKLOAD.md.
    static final long SETTLE_MS = 200;

    static void settle() throws InterruptedException {
        System.gc();
        Thread.sleep(SETTLE_MS);
    }

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

        // Pre-build every envelope before the timed window starts - this
        // benchmark measures bus dispatch/queueing overhead, not envelope
        // construction cost. In production the producer already holds a
        // constructed envelope before it ever calls publish(). See
        // ../../WORKLOAD.md.
        Envelope[][] perProducerEnvelopes = new Envelope[producers][];
        for (int p = 0; p < producers; p++) {
            int n = perProducer + (p == 0 ? remainder : 0);
            Envelope[] envs = new Envelope[n];
            for (int i = 0; i < n; i++) {
                Envelope e = Envelope.documentFactory();
                e.getDynamicRoutingSlip().addRoute(new SimpleRoute("bench", "RECEIVE"));
                envs[i] = e;
            }
            perProducerEnvelopes[p] = envs;
        }

        settle();
        long start = System.nanoTime();
        Thread[] threads = new Thread[producers];
        for (int p = 0; p < producers; p++) {
            Envelope[] envs = perProducerEnvelopes[p];
            threads[p] = new Thread(() -> {
                for (Envelope e : envs) {
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

        // Pre-build every envelope before the timed window starts - see the
        // comment in runShared.
        Envelope[][] perChannelEnvelopes = new Envelope[producers][];
        for (int c = 0; c < producers; c++) {
            String name = "bench" + c;
            int n = perChannel + (c == 0 ? remainder : 0);
            Envelope[] envs = new Envelope[n];
            for (int i = 0; i < n; i++) {
                Envelope e = Envelope.documentFactory();
                e.getDynamicRoutingSlip().addRoute(new SimpleRoute(name, "RECEIVE"));
                envs[i] = e;
            }
            perChannelEnvelopes[c] = envs;
        }

        settle();
        long start = System.nanoTime();
        Thread[] threads = new Thread[producers];
        for (int c = 0; c < producers; c++) {
            Envelope[] envs = perChannelEnvelopes[c];
            threads[c] = new Thread(() -> {
                for (Envelope e : envs) {
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

    // -- capacity curve (primary benchmark; see ../../WORKLOAD.md) ------

    static final int CAP_CAPACITY = 1024;
    static final int CALIBRATION_TRIALS = 2;
    static final long CALIBRATION_DURATION_MS = 2000;
    static final int SWEEP_TRIALS = 2;
    static final long SWEEP_DURATION_MS = 4000;
    static final long TICK_MS = 20;
    // Hard ceiling on one sweep trial's TOTAL pre-built pool, across all
    // its producers combined - the rate-derived formula below grows
    // unboundedly with target_rate_eps, which for a fast config at high
    // load and several producers can ask for millions of Envelopes per
    // producer (an uncapped version of this same formula OOM-killed
    // seda-bus-go's equivalent benchmark outright; capped here before ever
    // running it).
    static final int MAX_SWEEP_POOL_TOTAL = 400_000;
    static final double[] LOAD_FRACTIONS = {0.5, 1.0, 1.5};

    record CapResult(String config, int producers, int concurrency, int channels, int capacity,
                      double loadFraction, double targetRateEps, int total, long delivered, long elapsedMs,
                      double throughputEps, int endOfWindowDepth, long drainTailMs, LatencyStats latency,
                      boolean drained) {
    }

    static void printCap(CapResult r, int trial) {
        System.out.printf(
                "{\"language\":\"java\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
                        + "\"channels\":%d,\"capacity\":%d,\"load_fraction\":%.2f,\"target_rate_eps\":%.2f,"
                        + "\"total\":%d,\"delivered\":%d,\"elapsed_ms\":%d,\"throughput_eps\":%f,"
                        + "\"end_of_window_depth\":%d,\"drain_tail_ms\":%d,"
                        + "\"p50_us\":%f,\"p99_us\":%f,\"p999_us\":%f,\"max_us\":%f,\"drained\":%s}%n",
                r.config(), trial, r.producers(), r.concurrency(), r.channels(), r.capacity(), r.loadFraction(),
                r.targetRateEps(), r.total(), r.delivered(), r.elapsedMs(), r.throughputEps(),
                r.endOfWindowDepth(), r.drainTailMs(),
                r.latency().p50Us(), r.latency().p99Us(), r.latency().p999Us(), r.latency().maxUs(), r.drained());
    }

    // One channel, Capacity 1024, Backpressure.Block, `producers` producer
    // threads sharing it with Concurrency == producers (mirrors seq/par's
    // producer/concurrency shape). targetRateEpsTotal == 0 means unpaced
    // (Step 1's calibration burst); otherwise each producer paces itself in
    // TICK_MS ticks, per ../../WORKLOAD.md Step 2.4.
    static CapResult runCapacityWindow(String config, int producers, int concurrency, long durationMs,
                                        double targetRateEpsTotal, double loadFraction, int poolPerProducer)
            throws InterruptedException {
        SEDABus bus = new SEDABus();
        Properties props = new Properties();
        props.setProperty("ra.sedabus.pool.max", Integer.toString(concurrency));
        bus.start(props);

        AtomicLong count = new AtomicLong();
        double[] latencies = new double[poolPerProducer * producers];
        MessageChannel channel = bus.registerChannel(
                "bench", CAP_CAPACITY, ServiceLevel.AtMostOnce, null, false, concurrency, Backpressure.Block);
        bus.registerAsynchConsumer("bench", envelope -> {
            long t1 = System.nanoTime();
            long t0 = (Long) envelope.getContent();
            int idx = (int) count.getAndIncrement();
            if (idx < latencies.length) {
                latencies[idx] = (t1 - t0) / 1000.0;
            }
            return true;
        });

        // Pre-build this trial's envelope pool before the timed window
        // starts - construction must never fall inside it. Sized per
        // ../../WORKLOAD.md Step 2.2 for the sweep; calibration passes its
        // own generous fixed size (see runCalibration).
        Envelope[][] pool = new Envelope[producers][];
        for (int p = 0; p < producers; p++) {
            Envelope[] envs = new Envelope[poolPerProducer];
            for (int i = 0; i < poolPerProducer; i++) {
                Envelope e = Envelope.documentFactory();
                e.getDynamicRoutingSlip().addRoute(new SimpleRoute("bench", "RECEIVE"));
                envs[i] = e;
            }
            pool[p] = envs;
        }

        double targetRatePerProducer = targetRateEpsTotal / producers;
        // Unpaced (calibration): publish everything back-to-back, no sleep.
        long perTick = targetRatePerProducer > 0
                ? Math.max(1, Math.round(targetRatePerProducer * (TICK_MS / 1000.0)))
                : Long.MAX_VALUE;

        AtomicLong published = new AtomicLong();
        settle();
        long start = System.nanoTime();
        long windowDeadlineNanos = start + durationMs * 1_000_000L;
        Thread[] threads = new Thread[producers];
        for (int p = 0; p < producers; p++) {
            Envelope[] envs = pool[p];
            threads[p] = new Thread(() -> {
                int i = 0;
                while (i < envs.length && System.nanoTime() < windowDeadlineNanos) {
                    long tickStart = System.nanoTime();
                    long n = 0;
                    while (n < perTick && i < envs.length && System.nanoTime() < windowDeadlineNanos) {
                        Envelope e = envs[i++];
                        e.addContent(System.nanoTime());
                        bus.publish(e); // Block: waits for room rather than failing; see ../../WORKLOAD.md
                        published.incrementAndGet();
                        n++;
                    }
                    long tickElapsedMs = (System.nanoTime() - tickStart) / 1_000_000L;
                    if (tickElapsedMs < TICK_MS) {
                        try {
                            Thread.sleep(TICK_MS - tickElapsedMs);
                        } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                        }
                    }
                    // else: don't sleep - Block is already the bottleneck,
                    // and the achieved rate falling below target is exactly
                    // the signal this benchmark exists to show.
                }
            });
            threads[p].start();
        }
        for (Thread t : threads) t.join();
        long windowElapsedNs = System.nanoTime() - start;

        int endOfWindowDepth = channel.queued();

        long drainStart = System.nanoTime();
        // seda-bus-java's public API has no custom-timeout shutdown call;
        // gracefulShutdown()'s own internal 30s bound stands in for
        // ../../WORKLOAD.md's "60s, generous" - plenty for this workload's
        // bounded overload (worst case a few seconds of backlog).
        boolean drained = bus.gracefulShutdown();
        long drainTailMs = (System.nanoTime() - drainStart) / 1_000_000L;

        long deliveredCount = Math.min(count.get(), latencies.length);
        long publishedCount = published.get();
        double elapsedS = windowElapsedNs / 1e9;

        return new CapResult(config, producers, concurrency, 1, CAP_CAPACITY, loadFraction, targetRateEpsTotal,
                (int) publishedCount, deliveredCount, windowElapsedNs / 1_000_000, publishedCount / elapsedS,
                endOfWindowDepth, drainTailMs,
                computeLatencyStats(Arrays.copyOf(latencies, (int) deliveredCount)), drained);
    }

    public static void main(String[] args) throws Exception {
        int cores = Runtime.getRuntime().availableProcessors();
        int par = Math.max(1, Math.min(8, cores));

        for (int trial = 1; trial <= TRIALS; trial++) print(runShared("seq", 1), trial);
        for (int trial = 1; trial <= TRIALS; trial++) print(runShared("par", par), trial);
        for (int trial = 1; trial <= TRIALS; trial++) print(runIndependentChannels(par), trial);

        for (String config : new String[]{"cap1", "cap8"}) {
            int producers = config.equals("cap1") ? 1 : par;

            double maxThroughput = 0;
            // Fixed, generous calibration pool - independent of the
            // target-rate-based sweep formula below, since calibration is
            // exactly what measures that rate. Comfortably covers this
            // port's achievable throughput over CALIBRATION_DURATION_MS.
            int calibrationPoolPerProducer = Math.max(2_000, 600_000 / producers);
            for (int trial = 1; trial <= CALIBRATION_TRIALS; trial++) {
                CapResult r = runCapacityWindow(config, producers, producers, CALIBRATION_DURATION_MS, 0, 0,
                        calibrationPoolPerProducer);
                printCap(r, trial);
                maxThroughput = Math.max(maxThroughput, r.throughputEps());
            }

            for (double loadFraction : LOAD_FRACTIONS) {
                double targetRateEpsTotal = maxThroughput * loadFraction;
                double targetRatePerProducer = targetRateEpsTotal / producers;
                int poolPerProducer = (int) Math.ceil(targetRatePerProducer * (SWEEP_DURATION_MS / 1000.0) * 1.5);
                poolPerProducer = Math.min(poolPerProducer, MAX_SWEEP_POOL_TOTAL / producers);
                for (int trial = 1; trial <= SWEEP_TRIALS; trial++) {
                    CapResult r = runCapacityWindow(config, producers, producers, SWEEP_DURATION_MS,
                            targetRateEpsTotal, loadFraction, poolPerProducer);
                    printCap(r, trial);
                }
            }
        }
    }
}
