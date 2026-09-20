"""Lab 2 Task 4 — register the chosen model with lineage, then promote it.

    make register                       # retrain the chosen trial at HEAD, register, stage it
    make register TRIAL=trial-04        # a different trial's configuration
    make promote VERSION=1 ALIAS=production

Why it retrains instead of registering the study's own artifact: the study ran from a
working tree that was not committed, so its trials carry `<sha>-dirty` as their commit.
A lineage field that names a commit nobody can check out answers the question in the
handout — "six months from now, can you rebuild this exact model?" — with no. So the
registered version is produced by one more managed job, from a clean tree, on the same
data, with the trial's hyperparameters and seed; its metrics are compared against the
trial's as a check that the retrain really is the same model. `--from-study` registers
the study artifact instead, for when that history is what you want to keep.

The eight lineage fields go on the registered VERSION, not on the MLflow run. The run
says how the model was made; the version is the thing that gets promoted, deployed and,
later, blamed — and at that point the run is one indirection away at best.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow  # noqa: E402
from train_remote import job_env, resolve_image, stage_data  # noqa: E402

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config, costs  # noqa: E402

STUDY_JSON = Path("reports/lab2-study.json")
REPORT = Path("reports/lab2-registry.md")
RECORD = Path("reports/lab2-registry.json")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group()
    src.add_argument("--image", help="digest-pinned image reference to run")
    src.add_argument("--local-tag", help="local image to push first")
    p.add_argument("--name", default=None, help="registry name; defaults to MODEL_REGISTRY_NAME")
    p.add_argument("--trial", default="trial-00", help="which trial's configuration to register")
    p.add_argument("--study-json", type=Path, default=STUDY_JSON)
    p.add_argument("--from-study", action="store_true",
                   help="register the artifact the study produced, without retraining")
    p.add_argument("--instance", default="n1-standard-4")
    p.add_argument("--spot", action="store_true")
    p.add_argument("--experiment", default="itcs355-lab2")
    p.add_argument("--promote", default="staging",
                   help="alias to move to the new version; empty string to skip")
    p.add_argument("--promote-only", default=None, metavar="VERSION",
                   help="move --promote to this existing version and exit")
    p.add_argument("--allow-dirty", action="store_true",
                   help="register from an uncommitted tree; records <sha>-dirty")
    return p.parse_args()


def git_head(allow_dirty: bool) -> str:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode != 0
    if dirty and not allow_dirty:
        raise SystemExit(
            "working tree is dirty, so the image would be built from code no commit "
            "contains and git_commit would read <sha>-dirty. Commit first, or pass "
            "--allow-dirty and accept that the lineage points nowhere.")
    return f"{sha}-dirty" if dirty else sha


def trial_record(study_json: Path, trial: str) -> tuple[dict, dict, dict]:
    """(trial record, the job that produced it, the study document)."""
    if not study_json.exists():
        raise SystemExit(f"{study_json} not found. Run `make tune` first.")
    doc = json.loads(study_json.read_text())
    records = {r["trial"]: r for r in doc["state"]["results"]}
    if trial not in records:
        raise SystemExit(f"{trial} is not in {study_json}. Have: {sorted(records)}")
    producer = next((a for a in doc["ledger"]["attempts"] if trial in a["trials"]), {})
    return records[trial], producer, doc


def train_args_for(params: dict, seed: int) -> list[str]:
    """The trial's hyperparameters as src.train flags. max_depth=None means unlimited,
    which argparse cannot express as an int, so it is left at the model's default."""
    args = ["--seed", str(seed),
            "--n-estimators", str(params["n_estimators"]),
            "--min-samples-leaf", str(params["min_samples_leaf"]),
            "--max-features", str(params["max_features"])]
    if params["max_depth"] is not None:
        args += ["--max-depth", str(params["max_depth"])]
    if params["class_weight"] is not None:
        args += ["--class-weight", params["class_weight"]]
    return args


def log_run(cfg, experiment: str, name: str, record: dict) -> str:
    """Re-log the managed run into the local tracking store and return its run id.

    The job logs to MLflow inside the container, where the sqlite file dies with the
    container. The run that lineage points at has to be one someone can actually open.
    """
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=name) as run:
        mlflow.log_params({**record["params"], "seed": record["seed"],
                           "instance": record["instance"], "spot": record["spot"]})
        mlflow.log_metrics({k: v for k, v in record["metrics"].items()
                            if isinstance(v, (int, float))})
        mlflow.log_metrics({"duration_s": record["duration_s"],
                            "cost_thb": record["cost_thb"]})
        mlflow.set_tags({
            "lab": "2", "role": "registered", "trial": record["trial"],
            "git_commit": record["git_commit"], "data_fingerprint": record["data_version"],
            "training_job_id": record["training_job_id"], "image": record["image_digest"],
            "model_uri": record["model_uri"],
            "container_run_id": record["metrics"].get("mlflow_run_id", "unknown"),
        })
        return run.info.run_id


def write_report(path: Path, entry: dict, record: dict, comparison: str) -> None:
    lineage = entry["lineage"]
    lines = [
        "# Lab 2 — registered model", "",
        f"`{entry['name']}` version **{entry['version']}** · aliases "
        f"{', '.join('`' + a + '`' for a in entry['aliases']) or '—'} · registered "
        f"{entry['created']}", "",
        f"Resource: `{entry['resource']}`  ",
        f"Artifact: `{entry['artifact_uri']}`", "",
        "## Lineage, on the version", "",
        "Vertex keeps labels and the version description per version, not per model — "
        "checked by registering a second version with a different commit and reading the "
        "first one back. The full values are in the version description; labels carry the "
        "same fields for console filtering, truncated to the 63 characters a GCP label "
        "allows, which is why `image_digest` is short there and complete here.", "",
        "| field | value |", "|---|---|",
    ]
    for k in ("git_commit", "data_version", "mlflow_run_id", "training_job_id",
              "image_digest", "seed", "metric_val", "metric_test"):
        lines.append(f"| `{k}` | `{lineage.get(k, '—')}` |")
    lines += [
        "", "## How this version was produced", "",
        f"Job `{record['training_job_id'].rsplit('/', 1)[-1]}` ran the digest-pinned image "
        f"above on {record['instance']}{' spot' if record['spot'] else ''} for "
        f"{record['duration_s']:.0f} s, costing {record['cost_thb']:.4f} THB. It read the "
        f"data at `{record['data_version']}` from object storage and wrote the model back "
        "to it; nothing on the laptop took part.", "",
        comparison, "",
        "## Reload", "", "```bash",
        f"make reload-check VERSION={entry['version']}", "```", "",
        "`scripts/reload_check.py` resolves name and version through the registry, reads "
        "the artifact URI it gets back, and loads that — no local path, no remembered "
        "bucket key.", "",
        "## Promotion", "",
        f"`{entry['promoted_alias']}` now points at version {entry['version']}. Vertex "
        "aliases are unique within a model, so promoting moves the alias off whatever "
        "held it — the alias, not the version number, is what a service should load. "
        "Note that the `default` alias stays on version 1 unless it is moved explicitly; "
        "registering a newer version does not promote it, which is the correct default "
        "and a surprise to anyone expecting latest-wins.", "",
        "Who may do this, and on what evidence: see README, *Who may promote a model*.",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    cfg = config.load()
    if cfg.provider == "local":
        print("CLOUD_PROVIDER=local has no model registry. Set your provider in cloud.env.")
        return 2
    adapter = get_adapter(cfg)
    name = args.name or cfg.model_registry_name

    if args.promote_only:
        entry = adapter.promote_model(name, args.promote_only, args.promote)
        print(f"{name} version {entry['version']} aliases: {', '.join(entry['aliases'])}")
        return 0

    trial, producer, doc = trial_record(args.study_json, args.trial)
    print(f"trial    {args.trial}: {trial['params']}")

    if args.from_study:
        model_uri = f"{doc['study_uri']}/{trial['model_path']}"
        record = {
            "trial": args.trial, "params": trial["params"], "seed": trial["seed"],
            "instance": trial["instance"], "spot": trial["spot"],
            "duration_s": trial["duration_s"], "cost_thb": trial["cost_thb"],
            "git_commit": trial["git_commit"], "data_version": trial["data_fingerprint"],
            "training_job_id": producer.get("job_id", "unknown"),
            "image_digest": producer.get("image", "unknown"),
            "model_uri": model_uri,
            "metrics": {k: trial[k] for k in ("val_roc_auc", "val_pr_auc",
                                              "test_roc_auc", "test_pr_auc")},
        }
        comparison = ("Registered straight from the study artifact, so its metrics are the "
                      "study's own.")
    else:
        commit = git_head(args.allow_dirty)
        fingerprint, data_key = stage_data(adapter, cfg)
        image = resolve_image(adapter, args.image, args.local_tag)
        job_name = f"itcs355-register-{time.strftime('%Y%m%d-%H%M%S')}"
        output_key = f"registry/{name}/{job_name}"
        job_id = adapter.submit_training(image, {
            "job_name": job_name, "data_key": data_key, "output_key": output_key,
            "args": train_args_for(trial["params"], trial["seed"]),
            "instance": args.instance, "spot": args.spot, "env": job_env(cfg),
        })
        print(f"job      {job_id}")
        result = adapter.wait_training(job_id)
        if not result["succeeded"]:
            print(f"JOB {result['state'].upper()} — {result['error']}\nLogs: {result['logs']}")
            return 1

        output_uri = f"{cfg.blob_uri.rstrip('/')}/{output_key}"
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "metrics.json"
            adapter.download(f"{output_uri}/metrics.json", str(local))
            metrics = json.loads(local.read_text())
        record = {
            "trial": args.trial, "params": trial["params"], "seed": trial["seed"],
            "instance": result["instance"], "spot": result["spot"],
            "duration_s": result["duration_s"],
            "cost_thb": round(result["duration_s"] / 3600 * costs.hourly_rate(
                cfg.provider, result["instance"], result["spot"]), 6),
            "git_commit": commit, "data_version": fingerprint,
            "training_job_id": result["job_id"], "image_digest": image,
            "model_uri": f"{output_uri}/model", "metrics": metrics,
        }
        # A retrain that does not reproduce the trial is a finding, not a formality.
        drift = {k: round(metrics[k] - trial[k], 6)
                 for k in ("val_roc_auc", "test_roc_auc") if k in trial}
        same = all(abs(d) < 1e-9 for d in drift.values())
        comparison = (
            f"Retrained from commit `{commit[:12]}`, the study's `{args.trial}` "
            f"reproduces exactly: val ROC AUC {metrics['val_roc_auc']:.4f}, test "
            f"{metrics['test_roc_auc']:.4f}, identical to the trial's."
            if same else
            f"Retrained from commit `{commit[:12]}`; metrics moved against the study's "
            f"`{args.trial}` by {drift}. Same seed and same data, so a non-zero difference "
            "means the code or the library stack is not what the study ran.")
        print(comparison)

    run_id = log_run(cfg, args.experiment, f"registered-{args.trial}", record)
    print(f"mlflow   run {run_id}")

    lineage = {
        "git_commit": record["git_commit"],
        "data_version": record["data_version"],
        "mlflow_run_id": run_id,
        "training_job_id": record["training_job_id"].rsplit("/", 1)[-1],
        "image_digest": record["image_digest"],
        "seed": str(record["seed"]),
        "metric_val": f"{record['metrics']['val_roc_auc']:.6f}",
        "metric_test": f"{record['metrics']['test_roc_auc']:.6f}",
    }
    version = adapter.register_model(record["model_uri"], name, lineage=lineage)
    print(f"registry {name} version {version}")

    entry = (adapter.promote_model(name, version, args.promote) if args.promote
             else adapter.get_model(name, version))
    entry["promoted_alias"] = args.promote or "none"
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    with mlflow.start_run(run_id=run_id):
        mlflow.set_tags({"registry_name": name, "registry_version": version,
                         "registry_alias": entry["promoted_alias"]})

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps({"entry": entry, "run": record}, indent=2, default=str))
    write_report(REPORT, entry, record, comparison)
    print(f"promoted {args.promote or '—'} -> version {version}; wrote {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
