"""What is deployed right now, what share of traffic it has, and what it costs.

    python scripts/endpoint_status.py
    python scripts/endpoint_status.py --undeploy <deployed_model_id>

The cost line is the point. An endpoint bills per replica-hour for every deployment
attached to it, including ones holding 0% of the traffic, which is how a canary left
behind after a rollback keeps costing money silently.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config, costs  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", default=None)
    p.add_argument("--undeploy", default=None, metavar="DEPLOYED_MODEL_ID",
                   help="detach this deployment and release its replicas")
    args = p.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)
    endpoint = args.endpoint or f"{cfg.model_registry_name}-endpoint"

    if args.undeploy:
        print(f"undeploying {args.undeploy} — releasing its replicas")
        print(f"undeployed  {adapter.undeploy(endpoint, args.undeploy)}")

    deployments = adapter.list_deployments(endpoint)
    if not deployments:
        print(f"{endpoint}: nothing deployed, nothing billing")
        return 0

    total = 0.0
    print(f"{endpoint}\n")
    print(f"{'deployed_model_id':<22} {'version':<8} {'traffic':>8} {'instance':<16} "
          f"{'replicas':<10} {'THB/h':>7}")
    for d in deployments:
        rate = costs.serving_hourly_rate(cfg.provider, d["instance"], d["min_replicas"])
        total += rate
        print(f"{d['deployed_model_id']:<22} {d['model_version_id']:<8} "
              f"{d['traffic_percent']:>7}% {d['instance']:<16} "
              f"{str(d['min_replicas']) + '-' + str(d['max_replicas']):<10} {rate:>7.2f}")
    print(f"\n{total:.2f} THB/h = {total * 24:.0f} THB/day, traffic or no traffic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
