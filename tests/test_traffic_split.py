"""Lab 3 Task 4 — the traffic-split arithmetic a canary deploy depends on.

These exist because of a bug that only showed up the first time a canary asked for 10%.
`deploy()` sent the new deployment's share alone and its comment claimed the deployments
already attached kept the rest. They do not: the split is replaced wholesale and rejected
unless it totals 100. The single case Task 2 exercised — the first deployment, taking
everything — is the one case where sending one entry happens to be correct, which is why the
mistake survived being used in production.

No provider is named and no SDK is imported, because none is needed: the division is the part
that had the bug in it and it is the same division wherever the split is called production
variant weights, deployment traffic percentages, or traffic across deployed models.
"""
from __future__ import annotations

import pytest

from src.traffic import NEW, split_for_new_deployment, validate


def test_first_deployment_takes_everything():
    assert split_for_new_deployment({}, [], 100) == {NEW: 100}


def test_first_deployment_cannot_take_a_share():
    """The case the old code got away with, inverted: 10% of nothing is not 100%."""
    with pytest.raises(ValueError, match="must take 100"):
        split_for_new_deployment({}, [], 10)


def test_canary_at_ten_percent_leaves_production_at_ninety():
    assert split_for_new_deployment({"prod": 100}, ["prod"], 10) == {NEW: 10, "prod": 90}


@pytest.mark.parametrize("current,traffic", [
    ({"prod": 100}, 1), ({"prod": 100}, 10), ({"prod": 100}, 50), ({"prod": 100}, 99),
    ({"a": 70, "b": 30}, 10), ({"a": 70, "b": 30}, 33), ({"a": 50, "b": 50}, 7),
    ({"a": 34, "b": 33, "c": 33}, 10), ({"a": 1, "b": 99}, 13),
    ({"a": 0, "b": 0}, 25), ({"a": 100, "b": 0}, 10),
    ({"a": 33, "b": 33, "c": 33, "d": 1}, 17),
])
def test_every_split_totals_one_hundred(current, traffic):
    """The one property every provider enforces, over the shapes a lab can produce."""
    split = split_for_new_deployment(current, list(current), traffic)
    assert sum(split.values()) == 100, split
    assert split[NEW] == traffic
    assert set(split) == set(current) | {NEW}
    assert all(v >= 0 for v in split.values())


def test_existing_shares_keep_their_proportions():
    assert split_for_new_deployment({"a": 70, "b": 30}, ["a", "b"], 10) == \
        {NEW: 10, "a": 63, "b": 27}


def test_rounding_leftovers_go_to_the_largest_fractions():
    """34/33/33 sharing 90 is 30.6/29.7/29.7, which floors to 30/29/29 = 88.

    Two points are handed out, and they go to the two LARGEST fractional parts — 0.7 and
    0.7 — not to the largest share. `a` holds the most traffic and has the smallest
    remainder, so it gets nothing and all three end level at 30. That is the largest-
    remainder method behaving correctly, and it is worth pinning because the intuitive
    wrong answer (give the spare points to the biggest deployment) also sums to 100 and
    would pass a total-only test.
    """
    split = split_for_new_deployment({"a": 34, "b": 33, "c": 33}, ["a", "b", "c"], 10)
    assert sum(split.values()) == 100
    assert split == {NEW: 10, "a": 30, "b": 30, "c": 30}


def test_rounding_is_deterministic():
    """Ties broken by id, so the same input does not produce two different splits."""
    first = split_for_new_deployment({"a": 33, "b": 33, "c": 33}, ["a", "b", "c"], 10)
    for _ in range(5):
        assert split_for_new_deployment({"a": 33, "b": 33, "c": 33}, ["c", "b", "a"], 10) == first


def test_taking_all_the_traffic_zeroes_the_others_without_detaching_them():
    split = split_for_new_deployment({"prod": 100}, ["prod"], 100)
    assert split == {NEW: 100}, "an omitted deployment goes to 0%, it does not go away"


def test_deployments_holding_nothing_share_evenly():
    split = split_for_new_deployment({"a": 0, "b": 0}, ["a", "b"], 20)
    assert sum(split.values()) == 100
    assert split["a"] == split["b"] == 40


def test_a_deployment_attached_but_absent_from_the_split_counts_as_zero():
    split = split_for_new_deployment({"a": 100}, ["a", "b"], 10)
    assert sum(split.values()) == 100
    assert split["a"] == 90 and split["b"] == 0


def test_traffic_out_of_range_is_refused():
    for bad in (-1, 101):
        with pytest.raises(ValueError, match="0-100"):
            split_for_new_deployment({"prod": 100}, ["prod"], bad)


def test_reserved_key_collision_is_refused():
    with pytest.raises(ValueError, match="reserved"):
        split_for_new_deployment({NEW: 100}, [NEW], 10)


def test_validate_accepts_a_good_split():
    split = {"a": 90, "b": 10}
    assert validate(split, attached={"a", "b"}) is split


def test_validate_refuses_a_split_that_does_not_total_one_hundred():
    with pytest.raises(ValueError, match="must total 100"):
        validate({"a": 90}, attached={"a", "b"})


def test_validate_refuses_an_unattached_id():
    with pytest.raises(LookupError, match="not attached"):
        validate({"a": 50, "typo": 50}, attached={"a", "b"})


def test_validate_allows_the_new_key_before_it_has_an_id():
    assert validate({NEW: 10, "a": 90}, attached={"a"})
