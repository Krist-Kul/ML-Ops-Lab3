"""Lab 2 — budgeted hyperparameter study.

Managed:  make tune          (spot compute, state in object storage — the graded path)
Local:    make tune-local    (zero cost, for developing this file only)

The budget is enforced, not advisory. The study stops when projected spend would exceed
it, and reports what it did not get to. This is the habit the lab is teaching: compute is
a resource you spend deliberately, and a trial that is 0.3% better and four times the cost
is not better.

Every trial records its estimated cost alongside its metric, so `scripts/compare_runs.py`
can rank by cost per point rather than by metric alone.

Two modes, one loop:
  * without --artifact-dir, each trial is logged straight to MLflow (local development);
  * with --artifact-dir, the study directory is the record: checkpoint.json holds every
    finished trial's result and trials/<id>/model holds its model. A managed job's disk
    dies with it, so nothing that matters is written anywhere else, and
    scripts/tune_remote.py logs the trials into MLflow once the study is done.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import shutil
import time
from pathlib import Path

import mlflow
import mlflow.sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from src import config, costs, data, seeds
from src.train import git_commit

# Three hyperparameters, three different ideas about the model:
SEARCH_SPACE: dict[str, list] = {
    # Complexity. Depth is what moved the metric in Lab 1; None grows every tree out.
    "max_depth": [4, 8, None],
    # Decorrelation. "sqrt" is a random forest; 1.0 is plain bagging of full-feature trees.
    "max_features": ["sqrt", 1.0],
    # Imbalance. ~12% positives; weighting changes which splits the trees look for.
    "class_weight": [None, "balanced_subsample"],
}
# Held fixed on evidence rather than habit: in Lab 1, 600 trees bought nothing over 200,
# and leaf size is a second knob on the same complexity idea as depth.
FIXED: dict = {"n_estimators": 200, "min_samples_leaf": 5}


def grid(space: dict[str, list]) -> list[dict]:
    keys = list(space)
    return [dict(zip(keys, values)) for values in itertools.product(*(space[k] for k in keys))]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 2 — budgeted study")
    p.add_argument("--trials", type=int, default=12, help="minimum 12 for the lab")
    p.add_argument("--budget-thb", type=float, default=150.0)
    p.add_argument("--instance", default="local", help="key into src/costs.py PRICE_TABLE")
    p.add_argument("--spot", action="store_true", help="price trials at the discounted rate")
    p.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    p.add_argument("--experiment", default="itcs355-lab2")
    p.add_argument("--data-path", type=Path, default=None,
                   help="Raw CSV to train on. Defaults to config's raw_path.")
    p.add_argument("--artifact-dir", type=Path, default=None,
                   help="Study directory. Holds the checkpoint and every trial's model.")
    p.add_argument("--checkpoint", type=Path, default=None,
                   help="Resume file. Defaults to <artifact-dir>/checkpoint.json, "
                        "else reports/tune_checkpoint.json.")
    p.add_argument("--interrupt-at", type=int, default=None,
                   help="Drill: kill the process partway through this trial, once per study.")
    return p.parse_args()


def load_checkpoint(path: Path) -> dict:
    state = json.loads(path.read_text()) if path.exists() else {}
    return {"completed": [], "spent_thb": 0.0, "results": [], **state}


def save_checkpoint(path: Path, state: dict) -> None:
    # Write-then-rename, so an interruption mid-write cannot leave half a checkpoint.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def main() -> None:
    args = parse_args()
    cfg = config.load(strict=False)
    seed = seeds.set_all(args.seed)

    raw_path = args.data_path or cfg.raw_path
    df = data.load_raw(raw_path)
    fingerprint = data.data_fingerprint(raw_path)
    train_df, val_df, test_df = data.split(df, seed=seed)

    study_dir = args.artifact_dir
    ckpt = args.checkpoint or (study_dir / "checkpoint.json" if study_dir
                               else Path("reports/tune_checkpoint.json"))
    if not study_dir:
        mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
        mlflow.set_experiment(args.experiment)

    state = load_checkpoint(ckpt)
    candidates = grid(SEARCH_SPACE)
    if args.trials < len(candidates):
        # A prefix of itertools.product varies only the last keys. Sample the grid instead.
        candidates = random.Random(seed).sample(candidates, args.trials)
    elif args.trials > len(candidates):
        print(f"grid has {len(candidates)} configurations; running all of them")
    provider = "local" if args.instance == "local" else cfg.provider
    rate = costs.hourly_rate(provider, args.instance, spot=args.spot)
    commit = git_commit()

    skipped: list[dict] = []
    for i, params in enumerate(candidates):
        trial = f"trial-{i:02d}"
        key = json.dumps(params, sort_keys=True)
        if key in state["completed"]:
            print(f"{trial}: already done, skipping (resumed from checkpoint)")
            continue

        # Projected, not spent: the next trial is assumed to cost what the average one did.
        done = state["results"]
        projected = state["spent_thb"] + (state["spent_thb"] / len(done) if done else 0.0)
        if projected > args.budget_thb:
            skipped.append(params)
            continue

        started = time.perf_counter()
        model = RandomForestClassifier(random_state=seed, n_jobs=-1, **FIXED, **params)
        model.fit(train_df[data.FEATURES], train_df[data.TARGET])

        metrics = {}
        for name, part in (("val", val_df), ("test", test_df)):
            proba = model.predict_proba(part[data.FEATURES])[:, 1]
            metrics[f"{name}_roc_auc"] = float(roc_auc_score(part[data.TARGET], proba))
            metrics[f"{name}_pr_auc"] = float(average_precision_score(part[data.TARGET], proba))

        if args.interrupt_at == i and not state.get("drill_interrupted"):
            # Recorded first so a resumed run does not repeat the drill.
            state["drill_interrupted"] = trial
            save_checkpoint(ckpt, state)
            print(f"{trial}: DRILL — process killed mid-trial, before its checkpoint", flush=True)
            os._exit(137)

        model_path = None
        if study_dir:
            model_path = f"trials/{trial}/model"
            shutil.rmtree(study_dir / model_path, ignore_errors=True)  # a half-written one
            mlflow.sklearn.save_model(model, str(study_dir / model_path))

        elapsed_s = time.perf_counter() - started
        record = {
            "trial": trial, "params": {**FIXED, **params}, "seed": seed,
            "instance": args.instance, "spot": args.spot, **metrics,
            "duration_s": round(elapsed_s, 3), "cost_thb": round(elapsed_s / 3600 * rate, 6),
            "git_commit": commit, "data_fingerprint": fingerprint, "model_path": model_path,
        }

        if not study_dir:
            with mlflow.start_run(run_name=trial):
                mlflow.log_params({**record["params"], "seed": seed,
                                   "instance": args.instance, "spot": args.spot})
                mlflow.log_metrics({**metrics, "duration_s": record["duration_s"],
                                    "cost_thb": record["cost_thb"]})
                mlflow.set_tags({"git_commit": commit, "data_fingerprint": fingerprint,
                                 "lab": "2", "trial": trial})
                mlflow.sklearn.log_model(model, name="model")

        state["completed"].append(key)
        state["results"].append(record)
        state["spent_thb"] += record["cost_thb"]
        save_checkpoint(ckpt, state)
        print(f"{trial}: {params} -> val_roc_auc={metrics['val_roc_auc']:.4f} "
              f"{elapsed_s:.1f}s cost={record['cost_thb']:.4f} THB  "
              f"cumulative={state['spent_thb']:.4f}", flush=True)

    state["done"] = True
    state["skipped"] = skipped
    save_checkpoint(ckpt, state)
    print(f"\nspent {state['spent_thb']:.4f} of {args.budget_thb} THB on trial compute")
    if skipped:
        print(f"BUDGET EXHAUSTED — {len(skipped)} configurations not run:")
        for s in skipped:
            print(f"  {s}")
        print("Report this in your README. Which trials you could not afford is a finding, "
              "not an embarrassment.")


if __name__ == "__main__":
    main()
