"""Traffic-split arithmetic for canaries and rollbacks.

Neutral on purpose. Every provider spells this differently — production variant weights,
deployment traffic percentages, traffic split across deployed models — and every one of them
wants integer shares that total exactly 100. The division is the part that has the bug in it;
the API call is the part that has the provider in it. They belong in different files.

This module exists because of a bug worth remembering. `deploy()` used to send only the new
deployment's share and its comment claimed the deployments already attached kept the rest.
They do not: the split is replaced wholesale and rejected unless it totals 100. The single
case Lab 3 Task 2 exercised — the first deployment, taking everything — is the one case where
sending one entry happens to be correct, so the mistake survived until a canary asked for 10%.
"""
from __future__ import annotations

NEW = "0"
"""The key a provider uses for the deployment being created by the request in flight.

It is a string rather than an int because the split is a map with string keys, and "0" is not
a magic number here — it is the position of the new deployment in the request.
"""


def split_for_new_deployment(current: dict[str, int], deployment_ids: list[str],
                             traffic: int, new_key: str = NEW) -> dict[str, int]:
    """The complete split to send when attaching a deployment that takes `traffic` percent.

    `current` maps already-attached deployment ids to the share each holds now;
    `deployment_ids` is everything attached, including any holding nothing. The remainder,
    100 - traffic, is divided among them in proportion to what they hold, so a canary at 10%
    leaves one production deployment at 90, and leaves a 70/30 pair at 63/27 rather than
    silently levelling them.

    Always totals exactly 100, which is the only thing the providers agree on.
    """
    if not 0 <= traffic <= 100:
        raise ValueError(f"traffic must be 0-100, got {traffic}")
    if new_key in deployment_ids:
        raise ValueError(f"{new_key!r} is reserved for the deployment being created, but it "
                         "is also the id of one already attached.")

    remainder = 100 - traffic

    if not deployment_ids:
        if remainder:
            raise ValueError(
                f"traffic={traffic} leaves {remainder}% for deployments that do not exist "
                "yet. The first deployment on an endpoint must take 100%.")
        return {new_key: traffic}

    if remainder == 0:
        # Everything already attached goes to 0%. Still attached, still holding its machines,
        # still billing — releasing those is undeploy's job, not this function's.
        return {new_key: 100}

    weights = {k: max(current.get(k, 0), 0) for k in deployment_ids}
    total = sum(weights.values())
    if total == 0:
        # Nothing holds traffic: an endpoint caught mid-rollback, say. Even shares are the
        # only defensible reading of "in proportion to what they hold now".
        weights = {k: 1 for k in deployment_ids}
        total = len(deployment_ids)

    exact = {k: remainder * w / total for k, w in weights.items()}
    split = {k: int(v) for k, v in exact.items()}
    # Floor everything, then hand the leftover points to the largest fractional parts. Without
    # this a 34/33/33 trio sharing 90 floors to 30/29/29 = 88 and the call is rejected.
    short = remainder - sum(split.values())
    for key in sorted(exact, key=lambda k: (exact[k] - split[k], k), reverse=True)[:short]:
        split[key] += 1
    split[new_key] = traffic
    return split


def validate(split: dict[str, int], attached: set[str], new_key: str = NEW) -> dict[str, int]:
    """Check a split before it is sent. Returns it unchanged so it can be used inline.

    Two failures worth catching locally rather than as a provider error: a total that is not
    100, which is how every deployment silently loses its traffic, and an id that is not
    attached, which is the same mistake spelled as a typo.
    """
    total = sum(split.values())
    if total != 100:
        raise ValueError(
            f"traffic split must total 100, got {total} from {split}. A split that does not "
            "add up is usually a forgotten deployment about to lose all its traffic.")
    unknown = {k for k in split if k != new_key} - attached
    if unknown:
        raise LookupError(f"{sorted(unknown)} is not attached. Attached: {sorted(attached)}.")
    return split
