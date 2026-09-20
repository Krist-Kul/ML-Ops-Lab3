"""Training entry point.

Run locally:      python -m src.train --n-estimators 200 --max-depth 8
Run in Docker:    make reproduce

Every run logs: all hyperparameters, the seed, validation AND test metrics separately,
the data fingerprint, and the Git commit. A metric that cannot be traced to code and
data is not evidence of anything.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import mlflow
import mlflow.sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from src import config, data, seeds


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, cwd=config.REPO_ROOT,
        )
        return out.stdout.strip()
    except Exception:
        # Inside the image there is no .git; `make image` bakes the commit in instead.
        return os.environ.get("GIT_COMMIT", "unknown")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 1 — reproducible training")
    p.add_argument("--n-estimators", type=int, default=200)
    p.add_argument("--max-depth", type=int, default=8)
    p.add_argument("--min-samples-leaf", type=int, default=5)
    # The other two knobs src/tune.py varies, so a trial the study chose can be retrained
    # here exactly — a registered model must be reproducible from this entry point alone.
    p.add_argument("--max-features", default="sqrt", help="sqrt | log2 | a float like 1.0")
    p.add_argument("--class-weight", default=None, choices=[None, "balanced",
                                                            "balanced_subsample"])
    p.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    p.add_argument("--experiment", default="itcs355-lab1")
    p.add_argument("--run-name", default=None)
    p.add_argument("--metrics-out", type=Path, default=None,
                   help="Write final metrics as JSON. Used by `make verify`.")
    p.add_argument("--data-path", type=Path, default=None,
                   help="Raw CSV to train on. Defaults to config's raw_path. A managed job "
                        "points this at object storage, as the adapter mounts it.")
    p.add_argument("--artifact-dir", type=Path, default=None,
                   help="Also write metrics.json and the model here. A managed job points "
                        "this at object storage so its output outlives the container.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = config.load(strict=False)
    seed = seeds.set_all(args.seed)

    raw_path = args.data_path or cfg.raw_path
    df = data.load_raw(raw_path)
    fingerprint = data.data_fingerprint(raw_path)
    train_df, val_df, test_df = data.split(df, seed=seed)

    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    mlflow.set_experiment(args.experiment)

    with mlflow.start_run(run_name=args.run_name) as run:
        max_features = args.max_features
        if max_features not in ("sqrt", "log2"):
            max_features = float(max_features)
        mlflow.log_params({
            "n_estimators": args.n_estimators,
            "max_depth": args.max_depth,
            "min_samples_leaf": args.min_samples_leaf,
            "max_features": max_features,
            "class_weight": args.class_weight,
            "seed": seed,
            "n_features": len(data.FEATURES),
        })
        # Provenance. This is what makes the metric traceable.
        mlflow.set_tags({
            "git_commit": git_commit(),
            "data_fingerprint": fingerprint,
            "split_strategy": "group_by_machine_id",
            "n_train_rows": len(train_df),
            "n_val_rows": len(val_df),
            "n_test_rows": len(test_df),
        })

        model = RandomForestClassifier(
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            min_samples_leaf=args.min_samples_leaf,
            max_features=max_features,
            class_weight=args.class_weight,
            random_state=seed,
            n_jobs=-1,
        )
        model.fit(train_df[data.FEATURES], train_df[data.TARGET])

        metrics: dict[str, float] = {}
        for name, part in (("val", val_df), ("test", test_df)):
            proba = model.predict_proba(part[data.FEATURES])[:, 1]
            metrics[f"{name}_roc_auc"] = float(roc_auc_score(part[data.TARGET], proba))
            metrics[f"{name}_pr_auc"] = float(average_precision_score(part[data.TARGET], proba))
        mlflow.log_metrics(metrics)
        mlflow.sklearn.log_model(model, name="model")

        print(json.dumps({"seed": seed, "data_fingerprint": fingerprint, **metrics}, indent=2))
        if args.metrics_out:
            args.metrics_out.parent.mkdir(parents=True, exist_ok=True)
            args.metrics_out.write_text(json.dumps(
                {"seed": seed, "data_fingerprint": fingerprint, **metrics}, indent=2))
        if args.artifact_dir:
            # Written straight to the target, not via a local copy: on a managed job the
            # container's disk is discarded the moment the process exits.
            args.artifact_dir.mkdir(parents=True, exist_ok=True)
            (args.artifact_dir / "metrics.json").write_text(json.dumps({
                "seed": seed, "data_fingerprint": fingerprint,
                "mlflow_run_id": run.info.run_id, **metrics,
            }, indent=2))
            mlflow.sklearn.save_model(model, str(args.artifact_dir / "model"))
            print(f"artifacts written to {args.artifact_dir}")


if __name__ == "__main__":
    main()
