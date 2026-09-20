"""Lab 2 Task 3 — how much does the seed move the chosen configuration?

    python scripts/seed_variance.py

A single seed does two jobs in this repo: it deals the grouped train/val/test split AND
seeds the forest. They are separate sources of variance and the lab's question ("the
variance across seeds for your chosen configuration") is only answerable if you take them
apart, so this measures each one on its own:

  A. one fixed split, five different forest seeds  -> variance from the model alone
  B. five splits, each with its own forest seed    -> what `--seed N` actually moves

Run locally on purpose: the metric is a property of the data and the model, not of the
platform, and the managed job reproduces the local number exactly (Lab 2 Task 1 checked).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.metrics import average_precision_score, roc_auc_score  # noqa: E402

from src import config, data  # noqa: E402

# The registered configuration. Keep in step with reports/lab2-justification.md.
CHOSEN = {"n_estimators": 200, "max_depth": 4, "min_samples_leaf": 5,
          "max_features": "sqrt", "class_weight": None}
SEEDS = [20260101, 7, 1234, 99, 4242]


def score(df, split_seed: int, model_seed: int) -> dict:
    train_df, val_df, test_df = data.split(df, seed=split_seed)
    model = RandomForestClassifier(random_state=model_seed, n_jobs=-1, **CHOSEN)
    model.fit(train_df[data.FEATURES], train_df[data.TARGET])
    out = {"split_seed": split_seed, "model_seed": model_seed}
    for name, part in (("val", val_df), ("test", test_df)):
        proba = model.predict_proba(part[data.FEATURES])[:, 1]
        out[f"{name}_roc_auc"] = float(roc_auc_score(part[data.TARGET], proba))
        out[f"{name}_pr_auc"] = float(average_precision_score(part[data.TARGET], proba))
    return out


def spread(rows: list[dict], metric: str) -> tuple[float, float, float]:
    xs = [r[metric] for r in rows]
    return min(xs), max(xs), statistics.pstdev(xs)


def table(rows: list[dict], first_col: str) -> list[str]:
    lines = [f"| {first_col} | val ROC AUC | val PR AUC | test ROC AUC | test PR AUC |",
             "|---|---|---|---|---|"]
    for r in rows:
        key = r["model_seed"] if first_col == "forest seed" else r["split_seed"]
        lines.append(f"| {key} | {r['val_roc_auc']:.4f} | {r['val_pr_auc']:.4f} | "
                     f"{r['test_roc_auc']:.4f} | {r['test_pr_auc']:.4f} |")
    for metric in ("val_roc_auc", "test_roc_auc"):
        lo, hi, sd = spread(rows, metric)
        lines.append(f"| **{metric} spread** | {hi - lo:.4f} | (min {lo:.4f}, max {hi:.4f}, "
                     f"sd {sd:.4f}) | | |")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("reports/lab2-seed-variance.md"))
    args = ap.parse_args()

    cfg = config.load(strict=False)
    df = data.load_raw(cfg.raw_path)

    model_only = [score(df, SEEDS[0], s) for s in range(1, len(SEEDS) + 1)]
    split_too = [score(df, s, s) for s in SEEDS]

    lines = [
        "# Lab 2 — seed variance for the chosen configuration", "",
        f"`{CHOSEN}`, data fingerprint `{data.data_fingerprint(cfg.raw_path)}`.", "",
        "## A. Forest seed only (split held at 20260101)", "",
        *table(model_only, "forest seed"), "",
        "## B. Seed changes the split as well — what `--seed N` does", "",
        *table(split_too, "seed"), "",
        "The split-grouped design means a new seed deals different machines into test, so B "
        "is dominated by which machines land where, not by the model. B is the number to "
        "compare trial-to-trial differences against.", "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines))
    args.out.with_suffix(".json").write_text(json.dumps(
        {"chosen": CHOSEN, "forest_seed_only": model_only, "seed_moves_split": split_too}, indent=2))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
