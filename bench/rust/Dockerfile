# Build context is the monorepo root — see ../../scripts/build_and_run.sh.
FROM rust:1-slim AS build
WORKDIR /src
COPY common/ra-common-rust/ common/ra-common-rust/
COPY seda-bus/seda-bus-rust/ seda-bus/seda-bus-rust/
COPY seda-bus/seda-bus-compare/bench/rust/ seda-bus/seda-bus-compare/bench/rust/
WORKDIR /src/seda-bus/seda-bus-compare/bench/rust
RUN cargo build --release

FROM debian:12-slim AS run
COPY --from=build /src/seda-bus/seda-bus-compare/bench/rust/target/release/bench /bench
ENTRYPOINT ["/bench"]
