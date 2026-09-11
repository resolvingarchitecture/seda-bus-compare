# Build context is the monorepo root — see ../../scripts/build_and_run.sh.
FROM debian:12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ cmake make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY common/ra-common-cpp/ common/ra-common-cpp/
COPY seda-bus/seda-bus-cpp/ seda-bus/seda-bus-cpp/
COPY seda-bus/seda-bus-compare/bench/cpp/ seda-bus/seda-bus-compare/bench/cpp/
WORKDIR /src/seda-bus/seda-bus-compare/bench/cpp
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j"$(nproc)"

FROM debian:12-slim AS run
COPY --from=build /src/seda-bus/seda-bus-compare/bench/cpp/build/bench /bench
ENTRYPOINT ["/bench"]
