"""Lab 2 Task 2 — run the budgeted study as a managed, checkpointed spot job.

    make tune                           # a new study
    make tune INTERRUPT_AT=5            # the same, with trial 5 killed mid-run once
    make tune STUDY=lab2-20260914-1200  # resume a study that did not finish

A study lives under BLOB_URI/studies/<name>/: checkpoint.json (every finished trial's
result), trials/<id>/model, and jobs.json (every job that worked on it, and its cost).
Each attempt is a separate job against that same directory, so a job that dies —
reclaimed spot capacity, or the drill — is resumed by submitting again and loses at most
the trial it was in. Once the study is done, its trials are logged into the local MLflow
experiment, each tagged with the job that produced it.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow  # noqa: E402
from train_remote import job_env, resolve_image, stage_data  # noqa: E402

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config, costs  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="digest-pinned image reference to run")
    src.add_argument("--local-tag", help="local image to push first")
    p.add_argument("--study", default=None, help="resume this study instead of starting one")
    p.add_argument("--trials", type=int, default=12)
    p.add_argument("--budget-thb", type=float, default=150.0)
    p.add_argument("--instance", default="n1-standard-4")
    p.add_argument("--spot", action="store_true")
    p.add_argument("--seed", type=int, default=20260101)
    p.add_argument("--interrupt-at", type=int, default=None)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--experiment", default="itcs355-lab2")
    return p.parse_args()


def fetch_json(adapter, uri: str) -> dict | None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "f.json"
        try:
            adapter.download(uri, str(local))
        except Exception:  # absent is the expected case: a new study, or a job that died early
            return None
        return json.loads(local.read_text())


def put_json(adapter, obj: dict, key: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "f.json"
        local.write_text(json.dumps(obj, indent=2))
        adapter.upload(str(local), key)


def log_to_mlflow(cfg, experiment: str, study: str, study_uri: str, state: dict,
                  producer: dict[str, dict]) -> int:
    """One MLflow run per trial. Idempotent: a trial already logged for this study is skipped."""
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    exp_id = mlflow.set_experiment(experiment).experiment_id
    logged = set(mlflow.search_runs([exp_id], filter_string=f"tags.study = '{study}'")
                 .get("tags.trial", []))
    n = 0
    for r in state["results"]:
        if r["trial"] in logged:
            continue
        job = producer.get(r["trial"], {})
        with mlflow.start_run(run_name=r["trial"]):
            mlflow.log_params({**r["params"], "seed": r["seed"],
                               "instance": r["instance"], "spot": r["spot"]})
            mlflow.log_metrics({k: r[k] for k in (
                "val_roc_auc", "val_pr_auc", "test_roc_auc", "test_pr_auc",
                "duration_s", "cost_thb")})
            mlflow.set_tags({
                "lab": "2", "study": study, "trial": r["trial"],
                "git_commit": r["git_commit"], "data_fingerprint": r["data_fingerprint"],
                "training_job_id": job.get("job_id", "unknown"),
                "image": job.get("image", "unknown"),
                "model_uri": f"{study_uri}/{r['model_path']}",
            })
        n += 1
    return n


def write_summary(path: Path, study: str, study_uri: str, ledger: dict, state: dict,
                  budget: float) -> None:
    job_total = sum(a["cost_thb"] for a in ledger["attempts"])
    trial_total = sum(r["cost_thb"] for r in state.get("results", []))
    lines = [
        f"# Lab 2 — study `{study}`", "",
        f"State: `{study_uri}` · budget {budget:.0f} THB · "
        f"spent {job_total:.4f} THB on jobs, of which {trial_total:.4f} THB was trial compute",
        "", "## Jobs", "",
        "| attempt | job | state | running s | instance | spot | THB | trials finished |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for a in ledger["attempts"]:
        lines.append(f"| {a['attempt']} | `{a['job_id'].rsplit('/', 1)[-1]}` | {a['state']} | "
                     f"{a['duration_s']:.0f} | {a['instance']} | {a['spot']} | "
                     f"{a['cost_thb']:.4f} | {', '.join(a['trials']) or '—'} |")
    if state.get("drill_interrupted"):
        lines += ["", f"Interruption drill: `{state['drill_interrupted']}` was killed mid-trial, "
                  "before its checkpoint was written. Work resumed from the checkpoint — either "
                  "the platform restarted the container or the next attempt picked the study up — "
                  "skipping every finished trial, so the interruption cost one trial, not the run."]
    lines += ["", "## Trials", "",
              "| trial | max_depth | max_features | class_weight | val ROC AUC | test ROC AUC "
              "| s | THB |", "|---|---|---|---|---|---|---|---|"]
    for r in state.get("results", []):
        p = r["params"]
        lines.append(f"| {r['trial']} | {p['max_depth']} | {p['max_features']} | "
                     f"{p['class_weight']} | {r['val_roc_auc']:.4f} | {r['test_roc_auc']:.4f} | "
                     f"{r['duration_s']:.1f} | {r['cost_thb']:.6f} |")
    if state.get("skipped"):
        lines += ["", f"Not run for budget: {state['skipped']}"]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    cfg = config.load()
    if cfg.provider == "local":
        print("CLOUD_PROVIDER=local has no managed compute. Use `make tune-local`.")
        return 2
    adapter = get_adapter(cfg)
    rate = costs.hourly_rate(cfg.provider, args.instance, spot=args.spot)

    _, data_key = stage_data(adapter, cfg)
    image = resolve_image(adapter, args.image, args.local_tag)

    study = args.study or f"lab2-{time.strftime('%Y%m%d-%H%M%S')}"
    study_key = f"studies/{study}"
    study_uri = f"{cfg.blob_uri.rstrip('/')}/{study_key}"
    print(f"study    {study_uri}")

    ledger = fetch_json(adapter, f"{study_uri}/jobs.json") or {"study": study, "attempts": []}
    state = fetch_json(adapter, f"{study_uri}/checkpoint.json") or {"results": []}

    for _ in range(args.max_attempts):
        if state.get("done"):
            break
        spent = sum(a["cost_thb"] for a in ledger["attempts"])
        if spent >= args.budget_thb:
            print(f"budget spent ({spent:.2f} THB); not submitting another attempt")
            break

        attempt = len(ledger["attempts"]) + 1
        tune_args = ["--trials", str(args.trials), "--budget-thb", str(args.budget_thb),
                     "--instance", args.instance, "--seed", str(args.seed)]
        tune_args += ["--spot"] if args.spot else []
        if args.interrupt_at is not None:
            tune_args += ["--interrupt-at", str(args.interrupt_at)]  # fires once per study

        before = {r["trial"] for r in state["results"]}
        job_id = adapter.submit_training(image, {
            "job_name": f"itcs355-tune-{study}-a{attempt}", "command": ["python", "-m", "src.tune"],
            "args": tune_args, "data_key": data_key, "output_key": study_key,
            "instance": args.instance, "spot": args.spot, "env": job_env(cfg),
        })
        print(f"\nattempt {attempt}: {job_id}")
        result = adapter.wait_training(job_id)

        state = fetch_json(adapter, f"{study_uri}/checkpoint.json") or {"results": []}
        new = [r["trial"] for r in state["results"] if r["trial"] not in before]
        ledger["attempts"].append({
            "attempt": attempt, **result, "image": image, "trials": new,
            "cost_thb": round(result["duration_s"] / 3600 * rate, 6),
        })
        put_json(adapter, ledger, f"{study_key}/jobs.json")
        print(f"attempt {attempt}: {result['state']}, {len(new)} trials finished"
              + (f" — {result['error']}" if result["error"] else ""))
        if not result["succeeded"] and not new:
            print(f"no progress, so not retrying blindly. Logs: {result['logs']}")
            break

    summary = Path("reports/lab2-study.md")
    write_summary(summary, study, study_uri, ledger, state, args.budget_thb)
    Path("reports/lab2-study.json").write_text(json.dumps(
        {"study": study, "study_uri": study_uri, "ledger": ledger, "state": state}, indent=2))
    print(f"\nwrote {summary}")

    if not state.get("done"):
        print(f"study not finished. Resume with: make tune STUDY={study}")
        return 1
    producer = {t: a for a in ledger["attempts"] for t in a["trials"]}
    n = log_to_mlflow(cfg, args.experiment, study, study_uri, state, producer)
    print(f"logged {n} trials to MLflow experiment {args.experiment!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
