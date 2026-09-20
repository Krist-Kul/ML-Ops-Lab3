"""Lab 2 Task 1 — run the Lab 1 training image as a managed job.

    make train-remote                                   # build, push, submit, wait
    make train-remote IMAGE_URI=<repo>@sha256:...       # reuse an image already pushed
    python scripts/train_remote.py --image <ref> -- --max-depth 16

The job depends on nothing on this laptop: the data is uploaded to BLOB_URI first, under
a key derived from its content hash, and the job reads it from there and writes its
artifacts back there. Everything provider-specific happens behind the adapter.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config, costs, data  # noqa: E402

def job_env(cfg) -> dict[str, str]:
    # The image carries no cloud.env, so the provider (which prices the job) is passed in.
    # /app belongs to root and the job runs as `runner`, so MLflow's default
    # sqlite:///mlflow.db would land in /app and fail; /app/mlruns is writable.
    return {"CLOUD_PROVIDER": cfg.provider,
            "MLFLOW_TRACKING_URI": "sqlite:////app/mlruns/mlflow.db"}


def stage_data(adapter, cfg) -> tuple[str, str]:
    """Upload the raw data under a key derived from its content. Returns (fingerprint, key)."""
    fingerprint = data.data_fingerprint(cfg.raw_path)
    data_key = f"data/{fingerprint}/sensors.csv"
    print(f"data     {adapter.upload(str(cfg.raw_path), data_key)}")
    return fingerprint, data_key


def resolve_image(adapter, image: str | None, local_tag: str | None) -> str:
    image = image or adapter.push_image(local_tag)
    if "@sha256:" not in image:
        raise SystemExit(f"refusing to run {image!r}: not digest-pinned, so not traceable")
    print(f"image    {image}")
    return image


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="digest-pinned image reference to run")
    src.add_argument("--local-tag", help="local image to push first, e.g. itcs355-lab1:abc123")
    p.add_argument("--instance", default="n1-standard-4", help="key into src/costs.py")
    p.add_argument("--spot", action="store_true", help="discounted, preemptible compute")
    p.add_argument("--record", type=Path, default=Path("reports/lab2-train-remote.json"))
    p.add_argument("train_args", nargs="*", help="passed to src.train, after a `--`")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = config.load()
    if cfg.provider == "local":
        print("CLOUD_PROVIDER=local has no managed compute. Set your provider in cloud.env.")
        return 2
    adapter = get_adapter(cfg)

    fingerprint, data_key = stage_data(adapter, cfg)
    image = resolve_image(adapter, args.image, args.local_tag)

    job_name = f"itcs355-train-{time.strftime('%Y%m%d-%H%M%S')}"
    output_key = f"jobs/{job_name}"
    job_id = adapter.submit_training(image, {
        "job_name": job_name, "data_key": data_key, "output_key": output_key,
        "args": args.train_args, "instance": args.instance, "spot": args.spot, "env": job_env(cfg),
    })
    print(f"job      {job_id}")

    result = adapter.wait_training(job_id)
    output_uri = f"{cfg.blob_uri.rstrip('/')}/{output_key}"
    record = {
        **result, "image": image, "data_fingerprint": fingerprint,
        "output_uri": output_uri, "train_args": args.train_args,
        "cost_thb": round(result["duration_s"] / 3600
                          * costs.hourly_rate(cfg.provider, result["instance"], result["spot"]), 4),
    }
    if result["succeeded"]:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "metrics.json"
            adapter.download(f"{output_uri}/metrics.json", str(local))
            record["metrics"] = json.loads(local.read_text())

    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    if not result["succeeded"]:
        print(f"\nJOB {result['state'].upper()}. Logs: {result['logs']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
