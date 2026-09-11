# Build context is the monorepo root (the directory containing both
# common/ and seda-bus/) — see ../../scripts/build_and_run.sh, which invokes
# this with the right context and -f path so it works regardless of where
# the monorepo is cloned.
FROM golang:1.27 AS build
WORKDIR /src
COPY common/ra-common-go/ common/ra-common-go/
COPY seda-bus/seda-bus-go/ seda-bus/seda-bus-go/
COPY seda-bus/seda-bus-compare/bench/go/ seda-bus/seda-bus-compare/bench/go/
WORKDIR /src/seda-bus/seda-bus-compare/bench/go
RUN go build -o /bench .

FROM debian:12-slim AS run
COPY --from=build /bench /bench
ENTRYPOINT ["/bench"]
