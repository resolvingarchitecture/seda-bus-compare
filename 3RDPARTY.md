# 3rd Party Comparisons


## Message Brokers

| System                         | How it maps to SEDA                                                                                                                                                                                   |
|--------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Apache Kafka broker            | Network threads (reactor) → shared request queue → I/O thread pool — a 3-stage SEDA pipeline. No adaptive controller; pool sizes are static config.                                                   |
| Apache Camel `seda:` component | First-class component since Camel 1.1, still maintained. Messages exchange on a `BlockingQueue`, consumers run in a separate thread pool. Used in enterprise integration (Red Hat Fuse, Spring Boot). |
| Mule ESB                       | Explicitly built on SEDA — each component is a stage with its own thread pool and work queue.                                                                                                         |
| Apache MINA                    | SEDA-inspired staged handlers/filters for high-scale I/O.                                                                                                                                             |
| NATS                           | Per-connection goroutines (read loop + write loop) with event-driven dispatch — structurally similar but no explicit stage boundaries or admission control.                                           |
| Akka                           | Actor model with queue-mediated stages; developers implement SEDA-like pipelines for high-throughput processing.                                                                                      |



## Big Data Processors

Does big data software like storm implement some of these?

Yes, and the comparison is actually quite illuminating for your library's design decisions:

**All three use staged pipelines** (the core SEDA structural idea):

| System                    | Stages                        | Backpressure mechanism                                                                                                    | Adaptive?                                      |
|---------------------------|-------------------------------|---------------------------------------------------------------------------------------------------------------------------|------------------------------------------------|
| Apache Storm              | Spouts → Bolts (topology DAG) | **Bang-bang controller**: high/low watermark on receive queues → throttle spouts via ZooKeeper signal                     | No — static executor count                     |
| Apache Flink              | Operators in a job graph      | **Credit-based flow control**: downstream grants "credits" (buffer slots) upstream; sender can't exceed available credits | No — static parallelism                        |
| Spark Streaming (DStream) | Micro-batch pipeline          | **PID controller** on ingestion rate — measures batch processing duration, adjusts the receive rate per partition         | **Yes** — but only adjusts *rate*, not threads |

**The interesting one is Spark Streaming's PID controller.** It's the closest production precedent to a "real" adaptive controller in the big data space. It:
- Monitors a single signal (processing time per micro-batch)
- Uses a Proportional-Integral-Derivative controller to compute a new ingestion rate
- Has configurable bounds (`minRate`, `maxRatePerPartition`) to prevent oscillation
- Has been stable in production since Spark 1.5 (2015)

But note what it does *not* do: it doesn't resize thread pools, doesn't shed load at individual stages, and doesn't adapt per-stage. It's a single-loop rate governor on the source. That's a much simpler control problem than SEDA's per-stage thread-pool + admission-control.

**What none of them do:**
- Dynamically resize per-stage thread pools
- Implement per-stage admission control (load shedding)
- Use a multi-variable control loop (the full SEDA controller)

**What this means for your library:**

The industry has converged on a spectrum of increasing sophistication:

1. **Static** (Kafka, NATS, Mule) — no adaptation
2. **Bang-bang** (Storm) — threshold-based throttle, simple but can oscillate at boundaries
3. **Credit-based** (Flink) — receiver-driven, no central controller, very robust
4. **PID rate control** (Spark Streaming) — proper control theory, but single-loop, single-variable

Your "simple adaptive admission controller" idea (sliding-window QPS/RT, shed when exceeded) sits between levels 2 and 3 — it's more informed than Storm's bang-bang but simpler than Flink's credit system. That's a reasonable sweet spot for a library. The PID approach is a useful reference if you ever want to go beyond threshold-based shedding, but I'd still argue it's overkill for a library feature — Spark had to build it specifically because micro-batch rate is their *only* knob (they can't resize executors mid-batch either).

The **credit-based approach** (Flink's model) is arguably the best fit for a message bus library: it's receiver-driven, requires no central coordinator, and naturally composes across stages. If you wanted to go beyond simple threshold shedding in the future, that's the pattern to look at.

## Other Stacks

Are there other message brokers? the ones above seem mostly java based. what other brokers, e.g. microsoft's of cloud vendor stacks?

Good question — the landscape is much more diverse than the Java-centric ones we've been discussing. Here's a broader picture:

## Cloud-native / proprietary (closed-source internals)

| Broker            | Language/Stack              | Architecture model                                                                                                                                                                                                                                                        |
|-------------------|-----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Azure Service Bus | .NET (C#)                   | **Two-tier**: gateway nodes (auth) → messaging broker nodes (routing) → messaging stores. Partitioned across multiple brokers/stores for throughput. No public detail on thread pools or adaptive control.                                                                |
| AWS SQS           | Proprietary (likely C++/Go) | **Stateless front-end + durable backend**. No partitions visible to the user; horizontal scaling is fully opaque. Pull-based, at-least-once. No SEDA-like stages exposed.                                                                                                 |
| AWS SNS           | Proprietary                 | **Push-based fan-out**. Publishes to multiple subscribers (SQS, Lambda, HTTP) in parallel. No visible staged pipeline.                                                                                                                                                    |
| Google Pub/Sub    | C++/Go (Google-internal)    | **Two-plane**: control plane (*routers*) assign clients to data plane (*forwarders*). Forwarders handle publish/ack/delivery. Messages written to distributed log-like storage; each subscription maintains its own cursor. No user-visible thread pools or stage config. |

The key insight: **cloud vendors hide the concurrency model entirely**. You get horizontal scaling, but no knobs for per-stage thread counts, queue depths, or admission control. The SEDA question doesn't even arise because the abstraction boundary is "the service handles it."

## Open-source, non-Java

| Broker            | Language      | Concurrency model                                                                                                                              | SEDA relevance                                                                                                                                                         |
|-------------------|---------------|------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Redpanda          | C++ (Seastar) | **Thread-per-core** (Shard). Each CPU core gets a dedicated set of partitions; no cross-core shared state. Raft per partition for replication. | Closest to SEDA's *structural* idea in the streaming space — each shard is an isolated stage with its own "thread pool" (a single thread). But no adaptive controller. |
| NATS / JetStream  | Go            | **Goroutine-per-connection** + per-subject work queues (JetStream).                                                                            | We discussed this already — structurally SEDA-like, no adaptive control.                                                                                               |
| RabbitMQ          | Erlang        | **Actor model** — each queue is an isolated process with its own mailbox.                                                                      | Functionally equivalent to SEDA stages (mailbox = work queue, process = stage). Erlang's scheduler handles concurrency; no user-tunable per-stage pools.               |
| Eclipse Mosquitto | C             | **Single-threaded** event loop (select/epoll).                                                                                                 | The opposite of SEDA — one thread handles everything. Designed for embedded/edge, not high-throughput.                                                                 |
| EMQX              | Erlang        | Actor model (like RabbitMQ), but with a **C-based hot path** for message routing.                                                              | Hybrid: Erlang for control plane, C for data plane.                                                                                                                    |
| IBM MQ            | C/C++         | **Channel-based** — each channel is a dedicated connection with its own message flow. Shared message queues with multiple channels.            | Pre-dates SEDA; the channel model is a simpler form of staged processing.                                                                                              |
| Redis Streams     | C             | **Single-threaded** (event loop) with `XREADGROUP` for consumer groups.                                                                        | Not staged at all — single event loop. Consumer groups provide partitioning but no per-stage concurrency.                                                              |

## The pattern that emerges

Looking at this from a SEDA-design perspective, the industry splits into three camps:

1. **Hide it** (cloud vendors) — "we scale for you, you get no knobs." No SEDA concepts exposed.
2. **Actor model** (RabbitMQ, EMQX) — each queue/subscription is an isolated process with a mailbox. This is *structurally* SEDA (stage = process, queue = mailbox), but the concurrency is managed by the runtime scheduler, not by an explicit controller.
3. **Thread-per-core / shard** (Redpanda, Kafka) — work is statically partitioned to cores/threads. No adaptation, but very predictable.

Your library's SEDA design (explicit stages, per-stage queues, per-stage concurrency limits, back-pressure) actually sits in a **gap** — it's more explicit and tunable than the actor model (RabbitMQ), more structured than the cloud services (opaque), and more adaptive than Redpanda/Kafka (static sharding). The closest open-source analog in terms of *exposed control* is probably RabbitMQ's per-queue prefetch + consumer count tuning, but that's a much coarser abstraction.

**For your library's positioning:** the non-Java brokers that are most architecturally interesting as references are **Redpanda** (thread-per-core isolation, which validates the "one stage per thread" idea) and **NATS JetStream** (per-subject work queues with configurable consumer concurrency, which is closest to your per-stage concurrency limits). Neither has the adaptive admission control you're considering adding, so that would still be a differentiator.

