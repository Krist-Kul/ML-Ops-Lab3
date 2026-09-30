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
# --- Lab 3: serving ----------------------------------------------------------
# A prediction hour is NOT a training hour for the same machine, and assuming it is
# understates a serving bill. Verified 2026-09-30 against the Cloud Billing Catalog API
# (service C7E2-9256-1C43, Vertex AI) for asia-southeast1, at the same 33.355 THB/USD used
# in reports/lab2-prices.md:
#
#   N1 prediction vCPU-hour  0.045000 + 0.005870 management = 0.050870 USD
#   N1 prediction GiB-hour   0.006000 + 0.000783 management = 0.006783 USD
#   E2 prediction vCPU-hour  0.030945 + 0.004036 management = 0.034981 USD
#   E2 prediction GiB-hour   0.004146 + 0.000541 management = 0.004687 USD
#
# The N1 prediction core SKU (0.045000) is a different, dearer SKU from the Compute Engine
# core the training table is built on (0.038999): serving the same machine type costs about
# 13% more per vCPU-hour than training on it. E2 is ~30% cheaper than N1 for the same shape.
#
# These are per REPLICA-hour, and they are charged whether or not anything calls the
# endpoint. That is the difference between this table and PRICE_TABLE: a training job that
# finishes stops costing money, and an endpoint does not.
SERVING_PRICE_TABLE: dict[str, dict[str, float]] = {
    "local": {"local": 0.0},
    "gcp": {
        "n1-standard-2": 5.09,   # 2 vCPU, 7.5 GiB -> 0.152613 USD/h
        "n1-standard-4": 10.18,  # 4 vCPU, 15 GiB  -> 0.305225 USD/h
        "e2-standard-2": 3.58,   # 2 vCPU, 8 GiB   -> 0.107458 USD/h
        "e2-standard-4": 7.17,   # 4 vCPU, 16 GiB  -> 0.214916 USD/h
    },
    # TODO(Lab 3): aws and azure serving rates are unverified. Add them when you deploy there.
}


def serving_hourly_rate(provider: str, instance: str, replicas: int = 1) -> float:
    """THB per hour for `replicas` replicas of `instance`, idle or busy.

    Deliberately separate from `hourly_rate`: passing a serving instance to the training
    table would silently return a training price, and the two differ by more than rounding.
    """
    table = SERVING_PRICE_TABLE.get(provider.lower())
    if table is None:
        raise KeyError(f"No serving price table for {provider!r}. Add it to src/costs.py.")
    if instance not in table:
        raise KeyError(
            f"No serving rate for {instance!r} on {provider}. Known: {sorted(table)}. "
            "Verify the rate for the machine you deployed — do not reuse a training rate."
        )
    return table[instance] * replicas


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
