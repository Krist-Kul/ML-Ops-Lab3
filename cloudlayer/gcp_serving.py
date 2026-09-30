"""Vertex AI serving shim — Layer 3.

The handout's rule: do not rename the routes in `service/app.py`, absorb the provider's
expectations in `cloudlayer/`. This module is that absorption. It imports the neutral app
and adds exactly two provider-shaped things:

  * the request envelope. Vertex POSTs ``{"instances": [...]}`` to the container's predict
    route and requires ``{"predictions": [...]}`` back, one prediction per instance, in
    order. `/predict` and `/predict/batch` keep their own contract for every other caller.
  * the environment translation. Vertex hands the container its artifact as a storage URI
    in ``AIP_STORAGE_URI``; the neutral service reads ``MODEL_URI`` and does not know the
    name of any provider's variable.

The image is unchanged and its default CMD still runs `service.app:app`. The Vertex model
resource asks for this module instead, so the same bytes serve a plain HTTP client, a
SageMaker endpoint, or this:

    uvicorn cloudlayer.gcp_serving:app --host 0.0.0.0 --port 8080

Routes are read from the AIP_* variables rather than hard-coded, because they are declared
on the Model resource and Vertex is then the authority on what it will call.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

# Translate before the neutral app is imported: its module-level state reads MODEL_VERSION
# and the loader thread starts at startup, not at import, but the ordering is easier to
# reason about than to remember.
if os.environ.get("AIP_STORAGE_URI") and not os.environ.get("MODEL_URI"):
    os.environ["MODEL_URI"] = os.environ["AIP_STORAGE_URI"]

from service.app import STATE, _score, app  # noqa: E402
from service.schemas import PredictRequest  # noqa: E402

PREDICT_ROUTE = os.environ.get("AIP_PREDICT_ROUTE", "/v1/predict")
HEALTH_ROUTE = os.environ.get("AIP_HEALTH_ROUTE", "/ready")


class VertexPredictRequest(BaseModel):
    """Vertex's envelope. The rows inside are validated by the same schema as `/predict`,
    so a malformed instance is a 422 here too rather than a confident number from
    nonsense. The 100-row cap matches `/predict/batch`; Vertex's own limit is on payload
    bytes, which is the wrong unit to promise a caller."""

    instances: list[PredictRequest] = Field(..., min_length=1, max_length=100)
    parameters: dict[str, Any] | None = None


@app.post(PREDICT_ROUTE)
def vertex_predict(payload: VertexPredictRequest) -> dict[str, Any]:
    scores = _score([row.model_dump() for row in payload.instances])
    # One prediction per instance, in order, each carrying the version that produced it.
    # Vertex's own PredictResponse also names the deployed version; both are recorded in
    # Task 4, because they answer different questions — which model scored this row, and
    # which deployment the traffic split sent it to.
    return {
        "predictions": [
            {"probability": score, "model_version": STATE.version} for score in scores
        ]
    }


if HEALTH_ROUTE not in {route.path for route in app.routes}:
    # Vertex polls whatever health route the Model resource declares. If it is asked for a
    # path the service does not serve, every replica fails its check and the deployment
    # rolls back after the timeout — with no clue in the logs, because nothing was ever
    # called. Aliasing it to readiness is cheap insurance.
    @app.get(HEALTH_ROUTE)
    def vertex_health():
        from service.app import ready

        return ready()
