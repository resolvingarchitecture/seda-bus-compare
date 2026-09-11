# Build context is the monorepo root — see ../../scripts/build_and_run.sh.
FROM node:22-slim AS build
WORKDIR /src
COPY common/ra-common-ts/ common/ra-common-ts/
COPY seda-bus/seda-bus-ts/ seda-bus/seda-bus-ts/
COPY seda-bus/seda-bus-compare/bench/ts/ seda-bus/seda-bus-compare/bench/ts/
WORKDIR /src/common/ra-common-ts
RUN npm install && npm run build
WORKDIR /src/seda-bus/seda-bus-ts
RUN npm install && npm run build

FROM node:22-slim AS run
COPY --from=build /src /src
WORKDIR /src/seda-bus/seda-bus-compare/bench/ts
ENTRYPOINT ["node", "bench.mjs"]
