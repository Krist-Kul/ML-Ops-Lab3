"""Lab 3 Task 4 — canary a worse version, detect it blind, roll it back with evidence.

    make canary-reference     # what normal looks like, measured BEFORE the canary exists
    make canary-deploy        # attach the canary at 10% traffic
    make canary-watch         # drive traffic, detect from metrics alone, time it
    make canary-rollback      # traffic back to production, THEN release the canary's machines
    make canary-status        # who is attached, what share, what it costs

Three things this script is careful about, because each is a way the exercise can be done
and prove nothing.

**The detector does not see the version.** Every response carries Vertex's attestation of
which deployment answered, and that is the evidence traffic moved — so it is written, but to
a separate file. `monitoring/canary.py` reads only the blind stream and raises if a version
field appears in it. The split is structural rather than a promise.

**The input distribution is held fixed.** The same rows, in the same order, from the same
seed, are sent in the reference phase and the watch phase. If the payloads varied, a change
in the score distribution could be the inputs rather than the model, and the whole detection
claim would be unfalsifiable.

**Traffic is moved before machines are released.** A rollback that undeploys the canary while
it still holds traffic leaves the endpoint with a split pointing at nothing; a rollback that
only zeroes its traffic leaves the replicas running and billing. It is two calls, in order.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter  # noqa: E402
from monitoring import canary as detector  # noqa: E402
from src import config, costs, data, seeds  # noqa: E402

RECORD = Path("reports/lab3-canary.json")
BLIND = Path("reports/lab3-canary-blind.jsonl")
ATTESTED = Path("reports/lab3-canary-attested.jsonl")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def payload_rows(n: int, seed: int) -> list[dict]:
    """A fixed, reproducible sequence of real feature rows.

    Drawn from the dataset's own test split rather than sampled from the plausible ranges:
    the score distribution the detector calls "normal" has to be the distribution the model
    actually meets, and uniform noise over the feature ranges is not that.
    """
    cfg = config.load(strict=False)
    frame = data.load_raw(cfg.raw_path)
    _, _, test = data.split(frame, seed=seeds.DEFAULT_SEED)
    rows = test[data.FEATURES].to_dict("records")
    import random
    rng = random.Random(seed)
    order = list(range(len(rows)))
    rng.shuffle(order)
    # Cycled, not resampled, so a longer run is a prefix-compatible extension of a shorter
    # one and two phases of different lengths still see the same rows in the same order.
    return [{k: float(v) for k, v in rows[order[i % len(order)]].items()} for i in range(n)]


class Driver:
    """Sends requests through the adapter and splits each response into two streams."""

    def __init__(self, adapter, endpoint: str, blind: Path, attested: Path) -> None:
        self.adapter = adapter
        self.endpoint = endpoint
        self.blind = blind.open("a")
        self.attested = attested.open("a")
        self.lock = threading.Lock()
        self.scores: list[float] = []
        self.latencies: list[float] = []
        self.stamps: list[str] = []
        self.attestations: list[dict] = []
        self.errors = 0

    def close(self) -> None:
        self.blind.close()
        self.attested.close()

    def one(self, row: dict, phase: str) -> None:
        stamp = now()
        try:
            response = self.adapter.invoke(self.endpoint, row)
        except Exception as exc:  # a failed call is data, not a reason to stop the run
            with self.lock:
                self.errors += 1
                self.attested.write(json.dumps(
                    {"ts": stamp, "phase": phase, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
            return
        prediction = (response.get("predictions") or [{}])[0]
        probability = float(prediction.get("probability"))
        latency = float(response.get("latency_ms", 0.0))

        # The blind stream: what the detector is allowed to know.
        blind_row = {"ts": stamp, "probability": probability, "latency_ms": latency}
        # The attested stream: what the PLATFORM says about who answered. Not the container's
        # self-report — a container can claim any version it likes; this comes from Vertex.
        attested_row = {"ts": stamp, "phase": phase,
                        "deployed_model_id": response.get("deployed_model_id", ""),
                        "model_version_id": response.get("model_version_id", ""),
                        "container_reported_version": prediction.get("model_version", "")}
        with self.lock:
            self.blind.write(json.dumps(blind_row) + "\n")
            self.attested.write(json.dumps(attested_row) + "\n")
            self.scores.append(probability)
            self.latencies.append(latency)
            self.stamps.append(stamp)
            self.attestations.append(attested_row)

    def run(self, rows: list[dict], phase: str, concurrency: int) -> None:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(lambda r: self.one(r, phase), rows))


def load_record() -> dict:
    return json.loads(RECORD.read_text()) if RECORD.exists() else {}


def save_record(record: dict) -> None:
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2, default=str) + "\n")


def deployment_table(adapter, endpoint: str) -> list[dict]:
    rows = adapter.list_deployments(endpoint)
    provider = config.load(strict=False).provider
    for row in rows:
        row["thb_per_hour"] = round(costs.serving_hourly_rate(
            provider, row["instance"], row["min_replicas"]), 2)
    return rows


def print_deployments(rows: list[dict], heading: str) -> None:
    print(f"\n{heading}  ({now()})")
    print(f"  {'deployed_model_id':<22}{'version':<9}{'traffic':>8}  {'instance':<16}{'THB/h':>7}")
    for row in rows:
        print(f"  {row['deployed_model_id']:<22}{row['model_version_id']:<9}"
              f"{row['traffic_percent']:>7}%  {row['instance']:<16}{row['thb_per_hour']:>7.2f}")
    print(f"  total {sum(r['thb_per_hour'] for r in rows):.2f} THB/h")


def attested_shares(attestations: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in attestations:
        key = row.get("deployed_model_id") or "error"
        counts[key] = counts.get(key, 0) + 1
    return counts


# --- phases -------------------------------------------------------------------------------

def phase_reference(adapter, endpoint: str, args) -> dict:
    """Measure normal. Must run while production is the only thing serving."""
    live = deployment_table(adapter, endpoint)
    if len(live) != 1:
        raise SystemExit(
            f"{len(live)} deployments attached; the reference must be measured with only "
            "production serving, or it is a reference to a mixture.")
    print_deployments(live, "before the reference window")

    BLIND.unlink(missing_ok=True)
    ATTESTED.unlink(missing_ok=True)
    rows = payload_rows(args.reference_n, args.seed)
    driver = Driver(adapter, endpoint, BLIND, ATTESTED)
    started = time.time()
    print(f"\nreference  {args.reference_n} requests at concurrency {args.concurrency}")
    driver.run(rows, "reference", args.concurrency)
    elapsed = time.time() - started
    driver.close()

    reference = detector.Reference.from_samples(driver.scores, driver.latencies)
    print(f"  {len(driver.scores)} scored in {elapsed:.1f}s "
          f"({len(driver.scores) / elapsed:.1f} req/s), {driver.errors} errors")
    print(f"  mean={reference.mean_score:.4f}  alert_rate={reference.alert_rate:.4f}  "
          f"p99={reference.tail_cut:.4f}  latency_p95={reference.latency_p95_ms:.1f}ms")

    record = load_record()
    record["endpoint"] = endpoint
    record["reference"] = {
        "captured_from": now(), "requests": len(driver.scores), "errors": driver.errors,
        "seconds": round(elapsed, 1), "rps": round(len(driver.scores) / elapsed, 2),
        "attested_shares": attested_shares(driver.attestations),
        "blind_file": str(BLIND), "attested_file": str(ATTESTED),
        **reference.to_json(),
    }
    record["reference_scores"] = reference.scores
    record["production_deployment"] = live[0]
    save_record(record)
    return record


def phase_deploy(adapter, endpoint: str, args) -> dict:
    """Attach the canary, or adopt one `scripts/deploy.py` has already attached.

    Adoption exists because registering a SERVING version — the model bytes plus the serving
    container and the provider's route declarations — is `scripts/deploy.py`'s job and was
    proved in Task 2. Duplicating it here to save one command would mean two code paths that
    have to agree about what a deployable version is. So the canary is created the same way
    production was, and this phase records what is attached.
    """
    record = load_record()
    if "reference" not in record:
        raise SystemExit("run the reference phase first — a detector with no notion of "
                         "normal cannot detect anything.")
    production_id = record.get("production_deployment", {}).get("deployed_model_id")

    if args.adopt:
        live = deployment_table(adapter, endpoint)
        others = [r for r in live if r["deployed_model_id"] != production_id]
        if len(others) != 1:
            raise SystemExit(
                f"expected exactly one deployment that is not production ({production_id}); "
                f"found {[r['deployed_model_id'] for r in others]}.")
        canary = others[0]
        canary_id = canary["deployed_model_id"]
        # The platform's own creation timestamp, not this process's clock. It is the earliest
        # defensible "the canary exists" moment and it is auditable after the fact.
        live_at = canary["created"] or now()
        elapsed = None
        print(f"adopted canary {canary_id} (version {canary['model_version_id']}, "
              f"{canary['traffic_percent']}% traffic, created {live_at})")
    else:
        print(f"deploying canary: serving version {args.canary_version} at {args.traffic}% "
              f"on {args.instance} — the meter starts when the replica does")
        started = time.time()
        adapter.deploy(f"{args.name}@{args.canary_version}", endpoint, args.instance,
                       min_replicas=1, max_replicas=1, traffic=args.traffic, lab=3)
        canary_id = adapter._last_deployed_model_id
        live_at = now()
        elapsed = round(time.time() - started, 1)
        live = deployment_table(adapter, endpoint)
        canary = next(r for r in live if r["deployed_model_id"] == canary_id)

    live = deployment_table(adapter, endpoint)
    print_deployments(live, "canary attached")
    record["canary"] = {
        "deployed_model_id": canary_id,
        "serving_version": canary["model_version_id"],
        "traffic_percent": canary["traffic_percent"],
        "instance": canary["instance"],
        "live_at": live_at, "deploy_seconds": elapsed, "adopted": bool(args.adopt),
        "split_after_deploy": {r["deployed_model_id"]: r["traffic_percent"] for r in live},
        "thb_per_hour_total": round(sum(r["thb_per_hour"] for r in live), 2),
    }
    save_record(record)
    return record


def phase_watch(adapter, endpoint: str, args) -> dict:
    record = load_record()
    if "canary" not in record:
        raise SystemExit("no canary recorded; run the deploy phase first.")
    reference = detector.Reference.from_samples(
        record["reference_scores"],
        [record["reference"]["latency_p95_ms"]] if record["reference"]["latency_p95_ms"] else [])
    # from_samples recomputes latency_p95 from the list it is given, so restore the measured
    # value rather than the one derived from a single-element list.
    reference.latency_p95_ms = record["reference"]["latency_p95_ms"]

    engine = detector.Detector(reference, confirmations=args.confirmations)
    rows = payload_rows(args.watch_n, args.seed)
    driver = Driver(adapter, endpoint, BLIND, ATTESTED)
    windows: list[dict] = []
    live_at = record["canary"]["live_at"]
    print(f"\nwatching  canary live at {live_at}, window {args.window} requests, "
          f"{args.confirmations} confirmations to declare")
    started = time.time()

    sent = 0
    while sent < args.watch_n and not engine.incident.declared:
        chunk = rows[sent:sent + args.window]
        if not chunk:
            break
        driver.run(chunk, "watch", args.concurrency)
        sent += len(chunk)
        window_scores = driver.scores[-args.window:]
        window_latencies = driver.latencies[-args.window:]
        at = driver.stamps[-1] if driver.stamps else now()
        signals, incident = engine.observe(at, window_scores, window_latencies)
        shares = attested_shares(driver.attestations[-args.window:])
        fired = [s.name for s in signals if s.fired]
        windows.append({
            "at": at, "through_request": sent, "elapsed_s": round(time.time() - started, 1),
            "fired": fired, "attested_shares": shares,
            "signals": [asdict(s) for s in signals],
        })
        print(f"  n<={sent:5d}  {at}  {round(time.time() - started, 1):>6}s  "
              f"fired: {','.join(fired) or '-'}")
        for signal in signals:
            marker = "**" if signal.fired else "  "
            p = f"p={signal.p_value:.2e}" if signal.p_value is not None else ""
            print(f"      {marker} {signal.name:<12} {signal.value:>10.5f} vs "
                  f"{signal.baseline:<10.5f} ratio {signal.ratio:>7.2f}  {p}")

    driver.close()
    incident = engine.incident

    canary_id = record["canary"]["deployed_model_id"]
    # The moment the bad model actually started answering, which is not the moment it was
    # attached: a replica spends its first ~40 s loading the model and Vertex routes nothing
    # to it until /ready passes. Timing detection from the deploy would credit the detector
    # with finding something that was not yet being served, and timing it only from here
    # would hide the exposure window. Both are reported.
    first_canary_at = next((row["ts"] for row in driver.attestations
                            if row.get("deployed_model_id") == canary_id), None)

    def since(stamp: str | None, origin: str | None = None) -> float | None:
        if not stamp or not origin:
            return None
        t0 = datetime.fromisoformat(origin.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return round((t1 - t0).total_seconds(), 1)

    requests_at = {}
    for window in windows:
        if incident.first_fire_at and window["at"] == incident.first_fire_at:
            requests_at["first_fire"] = window["through_request"]
        if incident.declared_at and window["at"] == incident.declared_at:
            requests_at["declared"] = window["through_request"]

    print(f"\nfirst canary response  {first_canary_at}")
    print(f"first fire  {incident.first_fire_at}  by {incident.first_fire_by or '-'}  "
          f"({since(incident.first_fire_at, first_canary_at)}s after the canary first "
          f"answered, {requests_at.get('first_fire')} requests into the watch)")
    print(f"declared    {incident.declared_at}  by {incident.declared_by or '-'}  "
          f"({since(incident.declared_at, first_canary_at)}s, "
          f"{requests_at.get('declared')} requests)")

    record["watch"] = {
        "windows": windows, "incident": asdict(incident),
        "requests_sent": sent, "errors": driver.errors,
        "seconds": round(time.time() - started, 1),
        "first_canary_response_at": first_canary_at,
        "detection_seconds_from_first_canary_response": {
            "first_fire": since(incident.first_fire_at, first_canary_at),
            "declared": since(incident.declared_at, first_canary_at)},
        "detection_seconds_from_canary_attached": {
            "first_fire": since(incident.first_fire_at, live_at),
            "declared": since(incident.declared_at, live_at)},
        "detection_requests": requests_at,
        "thresholds": {
            "decision_threshold": detector.DECISION_THRESHOLD, "alpha": detector.ALPHA,
            "min_effect_ratio": detector.MIN_EFFECT_RATIO, "min_window": detector.MIN_WINDOW,
            "confirmations": args.confirmations, "psi_alert": detector.PSI_ALERT,
            "ks_alert": detector.KS_ALERT, "mean_shift_alert": detector.MEAN_SHIFT_ALERT,
            "latency_p95_ratio_alert": detector.LATENCY_P95_RATIO_ALERT},
    }
    save_record(record)
    return record


def phase_rollback(adapter, endpoint: str, args) -> dict:
    record = load_record()
    canary_id = record.get("canary", {}).get("deployed_model_id")
    production_id = record.get("production_deployment", {}).get("deployed_model_id")
    if not (canary_id and production_id):
        raise SystemExit("no canary/production ids recorded; nothing to roll back.")

    before = deployment_table(adapter, endpoint)
    print_deployments(before, "before the rollback")

    print(f"\nmoving traffic: 100% -> {production_id} (was "
          f"{[r['traffic_percent'] for r in before if r['deployed_model_id'] == canary_id]}"
          "% on the canary)")
    moved_at = now()
    split = adapter.split_traffic(endpoint, {production_id: 100})
    print(f"  split now {split}   at {moved_at}")

    # Prove it with the platform's own attestation rather than with the split we just set:
    # the split is what we asked for, the responses are what happened.
    probe_rows = payload_rows(args.probe_n, args.seed + 1)
    driver = Driver(adapter, endpoint, BLIND, ATTESTED)
    driver.run(probe_rows, "after_traffic_moved", args.concurrency)
    driver.close()
    after_shares = attested_shares(driver.attestations)
    print(f"  {args.probe_n} probes after the move, attested by deployment: {after_shares}")
    leaked = after_shares.get(canary_id, 0)
    if leaked:
        print(f"  WARNING {leaked} of {args.probe_n} still answered by the canary")

    at_zero = deployment_table(adapter, endpoint)
    print_deployments(at_zero, "traffic moved, machines NOT yet released")
    print("  the canary is at 0% and still billing — this is the line the bill notices")

    print(f"\nundeploying {canary_id} — releasing its replicas")
    undeployed_at = now()
    adapter.undeploy(endpoint, canary_id)
    after = deployment_table(adapter, endpoint)
    print_deployments(after, "after the rollback")

    record["rollback"] = {
        "traffic_moved_at": moved_at, "split_requested": {production_id: 100},
        "split_returned": split,
        "probe_requests": args.probe_n,
        "attested_shares_after_move": after_shares,
        "canary_requests_after_move": leaked,
        "at_zero_traffic_still_billing_thb_per_hour": round(
            sum(r["thb_per_hour"] for r in at_zero), 2),
        "undeployed_at": undeployed_at,
        "final_thb_per_hour": round(sum(r["thb_per_hour"] for r in after), 2),
        "final_deployments": after,
    }
    save_record(record)
    return record


def phase_status(adapter, endpoint: str, args) -> dict:
    print_deployments(deployment_table(adapter, endpoint), "attached now")
    return load_record()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("phase", choices=["reference", "deploy", "watch", "rollback", "status"])
    p.add_argument("--endpoint", default=None)
    p.add_argument("--name", default=None, help="registry name; defaults to MODEL_REGISTRY_NAME")
    p.add_argument("--canary-version", default=None, help="serving version to canary")
    p.add_argument("--traffic", type=int, default=10, help="canary's share, percent")
    p.add_argument("--adopt", action="store_true",
                   help="record the canary scripts/deploy.py already attached, rather than "
                        "deploying one here")
    p.add_argument("--instance", default="n1-standard-2")
    p.add_argument("--reference-n", type=int, default=2000)
    p.add_argument("--watch-n", type=int, default=3000)
    p.add_argument("--probe-n", type=int, default=300)
    p.add_argument("--window", type=int, default=200)
    p.add_argument("--confirmations", type=int, default=detector.CONFIRMATIONS)
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--seed", type=int, default=20260101)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = config.load()
    if cfg.provider == "local":
        print("CLOUD_PROVIDER=local has no managed endpoint.")
        return 2
    adapter = get_adapter(cfg)
    args.name = args.name or cfg.model_registry_name
    endpoint = args.endpoint or f"{args.name}-endpoint"
    if args.phase == "deploy" and not (args.canary_version or args.adopt):
        raise SystemExit("--canary-version is required to deploy a canary")

    {"reference": phase_reference, "deploy": phase_deploy, "watch": phase_watch,
     "rollback": phase_rollback, "status": phase_status}[args.phase](adapter, endpoint, args)
    print(f"\nwrote {RECORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
