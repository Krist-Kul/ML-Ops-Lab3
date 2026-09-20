"""Cost model. Used by Labs 2, 3, and 5.

Prices change and differ by region, so these are STARTING VALUES you must verify against
your provider's calculator. Verifying them is part of Lab 2; citing a stale number without
checking is the kind of thing the cost report is designed to catch.

Rates are THB per hour, on-demand. Discounted (spot / low-priority / preemptible) compute
is roughly 30% of on-demand across all three providers — SPOT_FACTOR below.
"""
from __future__ import annotations

# GCP rows verified 2026-09-21 against the Cloud Billing Catalog API for asia-southeast1,
# at 33.355 THB/USD (2026-09-18). A Vertex training hour is the Compute Engine price plus a
# management fee, so it is NOT the VM price you find on the Compute Engine page:
# n1-standard-4 = 4 x (0.038999 + 0.005850) + 15 x (0.005226 + 0.000784) USD/h. Working and
# sources are in reports/lab2-comparison.md.
# TODO(Lab 2): the aws and azure rows are still the handout's starting values. Verify them
# against your own region before using them for anything.
PRICE_TABLE: dict[str, dict[str, float]] = {
    "local": {"local": 0.0},
    "aws": {
        "ml.m5.large": 4.2,
        "ml.m5.xlarge": 8.4,
        "ml.c5.xlarge": 7.3,
        "ml.g4dn.xlarge": 26.0,
    },
    "azure": {
        "Standard_DS3_v2": 8.1,
        "Standard_F4s_v2": 6.9,
        "Standard_NC4as_T4_v3": 24.5,
    },
    "gcp": {
        "n1-standard-4": 8.99,
        "e2-standard-4": 6.34,
        "n1-standard-4+t4": 23.19,
    },
}

SPOT_FACTOR = 0.30

# The 30% rule of thumb does not survive contact with a price list. The management fee is
# charged at the same rate on spot as on demand, and the underlying discount varies by
# machine type: in asia-southeast1 an N1 spot hour is 34% of on-demand while an E2 spot hour
# is 65% — cheap spot capacity is a property of a specific machine type in a specific region,
# not of "spot". Measured the same way and on the same date as PRICE_TABLE.
SPOT_FACTORS: dict[str, dict[str, float]] = {
    "gcp": {
        "n1-standard-4": 0.34,
        "e2-standard-4": 0.65,
        "n1-standard-4+t4": 0.37,
    },
}
DEFAULT_UTILISATIONS = (0.05, 0.25, 0.80)


def hourly_rate(provider: str, instance: str, spot: bool = False) -> float:
    table = PRICE_TABLE.get(provider.lower())
    if table is None:
        raise KeyError(f"No price table for provider {provider!r}. Add it to src/costs.py.")
    if instance not in table:
        raise KeyError(
            f"No rate for {instance!r} on {provider}. Known: {sorted(table)}. "
            "Add the instance you actually used — do not substitute a similar one silently."
        )
    if not spot:
        return table[instance]
    return table[instance] * SPOT_FACTORS.get(provider.lower(), {}).get(instance, SPOT_FACTOR)


def cost_per_1k_predictions(
    hourly_thb: float,
    throughput_rps: float,
    utilisation: float,
) -> float:
    """Cost of 1,000 predictions on an always-on endpoint.

    utilisation is the fraction of provisioned capacity you actually use. It is the most
    fragile number in any serving cost estimate, which is why Lab 3 makes you state it
    explicitly and Lab 5 makes you report three of them.
    """
    if not 0 < utilisation <= 1:
        raise ValueError("utilisation must be in (0, 1]")
    if throughput_rps <= 0:
        raise ValueError("throughput_rps must be positive")
    effective_rps = throughput_rps * utilisation
    seconds_per_1k = 1000.0 / effective_rps
    return hourly_thb * (seconds_per_1k / 3600.0)


def batch_breakeven_rps(
    endpoint_hourly_thb: float,
    batch_job_thb: float,
    batch_runs_per_day: int = 1,
) -> float:
    """Request rate below which scheduled batch inference is cheaper than a warm endpoint.

    Lab 3 asks you to compute this for your own service. The answer is usually lower than
    students expect, which is the point.
    """
    endpoint_daily = endpoint_hourly_thb * 24
    batch_daily = batch_job_thb * batch_runs_per_day
    if batch_daily >= endpoint_daily:
        return 0.0
    return (endpoint_daily - batch_daily) / 86400.0
