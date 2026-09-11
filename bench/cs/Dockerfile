# Build context is the monorepo root — see ../../scripts/build_and_run.sh.
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY common/ra-common-cs/ common/ra-common-cs/
COPY seda-bus/seda-bus-cs/ seda-bus/seda-bus-cs/
COPY seda-bus/seda-bus-compare/bench/cs/ seda-bus/seda-bus-compare/bench/cs/
WORKDIR /src/seda-bus/seda-bus-compare/bench/cs
RUN dotnet publish -c Release -o /app --self-contained false

FROM mcr.microsoft.com/dotnet/runtime:8.0 AS run
COPY --from=build /app /app
ENTRYPOINT ["dotnet", "/app/Bench.dll"]
