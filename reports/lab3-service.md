# Lab 3, Task 1 — the inference service

What the service is, what it was observed doing, and the decisions that are mine rather
than the scaffolding's. Everything below came from the run at the timestamps quoted;
nothing here is described rather than run.

Model under test: `itcs355-6688053` version **1**, the version Lab 2 registered and
promoted (`reports/lab2-registry.md`). Host: this laptop, single uvicorn worker,
Python 3.13, model artifact in GCS.

---

## Routes

| Route | Purpose | Observed |
|---|---|---|
| `GET /health` | Liveness — the process is up | 200 `{"status":"alive"}` throughout, including while the model was loading and after a load failure |
| `GET /ready` | Readiness — the model is loaded **and has scored a row** | 503 `phase=loading` for the first 7.3 s, then 200 with the resolved version |
| `POST /predict` | One row | 200, probability + `model_version` + `request_id` |
| `POST /predict/batch` | 1–100 rows | 200, probabilities in request order, same version |

```bash
make serve VERSION=1            # or VERSION=production — an alias resolves through the adapter
make serve-local                # a locally trained file; development only, not submittable
make smoke-local                # hits all four routes and prints the bodies
pytest -q tests/test_service.py # 16 tests, the contract and the startup lifecycle
```

---

## Evidence

**The model is loaded once, at startup, from the registry by version.** Startup log line,
2026-09-26T10:52:10Z, reformatted only by line breaks:

```json
{"ts": "2026-09-26T10:52:10.771Z", "level": "INFO", "logger": "service",
 "msg": "model loaded", "request_id": "-", "model_version": "1",
 "source": "registry:itcs355-6688053", "requested_version": "1",
 "artifact_uri": "gs://itcs355-6688053/registry/itcs355-6688053/itcs355-register-20260921-014231/model",
 "aliases": ["default", "production", "staging"],
 "load_seconds": 7.939, "selftest_probability": 0.5297927687777871, "loads": 1}
```

`loads: 1` is a counter on the process, not a comment. `tests/test_service.py::test_model_is_loaded_once_at_startup`
issues five predictions and asserts it is still 1, which is the cheapest available proof
that nothing reloads per request.

**Liveness and readiness genuinely differ.** Two calls, 2.1 s after the process started
and while the artifact was still downloading:

```
GET /health  200  {"status":"alive"}
GET /ready   503  {"status":"not_ready","phase":"loading","reason":"model still loading",
                   "requested_version":"production"}
```

That window is the handout's diagram, and it exists only because the load runs on a
background thread. Blocking startup on the load would produce the same safety with worse
symptoms: the port stays closed, and the platform's probe reads a closed port as a crash,
not as a slow start.

**An alias resolves to a concrete version.** Started with `MODEL_VERSION=production`:

```
GET /ready   200  {"status":"ready","model_version":"1","requested_version":"production",
                   "source":"registry:itcs355-6688053","load_seconds":7.329}
POST /predict 200 {"probability":0.027877785225882974,"model_version":"1","request_id":"alias-check"}
```

**Malformed input is rejected with a message worth reading.**

```json
{"error": "validation_error",
 "detail": [{"field": "load_pct", "message": "Input should be less than or equal to 100",
             "type": "less_than_equal", "received": 250.0}],
 "request_id": "smoke-859000", "model_version": "1"}
```

An unknown field is rejected the same way (`extra_forbidden`), and a 101-row batch is
rejected with `received: {"type":"array","length":101}` rather than the 101 rows echoed
back.

**Every line on stdout parses as JSON.** 13 of 13 lines from the last run, uvicorn's own
lines included. Two libraries had to be told to stop writing to the log stream: MLflow's
import-time advisory and its tqdm artifact-download progress bar
(`MLFLOW_DISABLE_AGENT_HINT`, `MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR`). One unparseable
line is enough for a collector to drop the batch it arrived in, which is a silent failure
of the observability you are paying for.

---

## Decisions

**The registry lookup goes through the adapter, not through `models:/`.** The scaffold's
`mlflow.sklearn.load_model(f"models:/{name}/{version}")` reads MLflow's own registry. The
registered version lives in the Vertex Model Registry, so that call resolves nothing. The
service asks `adapter.get_model(name, version)` for a URI and loads what it is given —
the path `scripts/reload_check.py` already proves in Lab 2. `make portability-audit`
passes: no provider string appears anywhere under `service/`.

**Readiness is earned by scoring, not by a non-`None` object.** At startup the service
scores one row built from the midpoints of `src/data.PLAUSIBLE_RANGES`. An artifact that
unpickles but cannot predict — a moved feature list, an estimator whose internals changed
between scikit-learn minors — is the failure that looks healthy, and this catches it
before the endpoint is declared ready rather than on the first real request.

**Responses carry the resolved version, never the alias.** `production` is a pointer that
Task 4 will move on purpose. A client that recorded `"model_version": "production"`
cannot afterwards say which model produced its number, which is precisely the question a
rollback post-mortem asks.

**`MODEL_VERSION` is configuration, not an image layer.** The image names no version. A
rollback is then a deployment-config change, not a rebuild — if the version were baked in,
Task 4's rollback would go through a docker build, and the detection-to-recovery time I
have to report would include it.

**`MODEL_LOAD_WAIT_S` defaults to 0 in the container and 30 elsewhere.** The container
serves its port immediately, because that is what the platform's probes expect. A local
run and CI block until the model is up, because a test that races a background thread is
worse than no test.

**Bounded echo in validation errors.** Quoting the offending value back is useful for a
scalar and reckless for a list: a 101-row batch would come back as a 101-row error body,
amplified at the sender and into log storage. Scalars come back whole, longer strings
truncate, containers come back described.

**One worker per container, for now.** Each uvicorn worker loads its own copy of the
model, so `--workers 2` doubles resident memory and doubles cold start. Whether it buys
throughput is a Task 3 measurement, not a default.

---

## The serving image

`make serve-image` → `itcs355-serve:22e6b6b`, **287 MB**, non-root (uid 10001), base pinned
to the same multi-arch index digest as the training image, dependencies installed with
`--require-hashes` from an 87-package lock compiled inside `python:3.11-slim` itself.

Run with a model file mounted, the container answers exactly as the host did:

```
GET  /health    200  {"status":"alive"}
GET  /ready     200  {"status":"ready","model_version":"image-smoke","source":"file","load_seconds":0.832}
POST /predict   200  {"probability":0.01074325194062792,"model_version":"image-smoke", ...}
```

Run with `MODEL_VERSION=1` and no credentials, it fails the way it should:

```
GET  /health    200  {"status":"alive"}
GET  /ready     503  {"status":"not_ready","phase":"failed",
                      "reason":"ModuleNotFoundError: No module named 'google.cloud'",
                      "requested_version":"1"}
```

Liveness stays up, readiness names the cause, and no traffic is ever routed there. That
error is also the Task 2 question in concrete form: either the provider SDK enters the
image so the container can resolve the version itself, or the endpoint hands the container
its artifact and the image stays SDK-free. Task 3 measures the cold start of both.

Two lock decisions worth recording. The scaffold pinned `uvicorn==0.34.0`, which cannot be
installed alongside the versions `requirements.txt` already resolved — 0.34 predates the
starlette line `fastapi==0.141.1` requires, and pip-compile refuses the combination. The
serving lock therefore takes the repo's existing `uvicorn==0.52.4`: two locks disagreeing
about a shared dependency is how an image passes CI and fails on the endpoint. And the
compile runs against `constraints-lab1.txt` (`scripts/derive_constraints.py`, derived from
`requirements.txt`), which is what makes it finish — unconstrained, `--generate-hashes`
explores every candidate version of numpy, pandas, scipy and scikit-learn and downloads
every wheel of each to hash it: over 7 GB and still running after forty minutes here,
against about ten minutes with the constraint.

---

## Numbers to carry into Task 3

Local, single worker, sequential client — context for the real measurement, not a
substitute for it.

| Figure | Value |
|---|---|
| Cold start (process start → ready) | 7.3 s, 7.4 s, 7.9 s across three runs |
| `/predict`, 200 sequential requests after 20 warm-up | p50 14.3 ms, p95 15.4 ms, p99 17.8 ms, max 29.9 ms |
| Serving image size | 287 MB |
| Load from a local file, in the container | 0.83 s — the 7.3 s above is mostly the object-storage download |

The cold start is almost entirely artifact download plus unpickle, and it is the figure
that will dominate p99 if the endpoint scales to zero. The tight p50-to-p99 spread is what
loading once looks like; a per-request load shows up here as a p99 several times p50.

---

## Not done in Task 1

The serving image's provider-facing details are Task 2 and are marked `TODO(Lab 3, Task 2)`
in `service/Dockerfile.serve`: the route mapping Vertex expects, and whether the endpoint
hands the container its artifact (`AIP_STORAGE_URI`) or the container resolves the version
itself through the adapter. The second choice decides whether a provider SDK has to enter
the image at all, and the cold-start difference between them belongs in Task 3.
