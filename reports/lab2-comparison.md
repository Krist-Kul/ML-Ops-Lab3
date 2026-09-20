# Lab 2 — Run comparison

Experiment `itcs355-lab2` · 12 trials · 0.1575 THB of trial compute, repriced at the verified rates below. That is fitting time only: the job that ran the study also paid for container startup and data loading, and reports/lab2-study.md has that larger, real bill.

`thb_per_point` is cost per percentage point of val_roc_auc above the worst trial. Cheap improvements rank low; expensive improvements rank high, however good the headline number is.

| trial    | run_id   | max_depth   | max_features   | class_weight       |   val_roc_auc |   test_roc_auc |   cost_thb |   logged_thb | thb_per_point   |
|:---------|:---------|:------------|:---------------|:-------------------|--------------:|---------------:|-----------:|-------------:|:----------------|
| trial-00 | 5c4187a7 | 4           | sqrt           | None               |        0.8405 |         0.8543 |     0.0226 |       0.0168 | 0.0081          |
| trial-01 | 50397ea1 | 4           | sqrt           | balanced_subsample |        0.8392 |         0.8515 |     0.0114 |       0.0085 | 0.0043          |
| trial-05 | d90110df | 8           | sqrt           | balanced_subsample |        0.8388 |         0.8471 |     0.017  |       0.0126 | 0.0065          |
| trial-08 | a78d4f33 | None        | sqrt           | None               |        0.8365 |         0.8418 |     0.0114 |       0.0085 | 0.0048          |
| trial-04 | ef4cd34b | 8           | sqrt           | None               |        0.8364 |         0.8482 |     0.0114 |       0.0085 | 0.0048          |
| trial-02 | 56683dfe | 4           | 1.0            | None               |        0.8317 |         0.847  |     0.0116 |       0.0087 | 0.0061          |
| trial-03 | 029a9d96 | 4           | 1.0            | balanced_subsample |        0.8311 |         0.853  |     0.0119 |       0.0089 | 0.0064          |
| trial-09 | 56594199 | None        | sqrt           | balanced_subsample |        0.8271 |         0.8443 |     0.0119 |       0.0089 | 0.0082          |
| trial-06 | 2171d7f1 | 8           | 1.0            | None               |        0.8239 |         0.8438 |     0.0119 |       0.0088 | 0.0105          |
| trial-07 | d4a5ceb7 | 8           | 1.0            | balanced_subsample |        0.8221 |         0.8443 |     0.0119 |       0.0089 | 0.0125          |
| trial-10 | edae3f91 | None        | 1.0            | None               |        0.819  |         0.8352 |     0.0122 |       0.0091 | 0.0191          |
| trial-11 | 2d731fe3 | None        | 1.0            | balanced_subsample |        0.8126 |         0.838  |     0.0123 |       0.0092 | —               |

Spread in val_roc_auc: 0.0279 across all trials, 0.0041 across the best five. Compare both against reports/lab2-seed-variance.md before believing any ranking here.

## Cost model

| instance | THB/hour on demand | spot factor | THB/hour spot |
|---|---|---|---|
| e2-standard-4 | 6.34 | 0.65 | 4.12 |
| n1-standard-4 | 8.99 | 0.34 | 3.06 |
| n1-standard-4+t4 | 23.19 | 0.37 | 8.58 |

Verified 2026-09-21 against the **Cloud Billing Catalog API** (`cloudbilling.googleapis.com`,
services `C7E2-9256-1C43` Vertex AI and `6F81-5844-456A` Compute Engine), region
`asia-southeast1`, converted at 33.355 THB/USD (frankfurter.dev, 2026-09-18). The AWS and
Azure rows in `src/costs.py` are still the handout's starting values and remain unverified.

A Vertex training hour is **not** the VM price: it is the Compute Engine rate plus a Vertex
management fee, billed per vCPU-hour and per GiB-hour.

| component | Compute Engine | + management fee | = Vertex training |
|---|---|---|---|
| N1 vCPU-hour | 0.038999 | 0.005850 | 0.044849 USD |
| N1 GiB-hour | 0.005226 | 0.000784 | 0.006010 USD |

`n1-standard-4` is 4 vCPU and 15 GiB, so 4 × 0.044849 + 15 × 0.006010 = **0.269546 USD/h =
8.99 THB/h**. Google publishes its own combined RAM SKU at 0.006010 USD, which matches the
sum exactly and confirms the model. The handout's starting value was 7.6 THB/h, 16% low.

**The 30% spot rule of thumb is wrong here**, in both directions. The management fee is not
discounted, and the underlying discount varies by machine type: spot `n1-standard-4` is
0.091785 USD/h, or **34%** of on-demand, while spot `e2-standard-4` is **65%** — barely a
discount at all. Cheap spot capacity is a property of one machine type in one region, not of
spot in general. This is why `src/costs.py` now carries `SPOT_FACTORS` per instance.

The study ran before this check, so it logged its trials at 7.6 THB/h × 0.30 = 2.28 THB/h
when the true spot rate was 3.06 THB/h. Every logged figure understates spend by 34%; the
`cost_thb` column above is repriced, `logged_thb` is what was recorded at the time, and
`reports/lab2-study.md` still holds the original numbers. Corrected, the study's one job cost
0.23 THB rather than 0.17, and the Task 1 on-demand training job cost 0.23 THB rather than
0.19 — both far inside the 150 THB budget.

## Which model did you register, and why?

**Registered: `trial-00` — `max_depth=4`, `max_features=sqrt`, `class_weight=None`, 200 trees.**

This is also the highest val ROC AUC (0.8405), so the margin has to be defended rather than
cited. On its own it is not defensible: the best five trials sit within 0.0040, while
re-seeding the forest alone moves this configuration by 0.0030 and re-seeding — which
re-deals the machine-grouped split — moves it by 0.0273 (`reports/lab2-seed-variance.md`).
Any single trial-to-trial gap here is noise.

What survives the noise is direction, not rank: `sqrt` beats `max_features=1.0` in all six
pairings, and depth 4 ≥ 8 ≥ unlimited in all four. I registered that family, then took
`class_weight=None` within it because weighting moves the metric by 0.0013 — nothing — while
distorting the probabilities Lab 3 has to serve.

Cost cannot break the tie: every trial costs about 0.011 THB. The study cost 0.23 THB, and
retraining this model monthly on spot costs roughly 0.08 THB, dominated by container startup
rather than fitting.

How this could be wrong: PR AUC ranks `trial-04` first. If the service is judged on
precision and recall at a threshold rather than on ranking, I have optimised the wrong
metric on a 12%-positive problem.