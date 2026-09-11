#!/usr/bin/env python3
"""Reads results/raw/*.jsonl (one JSON object per trial, see bench/WORKLOAD.md's
output contract), writes results/summary.csv, and prints a Markdown table
(the one embedded in ../RESULTS.md) to stdout.
"""

import csv
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPARE_DIR = HERE.parent
RAW_DIR = COMPARE_DIR / "results" / "raw"
SUMMARY_CSV = COMPARE_DIR / "results" / "summary.csv"


def variant_label(row: dict) -> str:
    lang = row["language"]
    if lang == "python":
        gil = row.get("gil_enabled")
        ver = row.get("python_version", "")
        if gil is True:
            return f"python {ver} (GIL)"
        if gil is False:
            return f"python {ver} (free-threaded)"
        return "python"
    return lang


def load_rows() -> list[dict]:
    rows = []
    for path in sorted(RAW_DIR.glob("*.jsonl")):
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                row["_source_file"] = path.name
                rows.append(row)
    return rows


def main() -> None:
    rows = load_rows()
    if not rows:
        print(f"no *.jsonl files found in {RAW_DIR}", file=sys.stderr)
        sys.exit(1)

    invalid = [r for r in rows if not r.get("drained", False) or r.get("delivered") != r.get("total")]
    if invalid:
        print(f"WARNING: {len(invalid)} invalid (non-drained or short-delivered) trial(s):", file=sys.stderr)
        for r in invalid:
            print(f"  {r['_source_file']}: {r}", file=sys.stderr)

    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (variant_label(r), r["config"])
        groups.setdefault(key, []).append(r)

    summary_rows = []
    for (variant, config), trials in sorted(groups.items()):
        throughputs = [t["throughput_eps"] for t in trials]
        summary_rows.append(
            {
                "variant": variant,
                "config": config,
                "trials": len(trials),
                "producers": trials[0]["producers"],
                "min_eps": round(min(throughputs)),
                "mean_eps": round(statistics.mean(throughputs)),
                "max_eps": round(max(throughputs)),
            }
        )

    SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"wrote {SUMMARY_CSV}", file=sys.stderr)

    # Markdown table, one row per variant, seq and par mean throughput side by side.
    by_variant: dict[str, dict[str, dict]] = {}
    for row in summary_rows:
        by_variant.setdefault(row["variant"], {})[row["config"]] = row

    print("| Implementation | seq mean eps | seq range | par mean eps | par range | par producers | par speedup |")
    print("|---|---|---|---|---|---|---|")
    for variant, configs in sorted(by_variant.items(), key=lambda kv: -kv[1].get("par", kv[1].get("seq", {})).get("mean_eps", 0)):
        seq = configs.get("seq")
        par = configs.get("par")
        seq_mean = seq["mean_eps"] if seq else None
        par_mean = par["mean_eps"] if par else None
        seq_range = f"{seq['min_eps']}-{seq['max_eps']}" if seq else "-"
        par_range = f"{par['min_eps']}-{par['max_eps']}" if par else "-"
        par_producers = par["producers"] if par else "-"
        speedup = f"{par_mean / seq_mean:.2f}x" if seq_mean and par_mean else "-"
        print(
            f"| {variant} | {seq_mean or '-'} | {seq_range} | {par_mean or '-'} | {par_range} "
            f"| {par_producers} | {speedup} |"
        )


if __name__ == "__main__":
    main()
