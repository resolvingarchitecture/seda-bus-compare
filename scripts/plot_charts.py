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
CHARTS_DIR = COMPARE_DIR / "results" / "charts"

# variant (as it appears in summary.csv) -> display label, in the same order
# as RESULTS.md's tables (ranked by chan throughput, matching aggregate.py).
LABELS = {
    "rust": "Rust",
    "go": "Go",
    "java": "Java",
    "cpp": "C++",
    "cs": "C#",
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


def main() -> None:
    rows = load_rows()
    plot_throughput(rows)
    plot_latency(rows)
    print(f"wrote {CHARTS_DIR / 'throughput.svg'}")
    print(f"wrote {CHARTS_DIR / 'latency.svg'}")


if __name__ == "__main__":
    main()
