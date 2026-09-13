#!/usr/bin/env python3
"""Reads results/raw/*.jsonl (one JSON object per trial, see bench/WORKLOAD.md's
output contract), writes results/summary.csv (firehose configs: seq/par/chan)
and results/capacity_summary.csv (capacity-curve configs: cap1/cap8), and
prints Markdown tables (the ones embedded in ../RESULTS.md) to stdout.
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
CAPACITY_SUMMARY_CSV = COMPARE_DIR / "results" / "capacity_summary.csv"

FIREHOSE_CONFIGS = ("seq", "par", "chan")
CAPACITY_CONFIGS = ("cap1", "cap8")


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


def build_firehose_summary(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["config"] not in FIREHOSE_CONFIGS:
            continue
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
    return summary_rows


def print_firehose_tables(summary_rows: list[dict]) -> None:
    by_variant: dict[str, dict[str, dict]] = {}
    for row in summary_rows:
        by_variant.setdefault(row["variant"], {})[row["config"]] = row

    def rank(kv):
        configs = kv[1]
        best = configs.get("chan") or configs.get("par") or configs.get("seq") or {}
        return -best.get("mean_eps", 0)

    print("### Firehose throughput (secondary/diagnostic - see WORKLOAD.md)")
    print()
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
    # as the throughput table above. p50/p99/p999 are the mean across
    # trials; max is the true max across trials (worst case seen, not
    # averaged away).
    print()
    print("| Implementation | Config | p50 us | p99 us | p999 us | max us |")
    print("|---|---|--:|--:|--:|--:|")
    for variant, configs in ranked_variants:
        for config_name in FIREHOSE_CONFIGS:
            row = configs.get(config_name)
            if row is None or row["p50_us"] == "":
                continue
            print(
                f"| {variant} | {config_name} | {row['p50_us']} | {row['p99_us']} "
                f"| {row['p999_us']} | {row['max_us']} |"
            )


def build_capacity_summary(rows: list[dict]) -> list[dict]:
    # Grouped by (variant, config, load_fraction) - unlike the firehose
    # configs, each (variant, config) pair here has multiple rows, one per
    # swept load_fraction (0 = Step 1 calibration, else 0.5/1.0/1.5).
    groups: dict[tuple[str, str, float], list[dict]] = {}
    for r in rows:
        if r["config"] not in CAPACITY_CONFIGS:
            continue
        key = (variant_label(r), r["config"], r.get("load_fraction", 0))
        groups.setdefault(key, []).append(r)

    summary_rows = []
    for (variant, config, load_fraction), trials in sorted(groups.items()):
        achieved = [t["throughput_eps"] for t in trials]
        target = [t.get("target_rate_eps", 0) for t in trials]
        depths = [t.get("end_of_window_depth", 0) for t in trials]
        tails = [t.get("drain_tail_ms", 0) for t in trials]
        mean_achieved = statistics.mean(achieved)
        mean_target = statistics.mean(target)
        summary_rows.append(
            {
                "variant": variant,
                "config": config,
                "load_fraction": load_fraction,
                "trials": len(trials),
                "producers": trials[0]["producers"],
                "concurrency": trials[0].get("concurrency", trials[0]["producers"]),
                "capacity": trials[0].get("capacity", ""),
                "mean_target_eps": round(mean_target),
                "mean_achieved_eps": round(mean_achieved),
                # ratio < 1 means the stage could not keep pace with the
                # paced input rate - expected and correct above sustained
                # capacity (load_fraction 1.5), a real finding at or below it.
                "achieved_vs_target": round(mean_achieved / mean_target, 3) if mean_target else "",
                "mean_end_of_window_depth": round(statistics.mean(depths), 1),
                "mean_drain_tail_ms": round(statistics.mean(tails), 1),
                "p50_us": round(statistics.mean(t["p50_us"] for t in trials), 1),
                "p99_us": round(statistics.mean(t["p99_us"] for t in trials), 1),
                "p999_us": round(statistics.mean(t["p999_us"] for t in trials), 1),
                "max_us": round(max(t["max_us"] for t in trials), 1),
            }
        )
    return summary_rows


def print_capacity_tables(summary_rows: list[dict]) -> None:
    by_variant_config: dict[tuple[str, str], dict[float, dict]] = {}
    for row in summary_rows:
        by_variant_config.setdefault((row["variant"], row["config"]), {})[row["load_fraction"]] = row

    def rank(kv):
        by_load = kv[1]
        ref = by_load.get(1.0) or by_load.get(0)
        return -(ref.get("mean_achieved_eps", 0) if ref else 0)

    ranked = sorted(by_variant_config.items(), key=rank)

    print("### Capacity-curve calibration (Step 1: sustained throughput, real bounded capacity)")
    print()
    print("| Implementation | Config | Sustained eps (max of 2 bursts) |")
    print("|---|---|--:|")
    for (variant, config), by_load in ranked:
        cal = by_load.get(0)
        if cal is None:
            continue
        print(f"| {variant} | {config} | {cal['mean_achieved_eps']} |")

    print()
    print("### Capacity-curve sweep (Step 2: paced load at 0.5x / 1.0x / 1.5x sustained capacity)")
    print()
    print(
        "| Implementation | Config | Load | Target eps | Achieved eps | Achieved/Target | "
        "End-of-window depth | Drain tail (ms) | p50 us | p99 us | max us |"
    )
    print("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for (variant, config), by_load in ranked:
        for load_fraction in (0.5, 1.0, 1.5):
            row = by_load.get(load_fraction)
            if row is None:
                continue
            print(
                f"| {variant} | {config} | {load_fraction}x | {row['mean_target_eps']} "
                f"| {row['mean_achieved_eps']} | {row['achieved_vs_target']} "
                f"| {row['mean_end_of_window_depth']} | {row['mean_drain_tail_ms']} "
                f"| {row['p50_us']} | {row['p99_us']} | {row['max_us']} |"
            )


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

    firehose_summary = build_firehose_summary(rows)
    capacity_summary = build_capacity_summary(rows)

    if firehose_summary:
        SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)
        with SUMMARY_CSV.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(firehose_summary[0].keys()))
            writer.writeheader()
            writer.writerows(firehose_summary)
        print(f"wrote {SUMMARY_CSV}", file=sys.stderr)

    if capacity_summary:
        CAPACITY_SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)
        with CAPACITY_SUMMARY_CSV.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(capacity_summary[0].keys()))
            writer.writeheader()
            writer.writerows(capacity_summary)
        print(f"wrote {CAPACITY_SUMMARY_CSV}", file=sys.stderr)

    if capacity_summary:
        print_capacity_tables(capacity_summary)
        print()
    if firehose_summary:
        print_firehose_tables(firehose_summary)


if __name__ == "__main__":
    main()
