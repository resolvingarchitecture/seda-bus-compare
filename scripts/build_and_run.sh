#!/usr/bin/env bash
# Builds and runs every bench/<lang> Docker image, writing each language's
# raw JSON-lines output to results/raw/<lang>.jsonl. Run from anywhere; paths
# are computed relative to this script.
#
# Requires: Docker. Requires the sibling repos this benchmark depends on to
# be checked out at the standard monorepo layout (../../common/ra-common-*,
# ../seda-bus-*) — see ../setup.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPARE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SEDA_BUS_DIR="$(cd "$COMPARE_DIR/.." && pwd)"
RA_ROOT="$(cd "$SEDA_BUS_DIR/.." && pwd)"
RAW_DIR="$COMPARE_DIR/results/raw"
mkdir -p "$RAW_DIR"

build_and_run() {
  local lang="$1"
  local dockerfile="$2"
  local out="$3"
  local image="seda-bus-bench-$lang"
  echo "== $lang =="
  docker build -f "$COMPARE_DIR/bench/$lang/$dockerfile" -t "$image" "$RA_ROOT"
  docker run --rm "$image" | tee "$RAW_DIR/$out"
}

build_and_run go Dockerfile go.jsonl
build_and_run rust Dockerfile rust.jsonl
build_and_run cpp Dockerfile cpp.jsonl
build_and_run cs Dockerfile cs.jsonl
build_and_run java Dockerfile java.jsonl
build_and_run ts Dockerfile ts.jsonl
build_and_run python Dockerfile python-gil.jsonl

echo "== python (free-threaded) =="
docker build -f "$COMPARE_DIR/bench/python/Dockerfile.freethreaded" -t seda-bus-bench-python-freethreaded "$RA_ROOT"
docker run --rm seda-bus-bench-python-freethreaded | tee "$RAW_DIR/python-freethreaded.jsonl"

echo
echo "All raw results in $RAW_DIR. Now run: python3 scripts/aggregate.py"
