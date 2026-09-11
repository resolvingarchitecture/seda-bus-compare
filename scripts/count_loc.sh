#!/usr/bin/env bash
# Source lines of code per seda-bus-* implementation: library source only,
# no tests, no vendored third-party code, no build output. The numbers in
# RESULTS.md's attributes table.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEDA_BUS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

count() {
  local label="$1"
  shift
  local n
  n="$(find "$@" -type f | xargs cat 2>/dev/null | wc -l)"
  printf "%-12s %s\n" "$label" "$n"
}

count "Java"   "$SEDA_BUS_DIR/seda-bus-java/src/main" -iname "*.java"
count "Rust"   "$SEDA_BUS_DIR/seda-bus-rust/src" -iname "*.rs"
count "Python" "$SEDA_BUS_DIR/seda-bus-python/src" -iname "*.py"
count "TS"     "$SEDA_BUS_DIR/seda-bus-ts/src" -iname "*.ts"
count "C++"    "$SEDA_BUS_DIR/seda-bus-cpp/include" -iname "*.hpp"
count "C#"     "$SEDA_BUS_DIR/seda-bus-cs/src" -iname "*.cs"
count "Go"     "$SEDA_BUS_DIR/seda-bus-go" -maxdepth 1 -iname "*.go" -not -iname "*_test.go"
