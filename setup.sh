#!/usr/bin/env bash
# Verifies (and can clone) the sibling repos this benchmark depends on, at
# the layout scripts/build_and_run.sh expects:
#
#   <root>/common/ra-common-{java,rust,python,ts,cpp,cs,go}
#   <root>/seda-bus/seda-bus-{java,rust,python,ts,cpp,cs,go}
#   <root>/seda-bus/seda-bus-compare        <- this repo
#
# These are separate GitHub repos under github.com/resolvingarchitecture,
# not published packages — that's why this exists, and why the benchmark is
# Dockerized (see METHODOLOGY.md): the source has to actually be present at
# this relative layout, but nothing about any individual toolchain version
# has to be installed on your machine beyond Docker itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEDA_BUS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RA_ROOT="$(cd "$SEDA_BUS_DIR/.." && pwd)"

LANGS=(java rust python ts cpp cs go)
ORG="https://github.com/resolvingarchitecture"

missing=0
check_or_clone() {
  local dir="$1" repo="$2"
  if [ -d "$dir" ]; then
    echo "  ok    $dir"
  else
    echo "  MISSING $dir"
    if [ "${SETUP_CLONE:-0}" = "1" ]; then
      git clone "$ORG/$repo.git" "$dir"
    else
      missing=1
    fi
  fi
}

echo "common/ra-common-*:"
for lang in "${LANGS[@]}"; do
  check_or_clone "$RA_ROOT/common/ra-common-$lang" "ra-common-$lang"
done

echo "seda-bus/seda-bus-*:"
for lang in "${LANGS[@]}"; do
  check_or_clone "$RA_ROOT/seda-bus/seda-bus-$lang" "seda-bus-$lang"
done

if [ "$missing" = "1" ]; then
  echo
  echo "Some repos are missing. Re-run with SETUP_CLONE=1 $0 to clone them," \
       "or clone them yourself at the paths listed above."
  exit 1
fi

echo
echo "All sibling repos present. Next: ./scripts/build_and_run.sh"
