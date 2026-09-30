"""Lab 3 Task 4 — tests for the blind canary detector.

Two of these pin behaviour that is a FINDING rather than a feature: that the rate signals
fire on a 10% mixture and that PSI does not. If a later change makes PSI fire there, the
report's central claim has stopped being true and a failing test is the right way to hear
about it.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from monitoring import canary


def _reference_scores(n: int = 4000, seed: int = 0) -> np.ndarray:
    """A skewed score distribution like the production model's: mostly near zero."""
    rng = np.random.default_rng(seed)
    return rng.beta(1.2, 9.0, size=n)


def _degraded_scores(n: int = 4000, seed: int = 1) -> np.ndarray:
    """Same ranking, different calibration — shifted up, which is what reweighting does."""
    rng = np.random.default_rng(seed)
    return rng.beta(2.5, 5.0, size=n)


def _mixture(ref: np.ndarray, bad: np.ndarray, share: float, n: int, seed: int = 2) -> list[float]:
    rng = np.random.default_rng(seed)
    n_bad = int(round(n * share))
    return [float(x) for x in np.concatenate([
        rng.choice(ref, n - n_bad), rng.choice(bad, n_bad)])]


def test_binom_sf_matches_scipy():
    scipy_stats = pytest.importorskip("scipy.stats")
    for k, n, p in [(1, 10, 0.5), (7, 200, 0.01), (30, 500, 0.025), (0, 5, 0.3), (5, 5, 0.9)]:
        expected = scipy_stats.binom.sf(k - 1, n, p)
        assert canary.binom_sf(k, n, p) == pytest.approx(expected, rel=1e-9, abs=1e-15)


def test_binom_sf_edges():
    assert canary.binom_sf(0, 100, 0.1) == 1.0
    assert canary.binom_sf(101, 100, 0.1) == 0.0
    # The log-space accumulation exists for this case: a naive product underflows and would
    # return 0.0, making every window fire.
    assert canary.binom_sf(200, 5000, 0.02) > 0.0


def test_no_signal_fires_on_the_reference_itself():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    # A fresh sample from the same distribution, not the same array, so this is not a
    # tautology about identical inputs.
    window = list(_reference_scores(600, seed=99))
    signals = canary.evaluate(reference, window, [20.0] * len(window))
    fired = [s.name for s in signals if s.fired]
    assert fired == [], f"false alarm on unchanged traffic: {fired}"


def test_rate_signals_fire_on_a_ten_percent_mixture():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    window = _mixture(ref, _degraded_scores(), share=0.10, n=600)
    by_name = {s.name: s for s in canary.evaluate(reference, window, [20.0] * len(window))}
    assert by_name["tail_rate"].fired, by_name["tail_rate"]
    assert by_name["alert_rate"].fired, by_name["alert_rate"]
    # The invariant is about rarity, not about which signal it happens to be: whichever
    # event is rarer in the reference shows the larger multiple, because a mixture adds a
    # roughly fixed amount of mass to a smaller denominator. Which of the two is rarer
    # depends on where the decision threshold falls relative to the p99, so the test asserts
    # the ordering rather than naming a winner.
    rarer, commoner = sorted((by_name["tail_rate"], by_name["alert_rate"]),
                             key=lambda s: s.baseline)
    assert rarer.ratio > commoner.ratio


def test_psi_does_not_fire_on_a_ten_percent_mixture():
    """The finding, pinned. A 10% canary moves PSI far less than the textbook threshold."""
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    window = _mixture(ref, _degraded_scores(), share=0.10, n=600)
    by_name = {s.name: s for s in canary.evaluate(reference, window, [20.0] * len(window))}
    assert not by_name["psi"].fired
    assert by_name["psi"].value < canary.PSI_ALERT


def test_psi_does_fire_at_fifty_fifty():
    """The other half of it: PSI is not useless, it is insensitive at 10%."""
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    window = _mixture(ref, _degraded_scores(), share=0.50, n=600)
    by_name = {s.name: s for s in canary.evaluate(reference, window, [20.0] * len(window))}
    assert by_name["psi"].fired
    assert by_name["psi"].value >= canary.PSI_ALERT


def test_significant_but_trivial_change_does_not_fire():
    """MIN_EFFECT_RATIO earns its place: significance alone must not trigger a rollback."""
    ref = _reference_scores(20000)
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    window = _mixture(ref, _degraded_scores(), share=0.005, n=8000)
    by_name = {s.name: s for s in canary.evaluate(reference, window)}
    for name in ("alert_rate", "tail_rate"):
        signal = by_name[name]
        if signal.p_value is not None and signal.p_value < canary.ALPHA:
            assert signal.ratio < canary.MIN_EFFECT_RATIO or not signal.fired


def test_latency_signal_catches_a_slow_canary_with_identical_scores():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    window = list(_reference_scores(600, seed=7))
    slow = [20.0] * 540 + [900.0] * 60
    by_name = {s.name: s for s in canary.evaluate(reference, window, slow)}
    assert by_name["latency_p95"].fired
    assert not by_name["alert_rate"].fired


def test_detector_requires_confirmation_before_declaring():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    detector = canary.Detector(reference, confirmations=2)
    bad = _mixture(ref, _degraded_scores(), share=0.10, n=600)

    _, incident = detector.observe("t1", bad)
    assert incident.first_fire_at == "t1"
    assert not incident.declared, "one window must not be enough"

    _, incident = detector.observe("t2", bad)
    assert incident.declared and incident.declared_at == "t2"
    assert incident.first_fire_at == "t1", "the first fire keeps its own timestamp"


def test_a_single_fluke_window_does_not_declare():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    detector = canary.Detector(reference, confirmations=2)
    detector.observe("t1", _mixture(ref, _degraded_scores(), share=0.10, n=600))
    detector.observe("t2", list(_reference_scores(600, seed=11)))
    assert not detector.incident.declared
    assert detector.incident.consecutive == 0, "a clean window must reset the run"


def test_short_windows_are_not_judged():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [20.0] * len(ref))
    detector = canary.Detector(reference)
    signals, incident = detector.observe("t1", [0.9] * (canary.MIN_WINDOW - 1))
    assert signals == [] and incident.first_fire_at is None


def test_blind_stream_refuses_a_version_field(tmp_path):
    path = tmp_path / "leaky.jsonl"
    path.write_text(json.dumps({"ts": "t", "probability": 0.1, "model_version_id": "5"}) + "\n")
    with pytest.raises(ValueError, match="blind stream"):
        canary.load_stream(path)


def test_blind_stream_reads_scores_and_latencies(tmp_path):
    path = tmp_path / "ok.jsonl"
    path.write_text("\n".join(json.dumps({"ts": f"t{i}", "probability": i / 10,
                                          "latency_ms": 20.0 + i}) for i in range(5)) + "\n")
    scores, latencies, stamps = canary.load_stream(path)
    assert scores == [0.0, 0.1, 0.2, 0.3, 0.4]
    assert latencies == [20.0, 21.0, 22.0, 23.0, 24.0]
    assert stamps == ["t0", "t1", "t2", "t3", "t4"]


def test_evaluate_takes_no_version_argument():
    """The constraint is structural, so it is asserted rather than trusted."""
    import inspect
    params = set(inspect.signature(canary.evaluate).parameters)
    assert params == {"reference", "scores", "latencies_ms"}
    for forbidden in ("version", "model_version", "deployed_model_id", "traffic"):
        assert forbidden not in params


def test_reference_tail_cut_is_its_own_p99():
    ref = _reference_scores()
    reference = canary.Reference.from_samples(list(ref), [])
    assert reference.tail_rate == pytest.approx(0.01, abs=0.003)
    assert math.isclose(reference.tail_cut, float(np.percentile(ref, 99)))
