package resolvingarchitecture.sedabuscompare;

// Benchmark harness. See ../../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

import ra.common.Envelope;
import ra.common.route.SimpleRoute;
import ra.common.service.ServiceLevel;
import ra.sedabus.SEDABus;

import java.util.Properties;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicLongArray;

public class Bench {
    static final int TOTAL = 200_000;
    static final int TRIALS = 3;

    record Result(String language, String config, int producers, int concurrency, int channels, int total,
                   long delivered, long elapsedMs, double throughputEps, boolean drained) {
    }

    static void print(Result r, int trial) {
        System.out.printf(
                "{\"language\":\"%s\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
                        + "\"channels\":%d,\"total\":%d,\"delivered\":%d,\"elapsed_ms\":%d,\"throughput_eps\":%f,\"drained\":%s}%n",
                r.language(), r.config(), trial, r.producers(), r.concurrency(), r.channels(), r.total(),
                r.delivered(), r.elapsedMs(), r.throughputEps(), r.drained());
    }

    // producers threads, all publishing to ONE channel with concurrency=producers
    // - the "seq"/"par" configs.
    static Result runShared(String config, int producers) throws InterruptedException {
        SEDABus bus = new SEDABus();
        Properties props = new Properties();
        props.setProperty("ra.sedabus.pool.max", Integer.toString(producers));
        bus.start(props);

        AtomicLong count = new AtomicLong();
        // AtMostOnce: no disk-persistence path, matching the other six
        // in-memory-only ports (this benchmark isolates bus overhead, not I/O).
        bus.registerChannel("bench", TOTAL, ServiceLevel.AtMostOnce, null, false, producers);
        bus.registerAsynchConsumer("bench", envelope -> {
            count.incrementAndGet();
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

        double elapsedS = elapsedNs / 1e9;
        return new Result("java", config, producers, producers, 1, TOTAL, count.get(), elapsedNs / 1_000_000,
                TOTAL / elapsedS, drained);
    }

    // producers threads, each with its OWN channel and OWN dedicated counter -
    // the "chan" config. No shared lock/counter between producers at all.
    static Result runIndependentChannels(int producers) throws InterruptedException {
        SEDABus bus = new SEDABus();
        Properties props = new Properties();
        props.setProperty("ra.sedabus.pool.max", Integer.toString(producers));
        bus.start(props);

        AtomicLongArray counts = new AtomicLongArray(producers);
        int perChannel = TOTAL / producers;
        int remainder = TOTAL - perChannel * producers;

        for (int c = 0; c < producers; c++) {
            String name = "bench" + c;
            int n = perChannel + (c == 0 ? remainder : 0);
            bus.registerChannel(name, n, ServiceLevel.AtMostOnce, null, false, 1);
            int idx = c;
            bus.registerAsynchConsumer(name, envelope -> {
                counts.incrementAndGet(idx); // safe: concurrency=1, only this channel's own drain touches it
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
        for (int c = 0; c < producers; c++) delivered += counts.get(c);

        double elapsedS = elapsedNs / 1e9;
        return new Result("java", "chan", producers, 1, producers, TOTAL, delivered, elapsedNs / 1_000_000,
                TOTAL / elapsedS, drained);
    }

    public static void main(String[] args) throws Exception {
        int cores = Runtime.getRuntime().availableProcessors();
        int par = Math.max(1, Math.min(8, cores));

        for (int trial = 1; trial <= TRIALS; trial++) print(runShared("seq", 1), trial);
        for (int trial = 1; trial <= TRIALS; trial++) print(runShared("par", par), trial);
        for (int trial = 1; trial <= TRIALS; trial++) print(runIndependentChannels(par), trial);
    }
}
