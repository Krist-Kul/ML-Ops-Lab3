"""Lab 2 — prove the registered model can be reloaded by version, from the registry.

    make reload-check VERSION=1
    python scripts/reload_check.py --version 1
    python scripts/reload_check.py --version production     # an alias resolves too

This is the lab's quiet test. Models that cannot be reloaded six months later are the
commonest form of dead work in industry, and the cause is nearly always a serialization
assumption: a custom class that no longer exists, a library version that moved, a
preprocessing step that only ever lived in a notebook.

Loading from a local file instead of the registry defeats the purpose and is checked.
Nothing here knows a bucket name: the version is looked up through the adapter, and the
artifact URI it returns is the only path used. If the registry entry is wrong, this fails
— which is the whole point of running it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow  # noqa: E402

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config, data  # noqa: E402

LINEAGE_FIELDS = ("git_commit", "data_version", "mlflow_run_id", "training_job_id",
                  "image_digest", "seed", "metric_val", "metric_test")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=None, help="registered model name; "
                                                 "defaults to MODEL_REGISTRY_NAME")
    ap.add_argument("--version", required=True, help="version id, or an alias like production")
    ap.add_argument("--rows", type=int, default=5)
    ap.add_argument("--source", choices=("registry", "mlflow"), default="registry",
                    help="registry: the provider's model registry, through the adapter. "
                         "mlflow: a models:/ URI in the tracking server's own registry.")
    args = ap.parse_args()

    cfg = config.load(strict=False)
    name = args.name or cfg.model_registry_name
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)

    if args.source == "mlflow":
        uri = f"models:/{name}/{args.version}"
        print(f"loading {uri}")
    else:
        entry = get_adapter(config.load()).get_model(name, args.version)
        print(f"registry {entry['resource']}")
        print(f"version  {entry['version']}  aliases: {', '.join(entry['aliases']) or '—'}")
        missing = [f for f in LINEAGE_FIELDS if not entry["lineage"].get(f)]
        for field in LINEAGE_FIELDS:
            print(f"  {field:<16} {entry['lineage'].get(field, 'MISSING')}")
        if missing:
            # Not fatal here — the model may still load — but a version you cannot trace
            # is not one you can promote, so say so loudly rather than printing PASS.
            print(f"\nFAIL  lineage incomplete on the version: {missing}")
            return 1
        uri = entry["artifact_uri"]
        print(f"\nloading {uri}")

    model = mlflow.sklearn.load_model(uri)

    df = data.load_raw(cfg.raw_path)
    _, _, test_df = data.split(df, seed=20260101)
    sample = test_df.head(args.rows)
    preds = model.predict_proba(sample[data.FEATURES])[:, 1]

    for rid, p in zip(sample[data.ID], preds):
        print(f"  reading {rid}: p(failure)={p:.4f}")
    print("\nPASS  model reloaded from the registry and scored rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
