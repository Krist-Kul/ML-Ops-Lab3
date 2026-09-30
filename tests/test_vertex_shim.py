"""Lab 3 Task 2 — the provider shim's request envelope.

The endpoint is billed by the hour, so the translation is tested here rather than
discovered by deploying. These are the exact shapes Vertex sends and expects: a wrong
envelope shows up in production as a 200 the platform then rejects, or as predictions
silently out of order.
"""
from __future__ import annotations

import importlib
import os
from contextlib import contextmanager

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_service import VALID, _local_model_path  # noqa: E402


@contextmanager
def shim(**env: str):
    saved = dict(os.environ)
    os.environ["MODEL_PATH"] = str(_local_model_path())
    os.environ["MODEL_VERSION"] = "test-7"
    os.environ.pop("MODEL_REGISTRY_NAME", None)
    os.environ.pop("MODEL_URI", None)
    os.environ.update(env)
    try:
        import cloudlayer.gcp_serving as module
        import service.app as service_app

        importlib.reload(service_app)
        yield importlib.reload(module)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_predict_route_speaks_vertex(tmp_path):
    with shim() as module, TestClient(module.app) as c:
        r = c.post("/v1/predict", json={"instances": [VALID, {**VALID, "temp_c": 92.0}]})
        assert r.status_code == 200
        body = r.json()
        assert list(body) == ["predictions"]
        assert len(body["predictions"]) == 2
        assert all(0.0 <= p["probability"] <= 1.0 for p in body["predictions"])
        assert {p["model_version"] for p in body["predictions"]} == {"test-7"}


def test_predictions_keep_instance_order():
    """Vertex matches predictions to instances by position and nothing else."""
    with shim() as module, TestClient(module.app) as c:
        rows = [VALID, {**VALID, "temp_c": 130.0, "hours_since_service": 19000.0}]
        envelope = c.post("/v1/predict", json={"instances": rows}).json()["predictions"]
        singles = [c.post("/predict", json=row).json()["probability"] for row in rows]
        assert [p["probability"] for p in envelope] == pytest.approx(singles, abs=1e-9)


def test_a_bad_instance_is_still_a_422():
    with shim() as module, TestClient(module.app) as c:
        bad = c.post("/v1/predict", json={"instances": [{**VALID, "load_pct": 250.0}]})
        assert bad.status_code == 422
        assert bad.json()["detail"][0]["field"] == "instances.0.load_pct"
        assert c.post("/v1/predict", json={"instances": []}).status_code == 422


def test_the_route_comes_from_the_platform():
    """Vertex declares the routes on the Model resource and passes them in AIP_*. A route
    hard-coded here is a deployment whose health checks call a path nobody serves."""
    with shim(AIP_PREDICT_ROUTE="/custom/predict") as module, TestClient(module.app) as c:
        assert c.post("/custom/predict", json={"instances": [VALID]}).status_code == 200
        assert c.post("/v1/predict", json={"instances": [VALID]}).status_code == 404


def test_storage_uri_becomes_the_neutral_model_uri():
    """The only place a provider's variable name is allowed to appear."""
    with shim(AIP_STORAGE_URI="file:///tmp/artifact", MODEL_PATH=""):
        assert os.environ["MODEL_URI"] == "file:///tmp/artifact"


def test_neutral_routes_survive_the_shim():
    with shim() as module, TestClient(module.app) as c:
        assert c.get("/health").json()["status"] == "alive"
        assert c.get("/ready").status_code == 200
        assert c.post("/predict", json=VALID).status_code == 200


def test_the_declared_entrypoint_and_routes_actually_exist():
    """What the Model resource declares is what Vertex runs and polls. A typo here is not
    a stack trace, it is a deployment that fails its health checks for ten minutes and then
    rolls back with nothing in the logs, because nothing was ever called."""
    import importlib

    from cloudlayer.gcp import GcpAdapter

    spec = GcpAdapter(cfg=None).serving_spec(env={"MODEL_VERSION": "1"})
    module_path, _, attribute = spec["args"][0].partition(":")
    entrypoint = importlib.import_module(module_path)
    assert hasattr(entrypoint, attribute)

    with shim() as module:
        paths = {route.path for route in module.app.routes}
        assert spec["health_route"] in paths
        assert spec["predict_route"] in paths
    assert spec["port"] == 8080
    assert spec["env"]["MODEL_VERSION"] == "1"
