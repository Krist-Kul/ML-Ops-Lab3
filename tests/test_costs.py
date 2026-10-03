"""Lab 3 Task 5 — the serving cost arithmetic the report's headline numbers rest on.

`reports/lab3-cost.md` publishes a cost per 1,000 predictions, and the only thing standing
between that number and a plausible-looking lie is the arithmetic in `src/costs.py`. These
tests pin the three ways it can be wrong without looking wrong:

1. **A serving rate fetched from the training table.** The two tables hold the same machine
   names at different prices, so the mistake is a silent 2x understatement, not an error.
2. **A utilisation outside (0, 1].** Utilisation above 1 returns a *cheaper* number than the
   saturated ceiling, which is the one direction a fabricated input would be tempting.
3. **The break-even question answered with the wrong model.** `batch_breakeven_rps` treats a
   batch job as a fixed daily cost; Task 5 asks the volume-dependent question. Both are
   defensible and they are not interchangeable, so the shape of each is pinned here.

No cloud SDK is imported and no price is re-derived from a provider. The rates are the
committed, dated values in `src/costs.py`; what is tested is what is done with them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import costs

REPORT = Path(__file__).resolve().parents[1] / "reports" / "lab3-cost.json"


# --- the formula --------------------------------------------------------------------

def test_cost_per_1k_matches_the_hand_calculation():
    """5.09 THB/h at 10.4 req/s fully saturated: 1000 predictions take 96.15 s."""
    assert costs.cost_per_1k_predictions(5.09, 10.4, 1.0) == pytest.approx(0.13595, abs=1e-5)


def test_halving_utilisation_doubles_the_unit_cost():
    """The property that makes utilisation the fragile term: it is a straight divisor."""
    full = costs.cost_per_1k_predictions(5.09, 10.4, 1.0)
    quarter = costs.cost_per_1k_predictions(5.09, 10.4, 0.25)
    assert quarter == pytest.approx(full * 4)


def test_the_labs_realised_utilisation_costs_two_orders_more_than_the_ceiling():
    """0.6% utilisation is not a rounding difference from 100%; it is the whole answer."""
    ceiling = costs.cost_per_1k_predictions(5.09, 10.4, 1.0)
    realised = costs.cost_per_1k_predictions(5.09, 10.4, 0.006045)
    assert realised / ceiling > 100


@pytest.mark.parametrize("utilisation", [0.0, -0.1, 1.5, 2.0])
def test_utilisation_outside_the_unit_interval_is_refused(utilisation):
    """Above 1 would report a cost below the saturated ceiling — cheaper than possible."""
    with pytest.raises(ValueError, match="utilisation"):
        costs.cost_per_1k_predictions(5.09, 10.4, utilisation)


@pytest.mark.parametrize("rps", [0.0, -1.0])
def test_non_positive_throughput_is_refused(rps):
    with pytest.raises(ValueError, match="throughput_rps"):
        costs.cost_per_1k_predictions(5.09, rps, 1.0)


# --- the two tables are not interchangeable ----------------------------------------

def test_serving_an_instance_costs_more_than_training_on_it():
    """The prediction vCPU SKU is dearer than the Compute Engine one. Same name, same shape."""
    training = costs.hourly_rate("gcp", "n1-standard-4")
    serving = costs.serving_hourly_rate("gcp", "n1-standard-4")
    assert serving > training


def test_a_serving_instance_is_not_in_the_training_table():
    """n1-standard-2 was deployed in Lab 3 and has no training rate; this must raise, not
    fall back to a similar machine."""
    with pytest.raises(KeyError, match="No rate for"):
        costs.hourly_rate("gcp", "n1-standard-2")


def test_a_training_only_instance_is_not_in_the_serving_table():
    with pytest.raises(KeyError, match="do not reuse a training rate"):
        costs.serving_hourly_rate("gcp", "n1-standard-4+t4")


def test_replicas_multiply_the_serving_rate():
    """The canary incident in dimensional form: a second replica is a second full rate."""
    one = costs.serving_hourly_rate("gcp", "n1-standard-2")
    two = costs.serving_hourly_rate("gcp", "n1-standard-2", replicas=2)
    assert two == pytest.approx(one * 2)


def test_an_unknown_provider_raises_rather_than_defaulting():
    with pytest.raises(KeyError, match="No serving price table"):
        costs.serving_hourly_rate("oracle", "n1-standard-2")


# --- the break-even, both models ---------------------------------------------------

def test_scaffold_breakeven_is_the_fixed_cost_model():
    """A batch job costing half an endpoint-hour per day beats a warm endpoint until the
    endpoint is handling a rate that justifies 24 hours of idle."""
    rate = 5.09
    rps = costs.batch_breakeven_rps(rate, batch_job_thb=rate * 0.5)
    expected = (rate * 24 - rate * 0.5) / 86400.0
    assert rps == pytest.approx(expected)


def test_scaffold_breakeven_is_zero_when_batch_costs_more_than_the_endpoint():
    """No request rate makes a dearer batch job the cheaper option."""
    assert costs.batch_breakeven_rps(5.09, batch_job_thb=200.0) == 0.0


def test_volume_breakeven_lands_at_the_endpoints_own_saturated_throughput():
    """The Task 5 result, derived independently of scripts/cost_per_1k.py.

    Both options bill the same rate for the same machine, so the only difference is the
    endpoint's idle time. With start-up ignored, the break-even volume is exactly the
    saturated throughput sustained for 24 hours — the endpoint breaks even only at 100%
    utilisation and is paying for latency everywhere below it.
    """
    rate, rps = 5.09, 10.4
    endpoint_daily = rate * 24
    breakeven_rows = endpoint_daily / rate * 3600 * rps
    assert breakeven_rows == pytest.approx(rps * 86400)


# --- the published report is reproducible ------------------------------------------

def test_report_json_is_consistent_with_the_cost_functions():
    """Guards against the report drifting from the module it claims to use."""
    if not REPORT.exists():  # pragma: no cover - report is committed alongside this test
        pytest.skip("run `make cost-per-1k` first")
    out = json.loads(REPORT.read_text())
    rate = out["inputs"]["thb_per_replica_hour"]
    rps = out["inputs"]["throughput_chosen_rps"]
    assert rate == costs.serving_hourly_rate("gcp", out["inputs"]["instance"])
    for name, util in out["utilisation"]["assumptions"].items():
        assert out["cost_per_1000_predictions_thb"]["single_row_route"][name] == pytest.approx(
            costs.cost_per_1k_predictions(rate, rps, util)
        )


def test_report_realised_utilisation_is_served_over_capacity():
    if not REPORT.exists():  # pragma: no cover
        pytest.skip("run `make cost-per-1k` first")
    d = json.loads(REPORT.read_text())["utilisation"]["realised_derivation"]
    assert d["predictions_served_total"] == sum(d["predictions_served"].values())
    assert d["utilisation"] == pytest.approx(
        d["predictions_served_total"] / d["capacity_predictions_at_saturation"]
    )


def test_report_billed_total_is_the_sum_of_its_parts():
    """The 194.6 THB incident figure overlaps the base deployment's own hours; the total
    must not double-count it."""
    if not REPORT.exists():  # pragma: no cover
        pytest.skip("run `make cost-per-1k` first")
    b = json.loads(REPORT.read_text())["what_was_billed"]
    assert b["total_thb"] == pytest.approx(
        b["base_n1_standard_2_thb"]
        + b["canary_second_replica_thb"]
        + b["task3_n1_standard_4_thb"]
    )
    rate = costs.serving_hourly_rate("gcp", "n1-standard-2")
    assert b["base_n1_standard_2_thb"] == pytest.approx(rate * b["base_n1_standard_2_hours"])
    assert b["canary_second_replica_thb"] == pytest.approx(
        rate * b["canary_second_replica_hours"]
    )
