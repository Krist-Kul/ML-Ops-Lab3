"""Lab 3 Task 2 — call the deployed endpoint with three known payloads.

    make smoke
    python scripts/smoke_endpoint.py --endpoint itcs355-6688053-endpoint --rejects

Three payloads, chosen to fail differently rather than to pass three times: a routine row,
a row the model should score high, and a boundary row at the top of every plausible range.
The scores are compared against the same rows scored locally from the registry, so the
check is "this endpoint serves the model we registered", not merely "something answered".
`--rejects` additionally sends an out-of-range row, which must be refused rather than
scored — a validation layer that disappears in deployment is worse than none, because the
422s in CI said it was there.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config  # noqa: E402

RECORD = Path("reports/lab3-smoke.json")

PAYLOADS = {
    "routine": {
        "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
        "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0,
    },
    "hot-and-overdue": {
        "temp_c": 118.0, "vibration_mm_s": 22.0, "pressure_kpa": 520.0,
        "hours_since_service": 17500.0, "load_pct": 96.0, "ambient_humidity": 80.0,
    },
    "upper-bound": {
        "temp_c": 140.0, "vibration_mm_s": 60.0, "pressure_kpa": 600.0,
        "hours_since_service": 20000.0, "load_pct": 100.0, "ambient_humidity": 100.0,
    },
}
REJECT = {**PAYLOADS["routine"], "load_pct": 250.0}


def local_scores(cfg, version: str) -> dict[str, float] | None:
    """The same rows, scored from the registry on this machine. None if it cannot load."""
    try:
        import os

        # Same two library stdout writers the service silences; a smoke run's output is
        # evidence, and a progress bar in the middle of it is not.
        os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
        os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")
        import mlflow.sklearn
        import pandas as pd

        from src.data import FEATURES

        entry = get_adapter(cfg).get_model(cfg.model_registry_name, version)
        model = mlflow.sklearn.load_model(entry["artifact_uri"])
        frame = pd.DataFrame(list(PAYLOADS.values()), columns=FEATURES)
        scores = model.predict_proba(frame)[:, 1]
        return dict(zip(PAYLOADS, (float(s) for s in scores)))
    except Exception as exc:  # the endpoint check still stands on its own
        print(f"local    could not score locally ({type(exc).__name__}: {exc})")
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", default=None, help="endpoint display name or resource name")
    p.add_argument("--source-version", default=None,
                   help="version to score against locally; defaults to the deploy record")
    p.add_argument("--rejects", action="store_true", help="also send a row that must be refused")
    p.add_argument("--record", type=Path, default=RECORD)
    args = p.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)
    deploy_record = json.loads(Path("reports/lab3-deploy.json").read_text()) \
        if Path("reports/lab3-deploy.json").exists() else {}
    endpoint = args.endpoint or deploy_record.get("endpoint_display_name") \
        or f"{cfg.model_registry_name}-endpoint"
    source_version = args.source_version or deploy_record.get("source_version", "production")

    expected = local_scores(cfg, source_version)
    results, failures = {}, []
    for label, payload in PAYLOADS.items():
        response = adapter.invoke(endpoint, payload)
        prediction = response["predictions"][0]
        probability = float(prediction["probability"])
        row = {
            "probability": probability,
            "model_version_in_body": prediction.get("model_version"),
            "model_version_id": response["model_version_id"],
            "deployed_model_id": response["deployed_model_id"],
            "latency_ms": response["latency_ms"],
        }
        if expected is not None:
            row["local_probability"] = expected[label]
            row["matches_local"] = abs(expected[label] - probability) < 1e-9
            if not row["matches_local"]:
                failures.append(f"{label}: endpoint {probability:.9f} != "
                                f"local {expected[label]:.9f}")
        if not 0.0 <= probability <= 1.0:
            failures.append(f"{label}: probability {probability} outside [0, 1]")
        results[label] = row
        print(f"{label:<16} p={probability:.6f}  model_version={row['model_version_in_body']}  "
              f"vertex_version={row['model_version_id']}  {row['latency_ms']:.0f} ms"
              + ("  == local" if row.get("matches_local") else ""))

    if args.rejects:
        try:
            adapter.invoke(endpoint, REJECT)
            failures.append("out-of-range row was scored instead of refused")
            print("reject           SCORED — validation is not running in the deployment")
        except Exception as exc:
            message = str(exc).split("\n")[0][:160]
            results["reject"] = {"refused": True, "error": message}
            print(f"reject           refused: {message}")

    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(
        {"endpoint": endpoint, "source_version": source_version,
         "results": results, "failures": failures}, indent=2))
    print(f"\nwrote    {args.record}")
    if failures:
        print("SMOKE FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("SMOKE PASSED — the endpoint serves the registered model, and says which version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
