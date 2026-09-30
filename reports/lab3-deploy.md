# Lab 3, Task 2 — the deployed endpoint

`itcs355-6688053-endpoint` ·
`projects/916002887960/locations/asia-southeast1/endpoints/4434156671983419392` ·
labels `course=itcs355 student=itcs355-6688053 lab=3`

| | |
|---|---|
| Serving registry version | **3** (alias `serving`) |
| Model bytes | version **1** — the same artifact Lab 2 registered and promoted, not a retrain |
| Deployed model id | `6116022434387722240` |
| Serving image | `itcs355-serve@sha256:d46bc66b…` (digest-pinned, built from `service/Dockerfile.serve`) |
| Machine | `n1-standard-2`, 1–1 replicas, 100% traffic |
| Rate | **5.09 THB/h = 122 THB/day**, called or not |

```bash
make deploy              # build, push, register a serving version, deploy it
make smoke               # three known payloads, checked against the registry
make endpoint-status     # what is attached, its traffic share, what it costs
make teardown            # delete it. The budget is 150 THB and the day is 122
```

---

## Why a new registered version exists

A Vertex version carries the container that serves it. Lab 2's version 1 names the
*training* image, because Vertex refuses a version with no container at all and no serving
image existed yet — deploying version 1 would have started `python -m src.train` behind an
HTTP endpoint. So `scripts/deploy.py` registers a version whose `artifact_uri` is the same
object — the same model bytes, byte for byte, not a retrain — and whose container is the
serving image with Vertex's routes declared on it.

The eight lineage fields are copied onto the new version unchanged. They describe how the
model bytes were produced, and putting a different web server in front of them did not
change the commit, the data hash, the training job, or the metrics. The serving image is
recorded separately, in the container spec and in `reports/lab3-deploy.json`.

This is also why two version numbers appear in every response: `model_version: "1"` in the
body is which model scored the row, and Vertex's own `model_version_id: "3"` is which
deployed version the traffic split chose. Task 4 needs both.

## Where the provider-shaped parts live

The handout's rule is to absorb the difference in `cloudlayer/`, and it took three pieces:

1. **`cloudlayer/gcp_serving.py`** — the shim. Vertex POSTs `{"instances": [...]}` and
   requires `{"predictions": [...]}`; the neutral service knows nothing about that. The
   shim imports the neutral app, adds the envelope route, and translates
   `AIP_STORAGE_URI` into the neutral `MODEL_URI`. It is the only file in the repository
   that names a Vertex environment variable.
2. **`adapter.serving_spec()`** — a new method on the seam, alongside Lab 2's `get_model`.
   It returns the routes, the port, and the command Vertex should run. Without it, a
   provider's route names would have to sit in `scripts/`, which is the leak the seam
   exists to prevent. The image's own `CMD` still runs the neutral app; the Model resource
   asks for the shim instead. Same image, same bytes, provider-shaped entrypoint.
3. **`google-cloud-storage` in the serving image.** SageMaker unpacks the model to
   `/opt/ml/model` and Azure ML sets `AZUREML_MODEL_DIR` — both hand the container a local
   directory, which needs no SDK. Vertex hands it a `gs://` URI, so reading that URI needs
   a storage client. One dependency keeps one image deployable on all three; `MODEL_URI`
   takes a local path just as happily.

**Health is readiness, not liveness.** The Model resource declares `health_route: /ready`.
Pointing Vertex at `/health` would mean traffic arrives as soon as the process is up and
fails until the model finishes loading — the exact failure the two routes exist to
prevent. In the logs, Vertex polls that route about every 10 seconds, 360 times an hour,
for the entire life of the deployment.

## Evidence

**`make smoke`, 2026-09-30, against the live endpoint.** Three payloads chosen to fail
differently, each also scored locally from the registry, so the check is "this endpoint
serves the model we registered" rather than "something answered":

```
routine          p=0.027878  model_version=1  vertex_version=3  451 ms  == local
hot-and-overdue  p=0.723253  model_version=1  vertex_version=3  562 ms  == local
upper-bound      p=0.727417  model_version=1  vertex_version=3  506 ms  == local
reject           refused: 400 {"error":"validation_error","detail":[{"field":
                 "instances.0.load_pct","message":"Input should be less than or equal
                 to 100","type":"less_than_equal", ...
```

All three match the locally loaded version 1 to the last decimal place. The fourth call is
the one worth the most: an out-of-range row is **refused in the deployment**, not scored.
A validation layer that passes in CI and disappears behind the endpoint is worse than
none, because the 422s in CI said it was there.

Those latencies are a laptop in Bangkok calling Singapore, including the internet. The
endpoint's own latency is Task 3's measurement, not this one.

**Deploy times.** 23.4 min the first time — endpoint creation plus the first replica
pulling a 287 MB image. 8.0 min for the second deployment to the same endpoint. The
difference matters for Task 4: a rollback that has to deploy is a twenty-minute rollback,
and a rollback that only moves traffic is a one-call rollback.

## Two things the deployment taught that local testing could not

**Every log line was an ERROR.** The first deployment's logs arrived in Cloud Logging with
severity `ERROR` — all of them, including the readiness probe's 200s. The handler wrote to
stderr, which is the Python default, and the platform files a prediction container's
stderr as errors. A healthy endpoint was reporting some 360 errors an hour from its own
health checks, and any alert on error rate would have been worthless before Lab 4 started
writing one. Fixed by logging to stdout and emitting a `severity` field; after
redeploying, the same lines arrive as `INFO`, and the rejected row arrives as `WARNING`.

Worth recording for Lab 4: Cloud Logging does **not** parse the JSON. Each line arrives as
a string inside `jsonPayload.message`, so the entry's severity comes from the stream, not
from the field. A log-based metric on the request lines will need to extract from that
string rather than filter on a parsed field.

**A deployment at 0% traffic still bills.** After the second deploy, `make endpoint-status`:

```
deployed_model_id      version   traffic instance         replicas     THB/h
1504336415960334336    2              0% n1-standard-2    1-1           5.09
6116022434387722240    3            100% n1-standard-2    1-1           5.09

10.18 THB/h = 244 THB/day, traffic or no traffic.
```

Moving traffic to zero does not release the machines; Vertex keeps them ready to take
traffic back. `adapter.undeploy()` is the call that stops the meter, and it is separate
from the traffic split on purpose — which is exactly the mistake waiting at the end of
Task 4, where a rolled-back canary sits at 0% and quietly costs the same as production.

## Cost

Verified 2026-09-30 against the Cloud Billing Catalog API (service `C7E2-9256-1C43`,
asia-southeast1, 33.355 THB/USD), now in `src/costs.py` as `SERVING_PRICE_TABLE`:

| | vCPU-hour | GiB-hour | `n1-standard-2` | `e2-standard-2` |
|---|---|---|---|---|
| Prediction SKU | 0.045000 | 0.006000 | | |
| Management fee | 0.005870 | 0.000783 | | |
| **Total** | **0.050870** | **0.006783** | **0.152613 USD/h = 5.09 THB/h** | **0.107458 USD/h = 3.58 THB/h** |

Two things fall out of that table. A **prediction** hour is not a **training** hour for the
same machine: the prediction core SKU is 0.045000 against the 0.038999 Compute Engine core
the Lab 2 training table is built on, so serving costs about 13% more per vCPU-hour than
training on the same shape. And E2 is 30% cheaper than N1 for a comparable machine, which
Task 3's instance-size comparison should measure rather than assume.

Against the 150 THB budget with 0.51 THB spent through Lab 2: this endpoint costs the
remaining budget in about **29 hours**. That is the whole argument for `make teardown`.

## Deliverables

- [x] `deploy()` and `invoke()` implemented in `cloudlayer/gcp.py`, plus `serving_spec()`,
      `list_deployments()`, `undeploy()` and `teardown()` on the seam
- [x] Endpoint live, serving registry version 3 with version 1's bytes
- [x] `make smoke` — three known payloads, all matching the registry, plus a rejected row
- [x] Route mapping in `cloudlayer/`, not in `service/app.py` and not in the Dockerfile
- [ ] `make teardown` — **not yet run; the endpoint is still up for Task 3's load test**
