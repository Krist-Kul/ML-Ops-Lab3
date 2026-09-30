# Lab 3, Task 3 — load test

Measured 2026-09-30, commit `6d36bcd` (the target) onwards. Script `loadtest/k6.js`, k6 v2.3.0.
Machine-readable record: `reports/lab3-load.json`.

**The target was committed in `6d36bcd`, before any of these numbers existed.** That commit
contains the script and no results; this file is the first thing that reports a measurement.
`git log --follow loadtest/k6.js` and `git log --diff-filter=A -- reports/lab3-load.md` show
the order.

---

## The verdict, first

| budget, stated in `6d36bcd` | result | |
|---|---|---|
| service p95 < 50 ms | **30.25 ms** at 1 concurrent caller, **65 ms** at 3 | **missed from 3 callers up** |
| service p99 < 120 ms | 32.03 ms at 1 caller | met to 5 callers |
| error rate < 0.5% | **0.000%** across 79 626 scored requests locally and 2 033 on the endpoint | **met** |
| end-to-end p95 < 200 ms in-region | ~135 ms projected in-region; 288.65 ms measured from Bangkok | met in-region, not from here |
| cold start < 60 s | **42.4 s** on `n1-standard-2`, 21.4 s on `n1-standard-4` | met, with less headroom than expected |

**Breaking point: 3 concurrent callers.** Two callers hold p95 at 41.5–42.3 ms across five
repeats; three cross to 65.0–65.6 ms across three. Nothing errors at any level — the service
degrades by queueing, never by dropping, all the way to 50 callers.

And the number that matters more than any of the above: **on the machine the service is
actually deployed on, the 50 ms target is missed at one caller.** The endpoint's own container
reports p95 = 123.5 ms serving a single request at a time. I set the target from a 15.4 ms
measurement taken on my laptop in Task 1, and production CPU is four to five times slower than
my laptop. The target was honest, committed in advance, and set against the wrong hardware.
That is the finding, not an excuse: a latency target belongs to a machine, and if you have not
measured the machine you will deploy on, you have set a target for your desk.

---

## What was measured, and where

Three subjects, because one number cannot answer both "is my service fast" and "is my
deployment fast":

| subject | shape | what it isolates |
|---|---|---|
| **local container** | the digest-pinned serving image, `--cpus=2 --memory=7500m`, 1 uvicorn worker, artifact pulled from the same `gs://` object the endpoint serves | the service, with the network removed |
| **deployed endpoint, client side** | k6 from Bangkok to `asia-southeast1` over TLS with keep-alive | what a caller here actually experiences |
| **deployed endpoint, container side** | the service's own `latency_ms` structured log line, read back out of Cloud Logging for the same requests | the service on production hardware, with the network removed |

The third is the one that makes this honest. Task 2 measured 451–629 ms round trips from this
laptop to Singapore and I said in `6d36bcd` that the endpoint's own speed could not be read
off a client-side number. The container logs a `latency_ms` per request, so it can: the same
2 033 requests appear on both sides, and the difference between the two is the network plus
Vertex's front door.

```bash
gcloud logging read 'resource.type="aiplatform.googleapis.com/Endpoint"
  labels.deployed_model_id="6116022434387722240"
  jsonPayload.message:"\"path\": \"/v1/predict\""' --limit 2000 \
  --format="value(jsonPayload.message)"
```

Each log line arrives as a **string** in `jsonPayload.message`, not as parsed JSON — Cloud
Logging does not look inside the container's stdout. The timestamp is written when the request
*finishes*, so a run's requests have to be bucketed by `ts − latency_ms` to recover when each
one started. Doing it the obvious way put a 60-request tail from one concurrency level into
the next level's bucket.

### One honest caveat about the local numbers

The host is arm64; the serving image is `linux/amd64` and runs under emulation. Same image,
same bytes as production, wrong instruction set — `itcs355-serve:ef1194c` is
`sha256:4353337add7f2aa754bc44a5cba2585ffbc7d7be5ab8f9f9da3e51651a1c35a6`, which is the digest
`reports/lab3-deploy.json` records as deployed, so this is not a rebuild that resembles
production, it is the artifact serving it. Measured directly inside the container,
`predict_proba` on one row takes **25.0 ms emulated** against **14.7 ms native on this host**
— a **1.70x** emulation tax. So the local figures below are not "what the endpoint does", and
they are not claimed to be. They are a controlled surface for the batch, payload and
concurrency questions, where every comparison is against another number from the same surface,
and the production figures come from the endpoint's own logs.

An arm64 build was not an option: `requirements-serve.txt` is compiled with
`--generate-hashes` inside the amd64 3.11 image, and arm64 wheels have different hashes, so
the build fails by design. That is the hash pinning working, not a defect.

---

## Primary sweep — local container, 2 vCPU, 60 s per level

| | req/s | min | p50 | p95 | p99 | max | errors | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **1 VU** | 37.5 | 14.01 | 26.31 | **30.25** | 32.03 | 56.82 | 0.000% | 2250 |
| **10 VU** | 51.3 | 120.26 | 193.62 | **221.67** | 241.42 | 295.46 | 0.000% | 3083 |
| **50 VU** | 49.3 | 504.95 | 996.53 | **1210.24** | 1302.61 | 1655.06 | 0.000% | 2979 |

Throughput saturates at **~50 req/s** and then stops moving: 51.3 at ten callers, 49.3 at
fifty. Latency from that point is pure queueing — fifty callers at 50 req/s means each request
waits behind 49 others, and 50/49.3 ≈ 1014 ms, which is the p50 to within 2%. One process, one
worker, one request at a time.

The five-fold rise from 10 VU to 50 VU costs nothing in errors. Vertex and uvicorn both queue
rather than shed, so the failure a caller sees is a timeout of their own choosing, never a 503
from here.

### Breaking point, and whether it reproduces

| | req/s | p50 | p95 | p99 | verdict |
|---|---:|---:|---:|---:|---|
| 1 VU | 37.3 | 26.34 | 30.11 | 32.09 | pass |
| **2 VU** | 51.7 | 38.72 | **41.50** | 51.09 | pass |
| **3 VU** | 53.0 | 53.80 | **64.95** | 74.19 | **breach** |
| 4 VU | 47.1 | 84.84 | 98.27 | 111.11 | breach |
| 5 VU | 47.0 | 105.12 | 121.48 | 136.18 | breach |

Five repeats at 2 VU: p95 = 41.50, 41.58, 41.88, 42.29, 41.94. Three at 3 VU: 64.95, 65.56,
65.55. Three at 1 VU: 30.11, 30.37, 30.13. Spread under 2% within each level, and the levels
do not overlap, so **3** is a real boundary and not a coin toss.

It is worth saying what went wrong before those repeats existed. The first pass measured 2 VU
at p95 = 53.1 ms and 3 VU at 77.2 ms — about 25% slower, and it put the breaking point at 2.
Repeating under quiet conditions moved every level down by roughly the same factor. I cannot
attribute the difference to a specific cause, which is exactly why the repeats are in the
table: a single 30-second run was enough to move a graded number by a whole concurrency level,
and one run is not a measurement. The primary sweep and every experiment below were re-taken
in one quiet pass so that the tables can be compared with each other.

---

## Cold start, reported separately

Excluded from every percentile above, as promised in `6d36bcd`: it is paid once per replica at
deploy time, not once per request.

| | uvicorn up → `/ready` 200 | service-reported `load_seconds` | source |
|---|---:|---:|---|
| local container, 2 vCPU, emulated | 6.53 s | 4.95 s | `gs://` artifact |
| **endpoint, `n1-standard-2`** | **42.4 s** | **37.60 s** | Vertex tenant bucket |
| **endpoint, `n1-standard-4`** | **21.4 s** | **19.61 s** | Vertex tenant bucket |
| local, from a local file (Task 1) | 0.83 s | — | `MODEL_PATH` |

Production cold start is **7.6x** the local container's and **45x** the local-file case. The
local-file comparison localises it: essentially all of it is fetching and unpickling the
artifact on a slow CPU, not importing Python.

42.4 s against a 60 s budget is 1.4x headroom, not the comfortable margin I assumed when I
wrote that number down. A replica restart on `n1-standard-2` spends three quarters of a minute
unable to serve. It clears the bar; it clears it by less than it looks.

The platform's readiness probe behaviour is visible in the same logs, and it is the Task 1
design working end to end on a real load balancer:

```
17:23:53.259  Started server process [1]          # port open
17:23:54.800  GET /ready  503  model_status=loading
17:24:04.658  GET /ready  503  model_status=loading
17:24:12.873  model loaded  load_seconds=19.613  selftest_probability=0.5297927687777871
17:24:14.654  GET /ready  200  model_status=ready  # traffic allowed in
```

19 seconds during which `/health` would have answered 200 and `/ready` refused. Had those been
the same route, Vertex would have routed prediction traffic into a container with no model.
Both replicas reported the identical `selftest_probability` of 0.5297927687777871, on two
different machine types — the same bytes scoring the same row.

---

## Where the latency actually is

The local p50 of 26.3 ms is not spread across the request. Timed inside the running container:

| | 1 row | 100 rows |
|---|---:|---:|
| `pd.DataFrame(rows, columns=FEATURES)` | 0.085 ms | 0.113 ms |
| `RandomForestClassifier.predict_proba` | **25.029 ms** | **25.053 ms** |
| HTTP + ASGI + JSON parse + Pydantic + response | 0.71 ms | — |
| sum | 25.82 ms | — |
| measured end to end | 26.31 ms | 28.65 ms |

The HTTP + validation figure is measured, not estimated: `MODE=parse` sends a body the schema
rejects, so the request completes having never reached the model, and a 212-byte rejected
request takes 0.84 ms at 1076 req/s.

**`predict_proba` costs the same for 1 row as for 100.** The model is 200 trees of depth 4; the
cost is dispatching across 200 estimators, and the rows that arrive at the leaves are free by
comparison. Everything else in this report follows from that one fact.

### The 11 ms that buys nothing

The registered model carries `n_jobs=-1`. Measured natively on the same artifact:

| `n_jobs` | 1 row | 100 rows |
|---:|---:|---:|
| −1 | 14.911 ms | 15.455 ms |
| 2 | 14.896 ms | 14.890 ms |
| **1** | **3.624 ms** | **3.592 ms** |

Single-threaded is **4.1x faster**. Joblib's thread-pool dispatch over 200 trees costs about
11 ms per call and returns nothing, because there is no work to parallelise — the trees are
depth 4 and there is one row. `n_jobs=-1` is right for fitting and wrong for serving, and the
same pickle carries the setting from one to the other.

This was **not** changed before measuring, deliberately: the numbers in this report describe
the service as deployed, and editing it mid-measurement would have invalidated every table
above. It is a one-line change at load time in `service/app.py` (`STATE.model.n_jobs = 1`
after unpickling, before the self-test) and it is not free of consequence — it needs a test
asserting the setting survives a reload, and a note that it changes no prediction, only the
schedule. Projected from the component measurement, it would take local p50 from 26.3 ms to
about 15 ms and move the breaking point from 3 callers to roughly 6. It is a projection. It is
the first thing I would do in Task 5, and it costs nothing per hour, which is more than can be
said for the bigger machine below.

---

## Experiment 1 — does a 100-row batch beat 100 single calls?

**Yes, by about 95x, and the advantage does not close at any concurrency tested.**

| | req/s | call p50 | call p95 | per-row p50 | 100-row group p50 | **rows/s** |
|---|---:|---:|---:|---:|---:|---:|
| batch of 100, 1 VU | 35.4 | 26.80 | 30.41 | **0.27** | — | **3537** |
| 100 singles, 1 VU | 36.5 | 27.14 | 29.12 | **27.49** | 2749 ms | **37** |
| batch of 100, 10 VU | 41.7 | 236.41 | 282.87 | **2.36** | — | **4168** |
| 100 singles, 10 VU | 43.9 | 224.92 | 269.22 | **227.21** | 22 721 ms | **44** |

Row throughput: **97.0x** at one caller, **95.0x** at ten. A caller with 100 rows waits
**2749 ms** doing it one at a time and **26.8 ms** in one batch — **103x**.

Note what is *not* different: the per-call latency. One row costs 27.14 ms and one hundred
rows cost 26.80 ms. Batching is not making each request faster; it is amortising a fixed
25 ms `predict_proba` over a hundred rows instead of one.

Which is why **the advantage cannot disappear by adding concurrency.** Concurrency multiplies
the same fixed cost on both sides, so the ratio is preserved — 97.0x becomes 95.0x, a 2%
erosion across a tenfold increase. It would only close if rows per call grew until scoring
finally dominated dispatch, and `BatchRequest` caps a batch at 100 rows, which by the
per-row column is about 1% of the way there. Within the contract the service offers, batching
always wins, and the honest answer to "at what concurrency does it stop winning" is: not at
any concurrency this service can be asked to serve.

If per-row latency mattered more than throughput, the reverse would be true — a single row
gets its answer in 27 ms either way, and waiting to fill a batch of 100 adds queueing delay
the batch cannot pay back. The 95x is a throughput number, not a latency number.

---

## Experiment 2 — payload size, and where serialisation starts to dominate

The schema is `extra='forbid'`, so a single-row request cannot be padded — it is rejected. That
turned out to be useful rather than obstructive, because it splits the question in two.

**(a) Valid inflation.** `/predict/batch`, rows 1 → 100, every row scored.

| rows | body bytes | req/s | p50 | p95 | per-row p50 |
|---:|---:|---:|---:|---:|---:|
| 1 | 214 | 36.0 | 27.39 | 29.50 | 27.392 |
| 5 | 1 029 | 36.0 | 27.43 | 29.39 | 5.486 |
| 10 | 2 049 | 35.3 | 27.44 | 30.04 | 2.744 |
| 25 | 5 108 | 34.9 | 28.08 | 31.34 | 1.123 |
| 50 | 10 205 | 34.6 | 28.14 | 31.77 | 0.563 |
| 100 | 20 399 | 34.0 | **28.65** | 32.13 | 0.286 |

**95x the bytes costs 1.26 ms — 4.6%.** Within everything the contract permits, payload size
is not a latency term.

**(b) Rejected inflation.** One row plus a junk field, rejected at validation, so the body is
parsed and validated and the model is never reached. This is the serialisation cost alone.

| pad | body bytes | req/s | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|---:|
| none | 212 | 1076.4 | 0.84 | 1.14 | 1.43 |
| 1 KB | 1 212 | 1066.9 | 0.84 | 1.13 | 1.40 |
| 10 KB | 10 212 | 924.0 | 0.90 | 1.22 | 1.53 |
| 100 KB | 100 212 | 393.8 | 1.37 | 2.54 | 2.99 |
| 1 MB | 1 000 212 | 74.1 | 4.36 | 5.67 | 6.53 |
| 5 MB | 5 000 212 | 15.7 | 16.84 | 19.14 | 23.92 |
| **8 MB** | 8 000 212 | 9.7 | **29.08** | 32.91 | 38.16 |

Reading the request is free to 10 KB (+0.06 ms over an empty one), noticeable at 100 KB
(+0.53 ms), and linear above 1 MB at roughly 3 to 4 ms per megabyte
(3.12 ms/MB from 1 to 5 MB, 4.08 from 5 to 8).

**Serialisation overtakes scoring at about 8 MB**, where parsing alone costs 29.08 ms against
a fully-scored 100-row batch's 28.65 ms. The largest request the schema accepts is
20 399 bytes. **The crossover is 392x beyond the contract**, so for this service the answer to
"where does serialisation start to dominate" is: nowhere reachable. The 100-row cap put it out
of range, and the cap was chosen as a memory guard, not a latency one.

The other half of that table is a denial-of-service observation rather than a performance one.
A single caller sending 8 MB bodies that are *rejected* pulls the service down to 9.7 req/s —
below its legitimate throughput of ~50 — while consuming no model time at all. Validation
happens after the body is read, so `extra='forbid'` rejects the request but cannot stop the
cost of receiving it. A body-size limit at the ingress, ahead of the application, is the thing
that would; the schema cannot do it.

---

## Experiment 3 — one instance size up: `n1-standard-2` → `n1-standard-4`

Registered version 3, same artifact, same image digest, deployed to the same endpoint on
`n1-standard-4` with 100% traffic, measured, then traffic returned and the deployment removed.
Record: `reports/lab3-instance-n1s4.json`.

### Latency

Client side, from Bangkok:

| | req/s | p50 | p95 | p99 | errors |
|---|---:|---:|---:|---:|---:|
| `n1-standard-2`, 1 VU | 5.3 | 177.59 | 288.65 | 353.31 | 0.000% |
| `n1-standard-4`, 1 VU | 6.9 | 136.41 | 231.99 | 284.62 | 0.000% |
| `n1-standard-2`, 10 VU | 10.3 | 930.02 | 1293.49 | 1544.02 | 0.000% |
| `n1-standard-4`, 10 VU | 17.7 | 548.98 | 717.42 | 829.86 | 0.000% |
| `n1-standard-2`, 50 VU | 9.1 | 5575.12 | 6702.33 | 7299.12 | 0.000% |
| `n1-standard-4`, 50 VU | 16.4 | 3072.97 | 3619.21 | 3935.62 | 0.000% |

Container side, the same requests, the service's own `latency_ms`:

| | n | req/s | p50 | p95 | p99 | min |
|---|---:|---:|---:|---:|---:|---:|
| `n1-standard-2`, 1 VU | 158 | 5.3 | 82.9 | **123.5** | 152.8 | 68.5 |
| `n1-standard-4`, 1 VU | 206 | 6.9 | 50.4 | **70.9** | 82.6 | 48.0 |
| `n1-standard-2`, 10 VU | 313 | 10.4 | 793.5 | 1128.3 | 1370.0 | 233.8 |
| `n1-standard-4`, 10 VU | 532 | 17.7 | 458.7 | 569.5 | 677.1 | 148.1 |
| `n1-standard-2`, 50 VU | 298 | 9.9 | 5111.6 | 5947.3 | 6435.1 | 718.6 |
| `n1-standard-4`, 50 VU | 526 | 17.5 | 2788.8 | 3084.6 | 3257.8 | 199.8 |

Subtracting the two gives the network and Vertex's front door, which at one caller is stable
at **86–95 ms p50** and is not the service's to fix. An in-region caller would see roughly
123.5 + 10 ≈ 135 ms p95 on `n1-standard-2` — inside the 200 ms end-to-end budget, outside the
50 ms service budget.

Doubling the machine bought:

- **p95 at one caller: 123.5 → 70.9 ms, −43%**
- **saturated throughput: 10.4 → 17.7 req/s, +70%**
- **cold start: 37.60 → 19.61 s, −48%** — the largest single improvement in this report
- and it still **misses the 50 ms target** at one caller.

### Cost

| | THB/h | saturated req/s | req/h | **THB per 1000 requests** |
|---|---:|---:|---:|---:|
| `n1-standard-2` | 5.09 | 10.4 | 37 440 | **0.1360** |
| `n1-standard-4` | 10.18 | 17.7 | 63 720 | **0.1598** |

Rates from `src/costs.SERVING_PRICE_TABLE`, verified in Lab 2 and kept separate from the
training table on purpose.

**+100% cost for +70% throughput: 17.5% more expensive per request served.** The bigger machine
buys latency and cold start, and it buys them at a loss on unit economics. That is the whole
answer to the exercise: "one step up" is not a scaling strategy here, because the thing in the
way is one Python process serving one request at a time, and a process does not get wider when
the machine does.

The local control says the same thing from the other side. Same image, same host, `--cpus=2`
against `--cpus=4`:

| | req/s | p50 | p95 |
|---|---:|---:|---:|
| 2 vCPU, 1 VU | 38.0 | 25.80 | 28.38 |
| 4 vCPU, 1 VU | 37.5 | 26.33 | 28.68 |
| 2 vCPU, 2 VU | 52.3 | 38.18 | 40.80 |
| 4 vCPU, 2 VU | 50.9 | 39.88 | 44.03 |
| 2 vCPU, 10 VU | 50.7 | 195.38 | 225.27 |
| 4 vCPU, 10 VU | 50.6 | 195.90 | 231.70 |

**Locally, twice the cores changes nothing** — every pair is within noise, and 10 VU is
identical to three significant figures. On Vertex the same doubling gave +70%. The two are not
in conflict: under emulation the constraint is the instruction translation, not the core count,
so extra cores have nothing to do. It is a reminder that the local surface answers questions
about *ratios within itself* and cannot be asked what hardware to buy.

### What to buy instead

Cheapest first, and the first two cost nothing per hour:

1. **`n_jobs=1` at load time.** 4.1x on the dominant term, 0 THB/h. Measured above as a
   component; unmeasured end to end.
2. **`--workers 2`** on a 2-vCPU instance. The `Dockerfile.serve` comment reasons that two
   workers double resident memory and cold start "for no throughput gain on a 2-vCPU
   instance". The first half is right and **the second half is now contradicted by
   measurement**: throughput is capped by one process, not by the CPU, and 10.4 req/s on two
   real cores is not a CPU limit. Two workers should roughly double it for 0 THB/h, at the
   price of two model copies in RAM and a doubled cold start — which on `n1-standard-2` means
   going from 42 s toward the 60 s probe cliff, so it wants measuring, not assuming.
3. **`min_replicas=2`** — linear throughput, linear cost, no worse unit economics than one,
   and it survives a replica restart. 10.18 THB/h, the same as `n1-standard-4`, for about
   twice the throughput of one `n1-standard-2`, or about four times if the workers are fixed
   first. Both are projections from the ceiling being one process, not measurements.
4. **`e2-standard-2`, 3.58 THB/h** — 30% cheaper than `n1-standard-2` for the same 2 vCPU.
   If the ceiling is one process rather than the CPU, the cheapest 2-vCPU machine should serve
   the same 10.4 req/s for 0.0956 THB per 1000 requests, a 30% saving. **Untested** — the
   number above is arithmetic on a verified price, not a measurement, and it is exactly the
   kind of claim this report is otherwise built to avoid.

`n1-standard-4` is fourth-best at twice the price. It is the right answer only if cold start
is the thing hurting, and then the honest fix is two replicas so a cold start never blocks a
request.

---

## What this cost

| | |
|---|---|
| `n1-standard-4`, replica live 17:23:53Z → 17:32Z | ~9 min × 10.18 THB/h = **~1.5 THB** |
| `n1-standard-2` | already running from Task 2; no Task 3 increment |
| Local container runs, 295 171 requests (79 626 scored, 215 545 rejected on purpose) | 0 THB |
| Endpoint requests, 2 033 predictions | 0 THB — billed per replica-hour, not per call |

The endpoint was left at its Task 2 state: one deployment, `n1-standard-2`, 100% traffic,
5.09 THB/h, still billing. Traffic was moved back with a `PATCH` on `trafficSplit` **before**
undeploying, because undeploying the deployment holding 100% of the traffic would have left the
endpoint with nothing routable. The adapter has no traffic-split method yet — Task 4 needs one.

---

## Reproducing this

```bash
# the container, in the shape the endpoint runs it
docker run -d --name itcs355-load --cpus=2 --memory=7500m -p 8080:8080 \
  -e MODEL_URI="gs://itcs355-6688053/registry/itcs355-6688053/itcs355-register-20260921-014231/model" \
  -e MODEL_VERSION=3 -e CLOUDSDK_CONFIG=/gcloud \
  -e GOOGLE_APPLICATION_CREDENTIALS=/gcloud/application_default_credentials.json \
  -v "$HOME/.config/gcloud":/gcloud:ro itcs355-serve:ef1194c

make loadtest TARGET=http://127.0.0.1:8080/predict        # 1, 10, 50 VUs

# experiments
k6 run -e TARGET=http://127.0.0.1:8080/predict/batch -e MODE=batch   -e ROWS=100 loadtest/k6.js
k6 run -e TARGET=http://127.0.0.1:8080/predict       -e MODE=singles -e ROWS=100 loadtest/k6.js
k6 run -e TARGET=http://127.0.0.1:8080/predict       -e MODE=parse   -e PAD=8000000 loadtest/k6.js

# the deployed endpoint (REST, because invoke() is authenticated gRPC that k6 cannot speak)
k6 run -e FLAVOUR=vertex -e TOKEN="$(gcloud auth print-access-token)" \
  -e TARGET="https://asia-southeast1-aiplatform.googleapis.com/v1/projects/916002887960/locations/asia-southeast1/endpoints/4434156671983419392:predict" \
  loadtest/k6.js

make endpoint-status      # what is attached and what it costs
make teardown             # 5.09 THB/h does not stop on its own
```

---

## Limits of this report

- **Local figures are emulated amd64**, 1.70x slow on the dominant term. Used for ratios within
  themselves, never as production numbers. Production numbers come from the endpoint's own logs.
- **Endpoint runs are 30 s, not 60 s**, and n per level is 158–532 rather than thousands. That
  is a deliberate cost decision at 5.09–15.27 THB/h, and it means the endpoint p99s are the
  weakest numbers here — a p99 over 158 samples is the second-slowest request.
- **The 50 ms target was set against the wrong hardware** and this report does not retarget it.
  Rewriting it now, knowing the answer, is the exact thing `6d36bcd` exists to prevent. The
  right target for `n1-standard-2` is a number for Task 5 to commit before it measures again.
- **`n_jobs=1`, `--workers 2` and `e2-standard-2` are unmeasured end to end.** Flagged as
  projections in the text, and each says what it is projected from.
- **One replica throughout.** Autoscaling was never exercised; `max_replicas=1` on both
  deployments, so every queueing result above is the behaviour of a service that cannot
  scale out, which is the configuration, not a limit of the platform.
