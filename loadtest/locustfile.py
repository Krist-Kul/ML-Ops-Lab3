"""ITCS355 Lab 3 — load test (Locust alternative to k6).

    locust -f loadtest/locustfile.py --host https://<endpoint> \
           --users 10 --spawn-rate 5 --run-time 60s --headless

Use either this or k6, not both. Whichever you choose, commit it.

Task 3's measurements were taken with k6 (`loadtest/k6.js`), because the target and the
thresholds live in that file and k6 fails the run when they are crossed, which makes the
target enforceable rather than decorative. This file is kept working as the second opinion:
`batch_vs_singles` below reproduces experiment 1 under a different client, so a surprising
k6 number can be checked against a tool that shares none of its code.
"""
from __future__ import annotations

import random
import time

from locust import HttpUser, between, events, task

ROWS = 100


def sample_payload() -> dict:
    return {
        "temp_c": round(random.uniform(60, 95), 3),
        "vibration_mm_s": round(random.uniform(1.0, 8.0), 3),
        "pressure_kpa": round(random.uniform(280, 350), 3),
        "hours_since_service": round(random.uniform(0, 9000), 3),
        "load_pct": round(random.uniform(20, 100), 3),
        "ambient_humidity": round(random.uniform(30, 85), 3),
    }


class PredictUser(HttpUser):
    wait_time = between(0.0, 0.1)

    @task(9)
    def predict(self):
        with self.client.post("/predict", json=sample_payload(), catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status {r.status_code}")

    @task(1)
    def batch_vs_singles(self):
        """One batch of ROWS rows against ROWS separate calls, timed as groups.

        Answered, measured, in reports/lab3-load.md: one batch of 100 rows costs the same
        wall clock as one single-row call — 26.4 ms against 26.0 ms locally — so batching
        is worth about 95x the row throughput. The advantage does not disappear at any
        concurrency tested (93x still at 10 concurrent users), and that is not luck: the
        fixed cost is RandomForestClassifier.predict_proba, measured at 25.0 ms for 1 row
        and 25.1 ms for 100, because walking 200 trees costs the same whatever arrives at
        the leaves. Concurrency multiplies that fixed cost on both sides equally, so the
        ratio survives. It would only close if rows per call grew until scoring finally
        dominated dispatch, and the schema caps a batch at 100 — about 1% of the way there.

        Timed as groups on purpose. Comparing per-request latency would flatter the singles,
        since each of them really is fast; what a caller with 100 rows waits is the sum.
        """
        rows = [sample_payload() for _ in range(ROWS)]

        started = time.perf_counter()
        with self.client.post("/predict/batch", json={"rows": rows},
                              name="/predict/batch [%d rows, group]" % ROWS,
                              catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status {r.status_code}")
        batch_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        for row in rows:
            with self.client.post("/predict", json=row,
                                  name="/predict [x%d, group]" % ROWS,
                                  catch_response=True) as r:
                if r.status_code != 200:
                    r.failure(f"status {r.status_code}")
        singles_ms = (time.perf_counter() - started) * 1000

        # Locust has no metric type for "ratio of two groups", so it goes on the request
        # stream as its own named entry: a number nobody can see is not a measurement.
        events.request.fire(
            request_type="CALC", name="speedup [singles/batch]",
            response_time=(singles_ms / batch_ms) if batch_ms else 0,
            response_length=len(rows), exception=None, context={},
        )
