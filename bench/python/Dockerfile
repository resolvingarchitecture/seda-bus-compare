# The GIL variant (Python 3.13). Build context is the monorepo root — see
# ../../scripts/build_and_run.sh. bench.py has no dependencies beyond the
# stdlib, so this is just an interpreter + source copy, no pip install.
FROM python:3.13-slim
WORKDIR /src
COPY common/ra-common-python/ common/ra-common-python/
COPY seda-bus/seda-bus-python/ seda-bus/seda-bus-python/
COPY seda-bus/seda-bus-compare/bench/python/ seda-bus/seda-bus-compare/bench/python/
WORKDIR /src/seda-bus/seda-bus-compare/bench/python
ENTRYPOINT ["python3", "bench.py"]
