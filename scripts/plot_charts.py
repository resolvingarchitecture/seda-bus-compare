#!/usr/bin/env python3
"""Reads results/summary.csv and writes results/charts/throughput.svg and
results/charts/latency.svg — the same data as the tables in RESULTS.md,
as grouped bar charts. Run after scripts/aggregate.py.
"""

import csv
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

matplotlib.use("Agg")

HERE = Path(__file__).resolve().parent
COMPARE_DIR = HERE.parent
SUMMARY_CSV = COMPARE_DIR / "results" / "summary.csv"
CAPACITY_SUMMARY_CSV = COMPARE_DIR / "results" / "capacity_summary.csv"
CHARTS_DIR = COMPARE_DIR / "results" / "charts"

# variant (as it appears in summary.csv) -> display label, in the same order
# as RESULTS.md's tables (ranked by chan throughput, matching aggregate.py).
LABELS = {
    "java": "Java",
    "go": "Go",
    "rust": "Rust",
    "cs": "C#",
    "cpp": "C++",
    "python 3.14.0 (free-threaded)": "Python 3.14t",
    "ts": "TypeScript",
    "python 3.13.15 (GIL)": "Python 3.13 (GIL)",
}
ORDER = list(LABELS.keys())
CONFIGS = ["seq", "par", "chan"]
CONFIG_COLORS = {"seq": "#4C72B0", "par": "#DD8452", "chan": "#55A868"}


def load_rows() -> dict:
    rows = {}
    with SUMMARY_CSV.open() as f:
        for row in csv.DictReader(f):
            rows[(row["variant"], row["config"])] = row
    return rows


def plot_throughput(rows: dict) -> None:
    fig, ax = plt.subplots(figsize=(11, 6))
    x = range(len(ORDER))
    width = 0.26
    for i, config in enumerate(CONFIGS):
        values = [float(rows[(v, config)]["mean_eps"]) for v in ORDER]
        offsets = [xi + (i - 1) * width for xi in x]
        bars = ax.bar(offsets, values, width, label=config, color=CONFIG_COLORS[config])
        for bar, val in zip(bars, values):
            label = f"{val / 1000:.0f}k" if val < 1_000_000 else f"{val / 1_000_000:.2f}M"
            ax.annotate(
                label,
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                textcoords="offset points",
                xytext=(0, 3),
                ha="center",
                fontsize=7,
                rotation=90,
            )

    ax.set_yscale("log")
    ax.set_ylabel("throughput (envelopes/sec, log scale)")
    ax.set_title("seda-bus-compare: throughput by implementation and configuration")
    ax.set_xticks(list(x))
    ax.set_xticklabels([LABELS[v] for v in ORDER], rotation=20, ha="right")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.legend(title="config")
    ax.grid(axis="y", which="major", linestyle="--", alpha=0.4)
    fig.tight_layout()
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHARTS_DIR / "throughput.svg")
    plt.close(fig)


def plot_latency(rows: dict) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(13, 10))
    percentiles = [("p50_us", "p50"), ("p99_us", "p99"), ("p999_us", "p999"), ("max_us", "max")]
    x = range(len(ORDER))
    width = 0.26

    for ax, (field, title) in zip(axes.flat, percentiles):
        for i, config in enumerate(CONFIGS):
            values = [float(rows[(v, config)][field]) for v in ORDER]
            offsets = [xi + (i - 1) * width for xi in x]
            ax.bar(offsets, values, width, label=config, color=CONFIG_COLORS[config])
        ax.set_yscale("log")
        ax.set_title(title)
        ax.set_ylabel("microseconds (log scale)")
        ax.set_xticks(list(x))
        ax.set_xticklabels([LABELS[v] for v in ORDER], rotation=30, ha="right", fontsize=8)
        ax.grid(axis="y", which="major", linestyle="--", alpha=0.4)

    axes.flat[0].legend(title="config")
    fig.suptitle("seda-bus-compare: latency (p50 / p99 / p999 / max) by implementation and configuration")
    fig.tight_layout()
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHARTS_DIR / "latency.svg")
    plt.close(fig)


def load_capacity_rows() -> dict:
    rows = {}
    if not CAPACITY_SUMMARY_CSV.exists():
        return rows
    with CAPACITY_SUMMARY_CSV.open() as f:
        for row in csv.DictReader(f):
            key = (row["variant"], row["config"], float(row["load_fraction"]))
            rows[key] = row
    return rows


def plot_capacity_curve(rows: dict) -> None:
    if not rows:
        return
    variants = sorted({v for (v, _, _) in rows})
    load_fractions = [0.5, 1.0, 1.5]
    colors = plt.get_cmap("tab10").colors

    fig, axes = plt.subplots(2, 2, figsize=(13, 10))
    for col, config in enumerate(("cap1", "cap8")):
        ax_ratio = axes[0][col]
        ax_p50 = axes[1][col]
        for i, variant in enumerate(variants):
            xs, ratios, p50s = [], [], []
            for lf in load_fractions:
                row = rows.get((variant, config, lf))
                if row is None or row["achieved_vs_target"] == "":
                    continue
                xs.append(lf)
                ratios.append(float(row["achieved_vs_target"]))
                p50s.append(float(row["p50_us"]))
            if not xs:
                continue
            label = LABELS.get(variant, variant)
            color = colors[i % len(colors)]
            ax_ratio.plot(xs, ratios, marker="o", label=label, color=color)
            ax_p50.plot(xs, p50s, marker="o", label=label, color=color)

        ax_ratio.axhline(1.0, color="gray", linestyle=":", linewidth=1)
        ax_ratio.set_title(f"{config}: achieved / target rate")
        ax_ratio.set_xlabel("load fraction (x sustained capacity)")
        ax_ratio.set_ylabel("achieved / target")
        ax_ratio.set_xticks(load_fractions)
        ax_ratio.grid(axis="y", which="major", linestyle="--", alpha=0.4)

        ax_p50.set_yscale("log")
        ax_p50.set_title(f"{config}: p50 latency")
        ax_p50.set_xlabel("load fraction (x sustained capacity)")
        ax_p50.set_ylabel("p50 (us, log scale)")
        ax_p50.set_xticks(load_fractions)
        ax_p50.grid(axis="y", which="major", linestyle="--", alpha=0.4)

    axes[0][0].legend(fontsize=8)
    fig.suptitle(
        "seda-bus-compare: capacity curve - achieved/target rate and p50 latency "
        "as load approaches and exceeds sustained capacity"
    )
    fig.tight_layout()
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHARTS_DIR / "capacity_curve.svg")
    plt.close(fig)


def main() -> None:
    rows = load_rows()
    plot_throughput(rows)
    plot_latency(rows)
    print(f"wrote {CHARTS_DIR / 'throughput.svg'}")
    print(f"wrote {CHARTS_DIR / 'latency.svg'}")

    capacity_rows = load_capacity_rows()
    if capacity_rows:
        plot_capacity_curve(capacity_rows)
        print(f"wrote {CHARTS_DIR / 'capacity_curve.svg'}")
    else:
        print(f"no {CAPACITY_SUMMARY_CSV} yet - skipping capacity curve chart")


if __name__ == "__main__":
    main()
