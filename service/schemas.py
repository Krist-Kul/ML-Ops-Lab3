"""Request and response contracts.

Validation lives here so a malformed request is rejected with 422 and a useful message,
rather than reaching the model and producing a confident number from nonsense.

The bounds mirror src/data.PLAUSIBLE_RANGES. Keep them in step: when Lab 4 adds a data
contract test, the same bounds are what CI asserts against. `tests/test_service.py`
asserts the two agree, so a range that moves in one place fails the build rather than
letting the service accept a row the training data never contained.

Every response carries `model_version` and `request_id`. The version is the resolved
registry version, never the alias that was asked for: an alias moves, and a client that
recorded "production" cannot afterwards say which model produced its number.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

EXAMPLE_ROW = {
    "temp_c": 78.4,
    "vibration_mm_s": 3.1,
    "pressure_kpa": 315.2,
    "hours_since_service": 4200.0,
    "load_pct": 68.0,
    "ambient_humidity": 55.0,
}


class PredictRequest(BaseModel):
    temp_c: float = Field(..., ge=-10, le=140)
    vibration_mm_s: float = Field(..., ge=0, le=60)
    pressure_kpa: float = Field(..., ge=0, le=600)
    hours_since_service: float = Field(..., ge=0, le=20000)
    load_pct: float = Field(..., ge=0, le=100)
    ambient_humidity: float = Field(..., ge=0, le=100)

    # extra='forbid'. A silently ignored field is how a caller ends up sending a feature
    # you never read while believing it matters.
    model_config = {"extra": "forbid", "json_schema_extra": {"example": EXAMPLE_ROW}}


class PredictResponse(BaseModel):
    probability: float = Field(..., ge=0.0, le=1.0)
    model_version: str
    request_id: str


class BatchRequest(BaseModel):
    # The cap is a contract, not a guess: 100 rows is what the handout asks the service to
    # accept, and an unbounded batch is a request that can exhaust memory from outside.
    rows: list[PredictRequest] = Field(..., min_length=1, max_length=100)

    model_config = {"json_schema_extra": {"example": {"rows": [EXAMPLE_ROW]}}}


class BatchResponse(BaseModel):
    probabilities: list[float]
    model_version: str
    request_id: str


class ErrorResponse(BaseModel):
    """The shape of every non-2xx body the service produces."""

    error: str
    detail: Any
    request_id: str
    model_version: str
