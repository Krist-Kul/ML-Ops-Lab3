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
