# Lab 3, Task 4 — canary, blind detection, rollback

Run 2026-09-30/10-01. Detector and thresholds committed in `3857ccf`, **before** the canary
existed. Evidence: `reports/lab3-canary.json`, `reports/lab3-canary-detect.json`, and the raw
streams `reports/lab3-canary-{blind,attested}-{reference,watch,after_rollback}.jsonl`.

> **Read the cost section before anything else.** This run cost about **194 THB** instead of
> about 10, because an unbounded network call hung for 19 hours while two deployments billed.
> The handout lists that failure mode; I hit it. It is written up in full below, not softened.

---

## The five lines the handout asks for

1. **What metric revealed it:** `tail_rate` and `alert_rate` together — the share of scores
   above the reference's own p99, and the share above the product's 0.5 alert threshold, each
   tested with an exact one-sided binomial against the pre-canary reference. **Not PSI and not
   KS**: on the pooled 90/10 stream PSI read 0.0163 against a 0.10 threshold — 6.1x below
   firing, while the canary's own output sat 14.4x above it — and KS read 0.0589, 1.7x below.
   Both textbook drift statistics stayed silent through the whole incident.
2. **How long detection took:** **70.5 s and 800 requests** from the first canary-attested
   response — of which only **81** were actually the canary's. First fire 19:33:04.969Z. It
   was never *confirmed*, because `CONFIRMATIONS=2` needed a fifth window and the driver hung
   before it ran; that is the cost incident, not a property of the detector.
3. **What would have made it faster:** labelling the metric stream by the version the platform
   already attests on every response. The canary alone needs **20 requests** to cross p<0.01
   where the pooled stream needs **170**, and PSI on it is 1.4388 rather than 0.0163. After
   that, in order: more traffic (detection is request-bound, not time-bound — the same 800
   requests take 16 s at the ~50 req/s Task 3 measured locally, against the 70.5 s they took
   here), a larger canary share, and a smaller window **but not
   below 150**, because at 100 the detector false-fires on canary-free traffic.
4. **What would have happened at 50/50:** `tail_rate` crosses in about **20 requests** and
   `alert_rate` in about **30**, against 170 and 320 at 90/10 — roughly **8–10x faster** — and
   PSI and KS would both have fired instead of neither. The price is the blast radius: half of
   every caller's traffic served by the worse model instead of a tenth, so **5x the bad
   answers for ~9x the detection speed**. For this model that trade is worth taking, because a
   wrong maintenance alert costs an unnecessary inspection rather than a missed failure; for a
   model on the other side of that asymmetry it is not.
5. **The honest fifth line:** the detector found it, and the run still failed. Detection took
   70 seconds; the rollback took 37 seconds once I ran it; the canary stayed up for 19 hours
   because nothing was watching the watcher. The expensive failure was not the model and not
   the statistics — it was an `invoke()` with no deadline inside a loop that owned a billed
   resource.

---

## The canary: worse by a small margin, and the margin is the point

Lab 2's own hyperparameter study already contained the right candidate. `trial-01` differs
from the production model (`trial-00`) in **exactly one hyperparameter**:

| | production (v1 bytes, trial-00) | canary (v4 bytes, trial-01) |
|---|---|---|
| `n_estimators` | 200 | 200 |
| `max_depth` | 4 | 4 |
| `min_samples_leaf` | 5 | 5 |
| `max_features` | sqrt | sqrt |
| **`class_weight`** | **None** | **balanced_subsample** |
| seed | 20260101 | 20260101 |
| val ROC AUC | 0.8405 | **0.8392** (−0.0013) |
| test ROC AUC | 0.8543 | **0.8515** (−0.0028) |

A test AUC drop of 0.0028. Lab 2 measured seed-to-seed variance larger than that, so **no
offline gate keyed on AUC would have stopped this model**, and that is exactly why it is the
right canary. It is not broken. It ranks almost as well as production. What it does is
reweight the positive class during fitting, which leaves the ranking nearly intact and moves
the calibration a long way — and calibration is what a threshold-based product consumes.

Registered as bytes version **4**, then serving version **5** through the same
`scripts/deploy.py` path production used, with **the same serving image digest**
`sha256:4353337add7f2aa754bc44a5cba2585ffbc7d7be5ab8f9f9da3e51651a1c35a6`. Same container,
same routes, same machine type, same input rows in the same order. The only variable in the
experiment is the model bytes.

One trade-off stated rather than hidden: the canary was registered with `--from-study`, so its
lineage carries the study's `<sha>-dirty` commit instead of a clean retrain's. Version 1 was
registered the honest way, through a managed job from a clean tree, because it was going to
production. This one was built to be rolled back and deleted inside the hour, and a managed
retrain would have added ~15 minutes to a run that was already billing 10.18 THB/h. A canary
intended to survive its own promotion should be registered the way version 1 was.

---

## What normal looked like, measured before the canary existed

2000 requests, production the only thing attached, 0 errors.

| | |
|---|---|
| mean predicted probability | 0.1195 |
| alert rate at 0.5 | 0.0235 |
| p99 of the score distribution | 0.5525 |
| tail rate above that p99 | 0.0100 (1% by construction) |
| latency p95 | 1595 ms |

The latency number is large because 12 concurrent callers against one `n1-standard-2` replica
is past the ~10 req/s ceiling Task 3 measured, so this reference is a *saturated* one. It is
the honest baseline for this run and it is also why the latency signal was never going to be
useful here — see below.

---

## Traffic actually split 90/10

`split_for_new_deployment` put the canary at 10 and production at 90; the platform's own
attestation of which deployment answered, over 954 watch requests:

| | requests | share |
|---|---:|---:|
| production `6116022434387722240` (v3) | 858 | 89.94% |
| canary `5453782182931726336` (v5) | 96 | **10.06%** |

Per 200-request window the canary got 19, 17, 26, 19 and 15. That spread is the reason
detection took 800 requests rather than the 170 the pooled arithmetic predicts: the signal is
carried by about twenty samples per window, and twenty samples is a noisy thing to run a test
on even when the effect is enormous.

---

## The two models, measured side by side on the same hardware and the same inputs

This table is **not** what the detector saw. It is the ground truth, recovered afterwards by
joining the blind stream to the attested one, and it is what makes the dilution argument
checkable.

| stream | n | mean | alert @ 0.5 | tail > p99 |
|---|---:|---:|---:|---:|
| production, reference window | 2000 | 0.1195 | 0.0235 | 0.0100 |
| production, during the canary | 858 | 0.1181 | 0.0198 | 0.0082 |
| **canary** | **96** | **0.3425** | **0.2500** | **0.2500** |
| production, after the rollback | 300 | 0.1214 | 0.0333 | 0.0167 |

**Canary against production: 12.6x the alert rate, 30.6x the tail rate, 2.90x the mean score
— for 0.0028 of test AUC.** Production's own numbers barely move across the whole run, which
is the control that matters: the shift is the canary, not the day.

### The central finding: dilution destroys the drift statistics

| statistic | canary alone vs reference | pooled 90/10 vs reference | threshold |
|---|---:|---:|---:|
| **PSI** | **1.4388** | **0.0163** | 0.10 |
| **KS** | **0.5171** | **0.0589** | 0.10 |

PSI on the canary's own output is **14.4x over** the conventional threshold — this is not a
subtle model, it is a catastrophically miscalibrated one. Diluted to a tenth of the traffic it
reads **6.1x under** the same threshold. A 10x reduction in share produced an **88x**
reduction in PSI, because PSI is superlinear in the shift: it integrates a log-ratio over
bins, and moving a tenth of the mass barely bends any single bin's proportion.

So the statistic every drift tutorial reaches for is the wrong tool for a canary, and it fails
in the most dangerous direction — quietly, with a reassuring number. `monitoring/drift.py`
inherited that 0.10 from credit scoring, and `monitoring/canary.py` kept it **unchanged on
purpose** so this run could measure what it does rather than tune it until it worked. Two
tests in `tests/test_canary.py` pin the result, including the 50/50 case where PSI does fire,
so the claim stays "PSI is insensitive at 10%" and never drifts into "PSI is useless".

What works instead is a **rate of a rare event**. A mixture adds a roughly fixed amount of
mass to a small denominator, so the rarer the event the larger the multiple: diluting the
canary 10x took its tail rate from 25% to a pooled 3.4%, which is still 3.4x the reference's
1%. Rates survive dilution; distribution-shape statistics do not.

---

## Detection, as it happened

Blind. `monitoring/canary.py`'s `evaluate` has no parameter for a version, a deployment id or
a traffic split — a test asserts that by inspecting its signature — and `load_stream` raises
if the file it is handed contains a version field. The attested stream lives in a different
file that the detector cannot read.

Committed window of 200 requests, against the committed thresholds:

| window | alert hits | p | tail hits | p | fired |
|---|---:|---:|---:|---:|---|
| 1–200 | 9 / 200 | 4.8e-2 | 6 / 200 | 1.6e-2 | — |
| 201–400 | 7 / 200 | 1.9e-1 | 5 / 200 | 5.2e-2 | — |
| 401–600 | 6 / 200 | 3.3e-1 | 4 / 200 | 1.4e-1 | — |
| **601–800** | **14 / 200** | **3.1e-4** | **12 / 200** | **1.1e-6** | **`alert_rate`, `tail_rate`** |

```
canary created (platform)       2026-09-30T19:24:38.088Z
watch traffic begins            2026-09-30T19:31:54.463Z
first canary-attested response  2026-09-30T19:31:54.463Z   (already warm, attached 7 min earlier)
FIRST FIRE                      2026-09-30T19:33:04.969Z   alert_rate + tail_rate
```

**70.5 s. 800 requests. 81 of them the canary's. 11.3 req/s.**

Detection is timed from the first canary-attested response, not from the attach, because a
replica spends its first ~20–40 s loading the model and the platform routes nothing to it
until `/ready` passes — timing from the attach would credit the detector with finding
something not yet being served. Both origins are in the JSON; from the attach it is 506.9 s,
almost all of it the gap between deploying and starting to drive traffic.

**The incident was never declared.** `CONFIRMATIONS=2` requires a second consecutive firing
window and the fifth window never ran: the driver hung. So the recorded outcome is a
first-fire with no confirmation, and the thing that stopped the rollback from being automatic
was the hang, not the statistics. I am reporting it that way rather than lowering
`CONFIRMATIONS` after the fact to make the run look clean.

### Negative controls, because a detector that only ever fires proves nothing

| traffic | windows | fired |
|---|---:|---|
| 300 requests after the rollback, same reference | 1 | **none** |
| production's other 1000 reference requests, held out against its first 1000 | 5 | **none** |

The detector agrees the incident ended, and it does not fire on canary-free traffic it has
never seen. That second row is the one that makes the first fire meaningful.

### Window size, post hoc and labelled as such

The window is a CLI parameter, not a committed threshold, so varying it after the fact is
legitimate — but only as sensitivity analysis, and only next to a false-alarm count. Without
that second column the obvious conclusion is wrong.

| window | first fire | declared | fired by | false alarms on canary-free traffic |
|---|---|---|---|---|
| 100 | req 100 | **req 200** | `ks` | **`ks` x2, `mean_score` x1** |
| 150 | req 150 | none | `alert_rate`, `tail_rate`, `ks`, `mean_score` | none |
| **200** (committed) | **req 800** | none | `alert_rate`, `tail_rate` | none |
| 250 | req 250 | none | `tail_rate` | none |
| 300 | req 300 | none | `tail_rate`, `mean_score` | none |
| 400 | req 400 | **req 800** | `alert_rate`, `tail_rate` | none |

Window 100 looks four times faster and is not: it declares on `ks`, and `ks` at n=100 against
a 2000-sample reference false-fires twice on traffic containing no canary at all. That is
noise with a good outcome, which is the worst kind of result to keep. `MIN_WINDOW = 100` was
committed as a floor for a different reason — warm-up — and this run says the floor should
have been 150 and should have been justified by a false-alarm count rather than by warm-up
alone. Windows of 150–400 all fire on real signal and never on the control; the committed 200
is inside that band, and it was the unluckiest member of it.

### The signal that could not work here, and why that is worth saying

`latency_p95` never fired and never could have. It is one-sided on the ratio, and attaching a
canary **adds a replica**, so aggregate capacity went up and latency went *down* against a
reference measured while one replica was saturated. A latency-based canary monitor is
confounded by the canary's own capacity — on this endpoint it would have had to get much worse
than the reference while also being faster for the pool. `mean_score` fired only at some
window sizes and false-fired at 100: a mean over a skewed distribution is exactly the blunt
instrument its comment in the source says it is. Both are in the detector because a canary can
be worse by being slow rather than by being wrong, and knowing which of your six signals is
useless on a given endpoint is worth the six.

---

## Rollback, with the platform's own evidence that traffic moved

```
14:31:25.773Z  before          canary 10%, production 90%      10.18 THB/h
14:31:25.773Z  split_traffic   requested {production: 100}  ->  returned {production: 100}
14:31:25-59Z   300 probes      attested: production 300, canary 0
14:31:59.302Z  at 0% traffic   canary 0%, production 100%      10.18 THB/h   <-- still billing
14:32:02.408Z  undeploy        production 100%                  5.09 THB/h
14:3x          make teardown   nothing attached                  0.00 THB/h   (verified)
```

**300 of 300 probes answered by production, 0 by the canary** — and that is the platform's
attestation of which deployment served each request, read off `deployed_model_id` in the
predict response, not the split I asked for and not the container's own claim about its
version. A container can report any version it likes; this cannot. Re-running the blind
detector on those 300 requests fires nothing, which is the same conclusion from the other
direction.

The 36.6 seconds between moving traffic and releasing machines is deliberate and is the line the
bill notices: **at 0% traffic the canary still cost 5.09 THB/h.** Vertex keeps the replicas
ready to take traffic back. A rollback that stops at the traffic split has stopped the damage
and not the spending, which is how a rolled-back canary quietly bills for a weekend. It is two
calls, in that order, and `undeploy` is the one that matters to the invoice.

`split_traffic()` did not exist before this task — Task 3 had to move traffic with a
hand-written `PATCH`. It is now on the adapter seam, and `deploy()` had a real bug that only a
canary could find: it sent the new deployment's share alone and claimed the others kept the
rest, which the provider rejects outright. Both are in `19bdf5a` with the arithmetic in
`src/traffic.py` and 17 tests.

---

## What this cost, and the bug that caused it

| | |
|---|---|
| Canary attached | 2026-09-30 19:24:38Z |
| Traffic rolled back | 2026-10-01 14:31:25Z |
| **Elapsed with two deployments attached** | **19.11 h at 10.18 THB/h ≈ 194.5 THB** |
| Intended | ~1 h ≈ 10 THB |
| Overrun | **~184 THB**, which exceeded the remaining budget |

**Cause, verified.** `invoke()` had no deadline. The host slept partway through the watch, the
gRPC connections died under it, and a `predict` call neither returned nor raised. The driver
process stayed alive the whole time — 4.8 seconds of CPU across 19 hours — holding a canary up
at 10.18 THB/h. The watch loop owned a billed resource and blocked forever inside it.

The handout's common-failure table has this row: *"Cost far above estimate — the endpoint ran
overnight while you wrote the report."* It is there because it is the normal way to lose the
money, and knowing that in advance did not save me, because I was watching for the thing I had
planned (a canary left attached) rather than for the thing that actually happened (a client
call with no timeout).

**Fixed, both of them, in the same commit as this report:**

- `invoke()` takes `timeout_s`, default 30 s, passed to the predict call, and declared on the
  base adapter with the reason in its docstring. An unbounded network call inside a loop that
  owns a billed resource is a cost bug, not a reliability nicety.
- The blind and attested streams are now one file per phase. The first run appended every
  phase to one file, so the rollback's 300 recovered-traffic probes landed immediately after
  the watch's requests with nothing in the blind stream to separate them — and the detector
  read the recovered traffic as two more windows of the incident. A `phase` field inside the
  blind stream would have fixed the symptom while handing the detector a hint about when the
  canary arrived, which is the one thing it must not know. Separate files leak nothing.
- Both streams flush per line. The hung run was killed and lost 24 buffered rows from one
  stream but not the other, which broke the index alignment the two files depend on for the
  ground-truth join. Evidence that only survives a clean exit is not evidence; the 954
  aligned pairs are what this report uses, not the 978 blind rows.

**What would have actually prevented the 194 THB**, in the order I would add it:

1. The deadline above. It is three characters of API surface and it is the whole bug.
2. A hard wall-clock budget in the driver: the canary phases know the hourly rate, so the
   loop should abort and roll back on its own after a stated spend, not after a stated number
   of requests. Requests are the thing that does not happen when the process hangs.
3. A heartbeat the canary's own liveness depends on — if the driver stops reporting, the
   canary comes down. The rollback is 36.6 seconds and fully scripted; nothing about it needed
   me present.
4. A budget alert at the provider, which is the only one of the four that still works when
   every assumption in my own code is wrong.

---

## Reproducing this

```bash
make canary-reference                              # normal, before any canary exists
python scripts/register_model.py --from-study --trial trial-01 --promote "" \
  --report reports/lab3-canary-model.md --record reports/lab3-canary-model.json
python scripts/deploy.py --serving-version 5 --source-version 4 --traffic 10 --promote "" \
  --record reports/lab3-canary-deploy.json
make canary-deploy ADOPT=1                         # record what is attached
make canary-watch                                  # blind detection, timed
make canary-rollback                               # traffic first, then machines
make teardown && python scripts/teardown_verify.py --lab 3

# the detector, offline, on the committed streams
python -m monitoring.canary \
  --reference reports/lab3-canary-blind-reference.jsonl \
  --current   reports/lab3-canary-blind-watch.jsonl --window 200
```

## Limits of this report

- **The incident was never confirmed**, only first-fired. The fifth window did not run. The
  70.5 s / 800 requests figure is a first-fire time; a confirmed declaration would have been
  one window later, about 90 s and 1000 requests, had the process survived.
- **96 canary responses** is the whole sample for the canary's own rates. Its 0.2500 alert
  rate and 0.2500 tail rate are 24 hits out of 96 apiece, so a ±0.09 confidence interval at
  95%. The 12.6x and 30.6x multiples are large enough to survive that; the two rates being
  numerically identical is coincidence, not structure.
- **The 50/50 answer is arithmetic on measured rates, not a measured run.** Doing it properly
  would have meant a second canary deployment, and the budget was already gone.
- **The reference is saturated** — 12 concurrent callers on one replica — so the latency
  figures here describe the load generator as much as the service, and the latency signal is
  unusable for the reason given above rather than merely unused.
- **`MIN_WINDOW` and `CONFIRMATIONS` were committed on reasoning, and this run tested them.**
  The reasoning for `MIN_WINDOW` was warm-up and the false-alarm data says 150 would be the
  better floor. I have not changed it, because changing a committed threshold using the run it
  was committed for is the thing `3857ccf` exists to prevent. It belongs in Lab 4, with its
  own evidence and its own prior commit.
