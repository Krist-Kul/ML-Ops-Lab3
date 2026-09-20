"""Lab 2 — rank tracked runs by metric AND by cost per point.

    python scripts/compare_runs.py --experiment itcs355-lab2

Writes reports/lab2-comparison.md. The cost-per-point column is what the lab is about:
the highest-scoring run is frequently not the one you should register.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow
import pandas as pd

from src import config, costs


JUSTIFICATION = Path("reports/lab2-justification.md")
PRICES = Path("reports/lab2-prices.md")


def cost_model(provider: str) -> str:
    """What the THB columns were priced with, straight from src/costs.py."""
    rates = costs.PRICE_TABLE.get(provider, {})
    spot = costs.SPOT_FACTORS.get(provider, {})
    lines = ["| instance | THB/hour on demand | spot factor | THB/hour spot |",
             "|---|---|---|---|"]
    for inst, thb in sorted(rates.items()):
        f = spot.get(inst, costs.SPOT_FACTOR)
        lines.append(f"| {inst} | {thb:.2f} | {f:.2f} | {thb * f:.2f} |")
    if PRICES.exists():
        lines += ["", PRICES.read_text().strip()]
    return "\n".join(lines)


def justification() -> str:
    """The written argument lives in its own file so regenerating this table cannot eat it."""
    if JUSTIFICATION.exists():
        return JUSTIFICATION.read_text().strip()
    return "\n".join([
        "TODO(Lab 2): 200 words maximum. Must address all four:", "",
        "1. Why this model rather than the highest-scoring one, if they differ",
        "2. The variance across seeds for your chosen configuration",
        "3. What it costs to train, and to retrain monthly",
        "4. One way this choice could be wrong", "",
        'An answer that only says "highest validation score" scores zero on this task.',
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment", default="itcs355-lab2")
    ap.add_argument("--metric", default="val_roc_auc")
    ap.add_argument("--out", type=Path, default=Path("reports/lab2-comparison.md"))
    args = ap.parse_args()

    cfg = config.load(strict=False)
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    exp = mlflow.get_experiment_by_name(args.experiment)
    if exp is None:
        print(f"No experiment named {args.experiment!r}. Run `make tune` first.")
        return 1

    runs = mlflow.search_runs(experiment_ids=[exp.experiment_id])
    if "tags.role" in runs:
        # The registration run (scripts/register_model.py) trains the chosen configuration
        # again from a clean commit. It lives in the same experiment because it belongs to
        # the same piece of work, but it is not a trial and would be counted as a 13th.
        runs = runs[runs["tags.role"].fillna("") != "registered"]
    if runs.empty:
        print("No runs found.")
        return 1

    metric_col = f"metrics.{args.metric}"
    cost_col = "metrics.cost_thb"
    baseline = runs[metric_col].min()

    table = pd.DataFrame({
        "trial": runs.get("tags.trial"),
        "run_id": runs["run_id"].str[:8],
        # The three hyperparameters src/tune.py varies. Anything held fixed belongs in the
        # handout, not in a column of identical values.
        "max_depth": runs.get("params.max_depth"),
        "max_features": runs.get("params.max_features"),
        "class_weight": runs.get("params.class_weight"),
        args.metric: runs[metric_col].round(4),
        "test_roc_auc": runs.get("metrics.test_roc_auc").round(4),
        # Repriced from the trial's own running time at the rates in src/costs.py, which
        # were verified after this study ran. The figure the study logged at the time is
        # in metrics.cost_thb and in reports/lab2-study.md.
        "cost_thb": [
            round(d / 3600 * costs.hourly_rate(cfg.provider, inst, spot=str(sp) == "True"), 4)
            for d, inst, sp in zip(runs["metrics.duration_s"], runs["params.instance"],
                                   runs["params.spot"])
        ],
        "logged_thb": runs.get(cost_col, 0).round(4),
    })
    gain = table[args.metric] - baseline
    # The worst trial is the baseline it is measured against, so its own cost per point is
    # undefined. The scaffold's 1e-9 floor turned that into a six-figure number.
    table["thb_per_point"] = (table["cost_thb"] / (gain * 100)).round(4).where(gain > 0)
    table = table.sort_values(args.metric, ascending=False)
    table["thb_per_point"] = table["thb_per_point"].map(
        lambda v: "—" if pd.isna(v) else f"{v:.4f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Lab 2 — Run comparison",
        "",
        f"Experiment `{args.experiment}` · {len(table)} trials · "
        f"{table['cost_thb'].sum():.4f} THB of trial compute, repriced at the verified rates "
        "below. That is fitting time only: the job that ran the study also paid for container "
        "startup and data loading, and reports/lab2-study.md has that larger, real bill.",
        "",
        "`thb_per_point` is cost per percentage point of "
        f"{args.metric} above the worst trial. Cheap improvements rank low; expensive "
        "improvements rank high, however good the headline number is.",
        "",
        table.to_markdown(index=False),
        "",
        f"Spread in {args.metric}: {table[args.metric].max() - table[args.metric].min():.4f} "
        f"across all trials, {table[args.metric].nlargest(5).max() - table[args.metric].nlargest(5).min():.4f} "
        "across the best five. Compare both against reports/lab2-seed-variance.md before "
        "believing any ranking here.",
        "",
        "## Cost model",
        "",
        cost_model(cfg.provider),
        "",
        "## Which model did you register, and why?",
        "",
        justification(),
    ]
    args.out.write_text("\n".join(lines))
    print(f"wrote {args.out}  ({len(table)} trials)")
    print(table.head(5).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
