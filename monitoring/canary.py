"""Lab 3 Task 4 — detect a degraded canary from metrics alone.

The rule the handout sets is that the degradation must be found "from metrics alone,
without looking at which version is which". That is a statement about what this module is
allowed to see, so it is enforced structurally rather than promised: `evaluate` takes
scores and latencies and has no parameter for a version, a deployment id or a traffic
split, and nothing in this file imports a provider SDK. The driver writes the version
field to a separate file this code never reads.

Thresholds are committed before the canary is deployed, for the same reason the latency
target in `loadtest/k6.js` was: a threshold chosen once the incident is visible is not a
detector, it is a description.

    python -m monitoring.canary --reference ref.jsonl --current cur.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitoring.drift import ks_statistic, psi  # noqa: E402

# --- the thresholds, fixed in advance -----------------------------------------------------
#
# DECISION_THRESHOLD is not a detector parameter, it is the product's. This model scores a
# maintenance alert, and 0.5 is the point at which the alert fires, so the rate of alerts
# is the one number a plant operator would notice changing without being told anything about
# models. Monitoring it is monitoring the thing the service is for.
DECISION_THRESHOLD = 0.5

# A rate is compared against the reference with an exact one-sided binomial test, so the
# question asked is "could this many alerts in this many requests have come from the
# reference rate by chance". ALPHA is that answer's cutoff.
#
# 0.01 rather than 0.05 because this detector will be evaluated on many overlapping windows
# during one incident, and at 0.05 a quiet hour of twelve windows produces a false alarm
# about half the time. A rollback is disruptive; the alarm has to be worth acting on.
ALPHA = 0.01

# Statistical significance is not the same as mattering. A large enough window makes a 1%
# relative change significant, and nobody should roll back for that. A signal must be BOTH
# significant at ALPHA and at least this much worse in relative terms.
MIN_EFFECT_RATIO = 1.5

# A window smaller than this is not evaluated at all. At the endpoint's measured ~10 req/s
# this is ten seconds of traffic, and it keeps the first few requests after a deploy — which
# arrive while a replica is still warming — from deciding anything.
MIN_WINDOW = 100

# Two consecutive windows must agree before an incident is DECLARED. One window that fires
# alone is recorded as a first-fire and nothing else: a single window at ALPHA=0.01 is a
# 1-in-100 event, and 1-in-100 events happen. This is the knob that trades detection time
# for false alarms, and the report states what it cost.
CONFIRMATIONS = 2

# Conventional PSI reading, inherited from monitoring/drift.py, which inherited it from
# credit scoring. Kept unchanged ON PURPOSE so that this run can show what the textbook
# number does on a 10% canary rather than quietly tuning it until it works.
PSI_ALERT = 0.10
KS_ALERT = 0.10

# Relative shift in the mean predicted probability. Deliberately loose: the mean of a
# skewed score distribution is a blunt instrument, and it is here to be compared against
# the rate signals, not because it is expected to win.
MEAN_SHIFT_ALERT = 0.20

# Latency is in the set because a canary can be worse by being slow rather than by being
# wrong, and a detector that only watches scores would miss that entirely.
LATENCY_P95_RATIO_ALERT = 1.5


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact, in log space.

    Written out rather than imported so the one number the alarm turns on is readable. The
    log-space accumulation is not decoration: at n in the thousands the individual terms
    underflow to zero in float64 and the tail probability silently becomes 0.0, which would
    make every window fire.
    """
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0 if k > 0 else 1.0
    if p >= 1.0:
        return 1.0
    total = 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    for i in range(k, n + 1):
        term = (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                + i * log_p + (n - i) * log_q)
        total += math.exp(term)
    return min(total, 1.0)


@dataclass
class Reference:
    """What normal looked like, measured before the canary existed.

    Captured from the live endpoint rather than computed offline: the reference has to be
    the production model on production hardware, or a difference in either shows up as a
    false incident the first time the detector runs.
    """

    n: int
    mean_score: float
    alert_rate: float
    tail_cut: float          # the reference's own 99th percentile
    tail_rate: float         # its mass above that cut, ~0.01 by construction
    latency_p95_ms: float
    scores: list[float] = field(default_factory=list)

    @classmethod
    def from_samples(cls, scores: list[float], latencies_ms: list[float]) -> Reference:
        arr = np.asarray(scores, dtype=float)
        cut = float(np.percentile(arr, 99))
        return cls(
            n=len(arr),
            mean_score=float(arr.mean()),
            alert_rate=float((arr > DECISION_THRESHOLD).mean()),
            tail_cut=cut,
            tail_rate=float((arr > cut).mean()),
            latency_p95_ms=float(np.percentile(np.asarray(latencies_ms, dtype=float), 95))
            if latencies_ms else 0.0,
            scores=[float(x) for x in arr],
        )

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("scores")
        return d


@dataclass
class Signal:
    name: str
    value: float
    baseline: float
    threshold: float
    ratio: float
    p_value: float | None
    fired: bool
    note: str


def evaluate(reference: Reference, scores: list[float],
             latencies_ms: list[float] | None = None) -> list[Signal]:
    """Score one window against the reference. Knows nothing about versions.

    Every signal reports its value whether or not it fired, because "PSI did not fire" is
    the finding in a 10% canary and a detector that only prints its alarms cannot say that.
    """
    latencies_ms = latencies_ms or []
    cur = np.asarray(scores, dtype=float)
    n = len(cur)
    out: list[Signal] = []

    def rate_signal(name: str, hits: int, baseline: float, note: str) -> Signal:
        observed = hits / n if n else 0.0
        p = binom_sf(hits, n, baseline) if n else 1.0
        ratio = (observed / baseline) if baseline > 0 else float("inf")
        return Signal(name=name, value=round(observed, 6), baseline=round(baseline, 6),
                      threshold=ALPHA, ratio=round(ratio, 3), p_value=p,
                      fired=bool(p < ALPHA and ratio >= MIN_EFFECT_RATIO), note=note)

    # The two rate signals. Both are one-sided: a canary that fires FEWER alerts than
    # production is also a change, but it is not the failure this endpoint is exposed to,
    # and a two-sided test here would double the false alarm rate to catch a case that
    # would be caught by the distribution signals anyway.
    out.append(rate_signal(
        "alert_rate", int((cur > DECISION_THRESHOLD).sum()), reference.alert_rate,
        f"share of scores above the product's decision threshold {DECISION_THRESHOLD}"))
    out.append(rate_signal(
        "tail_rate", int((cur > reference.tail_cut).sum()), reference.tail_rate,
        f"share above the reference's own p99 ({reference.tail_cut:.4f}); a rate fixed at "
        "1% by construction, so the multiple it shows does not depend on where the "
        "product's decision threshold happens to sit"))

    ref_scores = np.asarray(reference.scores, dtype=float)
    psi_value = psi(ref_scores, cur)
    out.append(Signal("psi", round(psi_value, 5), 0.0, PSI_ALERT,
                      round(psi_value / PSI_ALERT, 3), None, bool(psi_value >= PSI_ALERT),
                      "population stability index on the score distribution"))

    ks_value = ks_statistic(ref_scores, cur)
    out.append(Signal("ks", round(ks_value, 5), 0.0, KS_ALERT,
                      round(ks_value / KS_ALERT, 3), None, bool(ks_value >= KS_ALERT),
                      "largest gap between the two score CDFs"))

    mean_now = float(cur.mean()) if n else 0.0
    shift = abs(mean_now - reference.mean_score) / reference.mean_score \
        if reference.mean_score else 0.0
    out.append(Signal("mean_score", round(mean_now, 5), round(reference.mean_score, 5),
                      MEAN_SHIFT_ALERT, round(shift, 3), None,
                      bool(shift >= MEAN_SHIFT_ALERT),
                      "relative shift in the mean predicted probability"))

    if latencies_ms and reference.latency_p95_ms:
        p95 = float(np.percentile(np.asarray(latencies_ms, dtype=float), 95))
        ratio = p95 / reference.latency_p95_ms
        out.append(Signal("latency_p95", round(p95, 2), round(reference.latency_p95_ms, 2),
                          LATENCY_P95_RATIO_ALERT, round(ratio, 3), None,
                          bool(ratio >= LATENCY_P95_RATIO_ALERT),
                          "a canary can be worse by being slow rather than by being wrong"))

    return out


@dataclass
class Incident:
    declared: bool = False
    first_fire_at: str | None = None
    first_fire_by: list[str] = field(default_factory=list)
    declared_at: str | None = None
    declared_by: list[str] = field(default_factory=list)
    consecutive: int = 0


class Detector:
    """Stateful wrapper that turns per-window signals into one declared incident.

    Separate from `evaluate` because "a window fired" and "there is an incident" are
    different claims and the report needs both timestamps: the gap between them is exactly
    what CONFIRMATIONS costs in detection time.
    """

    def __init__(self, reference: Reference, confirmations: int = CONFIRMATIONS) -> None:
        self.reference = reference
        self.confirmations = confirmations
        self.incident = Incident()

    def observe(self, at: str, scores: list[float],
                latencies_ms: list[float] | None = None) -> tuple[list[Signal], Incident]:
        if len(scores) < MIN_WINDOW:
            return [], self.incident
        signals = evaluate(self.reference, scores, latencies_ms)
        firing = [s.name for s in signals if s.fired]
        if firing:
            if self.incident.first_fire_at is None:
                self.incident.first_fire_at = at
                self.incident.first_fire_by = firing
            self.incident.consecutive += 1
            if self.incident.consecutive >= self.confirmations and not self.incident.declared:
                self.incident.declared = True
                self.incident.declared_at = at
                self.incident.declared_by = firing
        else:
            self.incident.consecutive = 0
        return signals, self.incident


def load_stream(path: Path) -> tuple[list[float], list[float], list[str]]:
    """Read a blind stream: one JSON object per line, scores and latencies only.

    Raises if a version field is present. The handout's constraint is that detection does
    not look at which version is which, and the cheapest way to keep that true a month from
    now is to make the file that carries a version unreadable by this code.
    """
    scores: list[float] = []
    latencies: list[float] = []
    stamps: list[str] = []
    forbidden = {"model_version", "model_version_id", "deployed_model_id", "version"}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        leaked = forbidden.intersection(row)
        if leaked:
            raise ValueError(
                f"{path} carries {sorted(leaked)}. This is the blind stream; the attested "
                "stream is a separate file. Detection must not see the version.")
        scores.append(float(row["probability"]))
        if "latency_ms" in row:
            latencies.append(float(row["latency_ms"]))
        stamps.append(str(row.get("ts", "")))
    return scores, latencies, stamps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reference", type=Path, required=True,
                    help="blind stream captured BEFORE the canary was deployed")
    ap.add_argument("--current", type=Path, required=True, help="blind stream to judge")
    ap.add_argument("--window", type=int, default=200,
                    help="requests per evaluation window")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    ref_scores, ref_lat, _ = load_stream(args.reference)
    reference = Reference.from_samples(ref_scores, ref_lat)
    cur_scores, cur_lat, cur_ts = load_stream(args.current)

    detector = Detector(reference)
    history = []
    print(f"reference  n={reference.n}  mean={reference.mean_score:.4f}  "
          f"alert_rate={reference.alert_rate:.4f}  p99={reference.tail_cut:.4f}")
    for end in range(args.window, len(cur_scores) + 1, args.window):
        lo = end - args.window
        at = cur_ts[end - 1] if end - 1 < len(cur_ts) else str(end)
        signals, incident = detector.observe(
            at, cur_scores[lo:end], cur_lat[lo:end] if cur_lat else None)
        history.append({"at": at, "through_request": end,
                        "signals": [asdict(s) for s in signals]})
        flags = ",".join(s.name for s in signals if s.fired) or "-"
        print(f"  n<={end:6d}  {at}  fired: {flags}")

    inc = detector.incident
    print(f"\nfirst fire  {inc.first_fire_at}  by {inc.first_fire_by or '-'}")
    print(f"declared    {inc.declared_at}  by {inc.declared_by or '-'}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(
            {"reference": reference.to_json(), "incident": asdict(inc),
             "thresholds": {"decision_threshold": DECISION_THRESHOLD, "alpha": ALPHA,
                            "min_effect_ratio": MIN_EFFECT_RATIO, "min_window": MIN_WINDOW,
                            "confirmations": CONFIRMATIONS, "psi_alert": PSI_ALERT,
                            "ks_alert": KS_ALERT, "mean_shift_alert": MEAN_SHIFT_ALERT,
                            "latency_p95_ratio_alert": LATENCY_P95_RATIO_ALERT},
             "windows": history}, indent=2))
    return 0 if not inc.declared else 1


if __name__ == "__main__":
    raise SystemExit(main())
