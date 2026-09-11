package resolvingarchitecture.sedabuscompare;

// Benchmark harness. See ../../WORKLOAD.md for the specification this
// implements — every bench/<lang> program follows the same shape.

import ra.common.Envelope;
import ra.common.route.SimpleRoute;
import ra.common.service.ServiceLevel;
import ra.sedabus.SEDABus;

import java.util.Properties;
import java.util.concurrent.atomic.AtomicLong;

public class Bench {
    static final int TOTAL = 200_000;
    static final int TRIALS = 3;

    record Result(String language, String config, int producers, int concurrency, int total, long delivered,
                   long elapsedMs, double throughputEps, boolean drained) {
    }

    static Result runOnce(String config, int producers) throws InterruptedException {
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
        return new Result("java", config, producers, producers, TOTAL, count.get(), elapsedNs / 1_000_000,
                TOTAL / elapsedS, drained);
    }

    public static void main(String[] args) throws Exception {
        int cores = Runtime.getRuntime().availableProcessors();
        int par = Math.max(1, Math.min(8, cores));

        Object[][] configs = {{"seq", 1}, {"par", par}};
        for (Object[] c : configs) {
            String name = (String) c[0];
            int producers = (Integer) c[1];
            for (int trial = 1; trial <= TRIALS; trial++) {
                Result r = runOnce(name, producers);
                System.out.printf(
                        "{\"language\":\"%s\",\"config\":\"%s\",\"trial\":%d,\"producers\":%d,\"concurrency\":%d,"
                                + "\"total\":%d,\"delivered\":%d,\"elapsed_ms\":%d,\"throughput_eps\":%f,\"drained\":%s}%n",
                        r.language(), r.config(), trial, r.producers(), r.concurrency(), r.total(), r.delivered(),
                        r.elapsedMs(), r.throughputEps(), r.drained());
            }
        }
    }
}
