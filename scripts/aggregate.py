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
        has_latency = all("p50_us" in t for t in trials)
        summary_rows.append(
            {
                "variant": variant,
                "config": config,
                "trials": len(trials),
                "producers": trials[0]["producers"],
                "channels": trials[0].get("channels", 1),
                "min_eps": round(min(throughputs)),
                "mean_eps": round(statistics.mean(throughputs)),
                "max_eps": round(max(throughputs)),
                # p50/p99/p999: mean-of-trials, same treatment as throughput.
                # max_us: true max across trials (worst case), not averaged -
                # more useful for "what's the worst tail latency we saw."
                "p50_us": round(statistics.mean(t["p50_us"] for t in trials), 1) if has_latency else "",
                "p99_us": round(statistics.mean(t["p99_us"] for t in trials), 1) if has_latency else "",
                "p999_us": round(statistics.mean(t["p999_us"] for t in trials), 1) if has_latency else "",
                "max_us": round(max(t["max_us"] for t in trials), 1) if has_latency else "",
            }
        )

    SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"wrote {SUMMARY_CSV}", file=sys.stderr)

    # Markdown table, one row per variant, seq/par/chan mean throughput side by side.
    by_variant: dict[str, dict[str, dict]] = {}
    for row in summary_rows:
        by_variant.setdefault(row["variant"], {})[row["config"]] = row

    def rank(kv):
        configs = kv[1]
        best = configs.get("chan") or configs.get("par") or configs.get("seq") or {}
        return -best.get("mean_eps", 0)

    print("| Implementation | seq mean | par mean | par vs seq | chan mean | chan vs seq |")
    print("|---|--:|--:|--:|--:|--:|")
    ranked_variants = sorted(by_variant.items(), key=rank)
    for variant, configs in ranked_variants:
        seq = configs.get("seq")
        par = configs.get("par")
        chan = configs.get("chan")
        seq_mean = seq["mean_eps"] if seq else None
        par_mean = par["mean_eps"] if par else None
        chan_mean = chan["mean_eps"] if chan else None
        par_ratio = f"{par_mean / seq_mean:.2f}x" if seq_mean and par_mean else "-"
        chan_ratio = f"{chan_mean / seq_mean:.2f}x" if seq_mean and chan_mean else "-"
        print(
            f"| {variant} | {seq_mean or '-'} | {par_mean or '-'} | {par_ratio} "
            f"| {chan_mean or '-'} | {chan_ratio} |"
        )

    # Latency (microseconds): one table, all three configs, same rank order
    # as the throughput table above. p50/p99/p999 are the mean across the 3
    # trials; max is the true max across trials (worst case seen, not
    # averaged away).
    print()
    print("| Implementation | Config | p50 us | p99 us | p999 us | max us |")
    print("|---|---|--:|--:|--:|--:|")
    for variant, configs in ranked_variants:
        for config_name in ("seq", "par", "chan"):
            row = configs.get(config_name)
            if row is None or row["p50_us"] == "":
                continue
            print(
                f"| {variant} | {config_name} | {row['p50_us']} | {row['p99_us']} "
                f"| {row['p999_us']} | {row['max_us']} |"
            )


if __name__ == "__main__":
    main()
