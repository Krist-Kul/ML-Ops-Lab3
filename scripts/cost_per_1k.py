"""Lab 3 Task 5 — cost per 1,000 predictions, and the batch break-even.

    make cost-per-1k

Reads the measurements Tasks 3 and 4 already wrote — `reports/lab3-load.json` for
throughput and `reports/lab3-canary.json` for what was actually deployed and for how
long — and writes `reports/lab3-cost.json`. Nothing here calls a cloud API or creates a
resource; it is arithmetic over committed evidence, so it can be re-run at any time and
will produce the same numbers.

Three choices in here are the ones worth arguing with, so they are stated rather than
buried:

**Throughput is the container-side saturated rate on the deployed machine (10.4 req/s),
not the local rate (51.3 req/s).** The local container is an emulated amd64 image on an
arm64 laptop and it bills nothing. The machine that bills is `n1-standard-2` in
`asia-southeast1`, and its own `latency_ms` logs are what Task 3 read back. Using the
laptop number would divide the hourly rate by a throughput that no invoice has ever seen.

**Utilisation is reported at four values, including the one this lab actually ran at.**
The handout asks for the assumption to be explicit because it is the fragile term. The
honest version is not a round 25%: it is the 5,290 predictions the endpoint really served
divided by the capacity it was provisioned for over its whole life.

**"Prediction" means one scored row, not one HTTP request.** The two differ by up to 100x
on this service, because `/predict/batch` takes 100 rows and `predict_proba` costs the
same for 100 rows as for 1 (25.053 ms against 25.029 ms, measured in Task 3). A cost per
1,000 predictions that silently means per 1,000 single-row requests is off by two orders
of magnitude on the same hardware, so both routes are priced.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import costs

PROVIDER = "gcp"
INSTANCE = "n1-standard-2"

# Endpoint lifetime, from the two timestamps the deploy and rollback records already carry.
# The base deployment became ready at `deployed_at` in reports/lab3-deploy.json and was
# released by the undeploy in the Task 4 rollback trace. An endpoint with no deployment
# attached bills nothing, so the deployment's life — not the endpoint's — is the billed span.
BASE_DEPLOYED_AT = "2026-09-30T15:09:40Z"
BASE_UNDEPLOYED_AT = "2026-10-01T14:32:02Z"

# Predictions the endpoint really answered, by task. Counted from the artefacts, not
# estimated: the Task 4 numbers are `wc -l` on the attested streams (one line per request;
# the blind stream is the same requests written a second time, so it is not added).
PREDICTIONS_SERVED = {
    "task2_smoke_scored": 3,          # reports/lab3-smoke.json, 3 scored + 1 refused at validation
    "task3_loadtest": 2033,           # lab3-load.json requests_issued.endpoint
    "task4_reference": 2000,          # lab3-canary-attested-reference.jsonl
    "task4_watch": 954,               # lab3-canary-attested-watch.jsonl
    "task4_after_rollback": 300,      # lab3-canary-attested-after_rollback.jsonl
}


def _hours(start: str, end: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() / 3600.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", type=Path, default=Path("reports/lab3-load.json"))
    ap.add_argument("--canary", type=Path, default=Path("reports/lab3-canary.json"))
    ap.add_argument("--out", type=Path, default=Path("reports/lab3-cost.json"))
    args = ap.parse_args()

    load = json.loads(args.load.read_text())
    canary = json.loads(args.canary.read_text())

    rate = costs.serving_hourly_rate(PROVIDER, INSTANCE)

    # --- throughput, three candidates, one choice ---------------------------------
    container = load["experiment_3_instance_size"]["container_side"][INSTANCE]
    rps_single = max(level["rps"] for level in container.values())   # saturated, 10 VU
    p50_single_ms = container["1"]["p50"]                            # one caller, no queueing
    local_rps = max(
        level["rps"] for level in load["primary_sweep_local_2vcpu_60s"].values()
    )
    client_rps = max(
        v["rps"] for k, v in load["experiment_3_instance_size"]["client_side"].items()
        if k.startswith(INSTANCE)
    )

    # The batch route on this hardware is a projection, not a measurement: Task 3 measured
    # batch only on the local container. It is projected from the one fact that makes batch
    # cheap, which Task 3 *did* measure on both — predict_proba is flat in rows — so a
    # 100-row call costs what a 1-row call costs, and 100 rows land per p50.
    batch_rows_per_call = 100
    rps_batch_rows = batch_rows_per_call / (p50_single_ms / 1000.0)
    batch_speedup_local = load["experiment_1_conclusion"]["row_throughput_speedup_x"]["1_vu"]

    # --- utilisation --------------------------------------------------------------
    base_hours = _hours(BASE_DEPLOYED_AT, BASE_UNDEPLOYED_AT)
    served = sum(PREDICTIONS_SERVED.values())
    capacity_single = rps_single * base_hours * 3600.0
    utilisation_realised = served / capacity_single

    utilisations = {
        "realised_this_lab": utilisation_realised,
        "5pct_idle_service": 0.05,
        "25pct_busy_business_hours": 0.25,
        "80pct_well_packed": 0.80,
        "100pct_saturated_ceiling": 1.00,
    }
    per_1k_single = {
        name: costs.cost_per_1k_predictions(rate, rps_single, u)
        for name, u in utilisations.items()
    }
    per_1k_batch = {
        name: costs.cost_per_1k_predictions(rate, rps_batch_rows, u)
        for name, u in utilisations.items()
    }

    # --- what was actually billed -------------------------------------------------
    incident = canary["cost_incident"]
    canary_hours = incident["hours_with_two_deployments"]
    n1s4 = load["cost_of_this_task_thb"]
    billed = {
        "base_n1_standard_2_hours": base_hours,
        "base_n1_standard_2_thb": rate * base_hours,
        "canary_second_replica_hours": canary_hours,
        "canary_second_replica_thb": rate * canary_hours,
        "task3_n1_standard_4_hours": n1s4["n1_standard_4_extra_replica_minutes"] / 60.0,
        "task3_n1_standard_4_thb": n1s4["n1_standard_4_thb"],
    }
    billed["total_thb"] = (
        billed["base_n1_standard_2_thb"]
        + billed["canary_second_replica_thb"]
        + billed["task3_n1_standard_4_thb"]
    )
    # Two realised figures, because they answer different questions. The first is what one
    # provisioned replica cost per 1,000 predictions; the second is what the invoice divided
    # by the predictions, incident included. Reporting only the first hides the incident;
    # reporting only the second blames the unit cost for a bug.
    realised_per_1k_one_replica = billed["base_n1_standard_2_thb"] / served * 1000.0
    realised_per_1k_all_billed = billed["total_thb"] / served * 1000.0

    # --- batch break-even ---------------------------------------------------------
    # A warm endpoint costs the same whether or not it is called. A batch job on the same
    # machine type bills the same per-replica rate for the length of the run only. So the
    # break-even is a volume, and it falls exactly where the endpoint would be saturated
    # around the clock: below that, batch does the same work in less billed time.
    endpoint_daily = rate * 24.0
    # Billed startup for one run. Not measured for a Vertex batch job in this lab; bounded
    # with the slowest start-up number we do have, the 42.4 s cold start on this machine,
    # rounded up to 5 minutes to cover job orchestration. Stated so the sensitivity is
    # visible rather than assumed away.
    batch_startup_s = 300.0
    batch_startup_thb = rate * batch_startup_s / 3600.0
    budget_after_startup = endpoint_daily - batch_startup_thb

    def breakeven_rows_per_day(rows_per_s: float) -> float:
        return budget_after_startup / rate * 3600.0 * rows_per_s

    breakeven = {
        "endpoint_warm_thb_per_day": endpoint_daily,
        "batch_startup_s_assumed": batch_startup_s,
        "batch_startup_thb": batch_startup_thb,
        "single_row_route": {
            "rows_per_s": rps_single,
            "predictions_per_day": breakeven_rows_per_day(rps_single),
            "equivalent_sustained_rps": breakeven_rows_per_day(rps_single) / 86400.0,
        },
        "batch_100_row_route": {
            "rows_per_s": rps_batch_rows,
            "predictions_per_day": breakeven_rows_per_day(rps_batch_rows),
            "equivalent_sustained_rps": breakeven_rows_per_day(rps_batch_rows) / 86400.0,
        },
        "this_lab_whole_volume_as_one_batch_thb": batch_startup_thb
        + rate * (served / rps_single) / 3600.0,
        "scaffold_fn_for_comparison": {
            "note": "src.costs.batch_breakeven_rps models a batch job as a FIXED daily cost, "
                    "so it answers a different question: given a job that costs this much "
                    "whatever the volume, what request rate makes the endpoint worth it. The "
                    "volume-dependent form above is the one Task 5 asks for.",
            "batch_job_thb_assumed": batch_startup_thb,
            "rps": costs.batch_breakeven_rps(rate, batch_job_thb=batch_startup_thb),
        },
    }

    out = {
        "lab": 3,
        "task": 5,
        "computed_utc": "2026-10-03",
        "inputs": {
            "provider": PROVIDER,
            "instance": INSTANCE,
            "thb_per_replica_hour": rate,
            "price_source": "src/costs.SERVING_PRICE_TABLE, verified 2026-09-30 against the "
                            "Cloud Billing Catalog API for asia-southeast1 at 33.355 THB/USD",
            "cloud_calls_made": 0,
            "throughput_candidates_rps": {
                "local_container_emulated": local_rps,
                "endpoint_client_side_from_bangkok": client_rps,
                "endpoint_container_side": rps_single,
            },
            "throughput_chosen_rps": rps_single,
            "throughput_chosen_why": "the only one of the three measured on the machine that "
                                     "bills, read from the container's own latency_ms logs",
            "endpoint_p50_one_caller_ms": p50_single_ms,
            "batch_rows_per_call": batch_rows_per_call,
            "batch_rows_per_s_projected": rps_batch_rows,
            "batch_projection_basis": "predict_proba is flat in rows (25.029 ms for 1, 25.053 "
                                      "for 100, Task 3), measured locally at a 97x row-"
                                      f"throughput gain ({batch_speedup_local}x); the endpoint "
                                      "batch route was never load-tested, so this is a "
                                      "projection and is labelled as one",
        },
        "utilisation": {
            "assumptions": utilisations,
            "realised_derivation": {
                "predictions_served": PREDICTIONS_SERVED,
                "predictions_served_total": served,
                "billed_replica_hours_base": base_hours,
                "capacity_predictions_at_saturation": capacity_single,
                "utilisation": utilisation_realised,
            },
        },
        "cost_per_1000_predictions_thb": {
            "single_row_route": per_1k_single,
            "batch_100_row_route": per_1k_batch,
            "realised_one_replica": realised_per_1k_one_replica,
            "realised_all_billed_incident_included": realised_per_1k_all_billed,
        },
        "what_was_billed": billed,
        "batch_breakeven": breakeven,
        "endpoint_state": {
            "running_now": False,
            "teardown_verified": canary["final_state"]["teardown_verified"],
            "thb_per_hour": canary["final_state"]["thb_per_hour"],
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {args.out}")
    print(f"rate                       {rate:.2f} THB/replica-hour ({INSTANCE})")
    print(f"throughput chosen          {rps_single:.1f} req/s (endpoint, container side)")
    print(f"utilisation realised       {utilisation_realised * 100:.2f}%")
    print(f"per 1,000 at saturation    {per_1k_single['100pct_saturated_ceiling']:.4f} THB")
    print(f"per 1,000 at 25%           {per_1k_single['25pct_busy_business_hours']:.4f} THB")
    print(f"per 1,000 as realised      {realised_per_1k_one_replica:.2f} THB (one replica)")
    print(f"per 1,000 as billed        {realised_per_1k_all_billed:.2f} THB (incident included)")
    print(
        "batch cheaper below        "
        f"{breakeven['single_row_route']['predictions_per_day']:,.0f} predictions/day "
        "single-row, "
        f"{breakeven['batch_100_row_route']['predictions_per_day']:,.0f} batched"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
