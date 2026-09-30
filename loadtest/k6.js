// ITCS355 Lab 3 — load test.
//
//   k6 run -e TARGET=http://127.0.0.1:8080/predict -e VUS=10 loadtest/k6.js
//
// Run this at THREE concurrency levels (suggested 1, 10, 50) and record p50, p95, p99,
// throughput, and error rate for each. Commit the results in reports/lab3-load.md.
//
// An uncommitted load test is not evidence.
//
// ---------------------------------------------------------------------------------------
// THE TARGET, STATED BEFORE ANY MEASUREMENT WAS TAKEN
//
// Committed in its own commit, ahead of the run that produced reports/lab3-load.md, so that
// git history can show the target was not chosen to fit the numbers.
//
// Subject: ONE container in the production shape the endpoint runs — 2 vCPU, 1 replica,
// the digest-pinned serving image, model resolved from the registry at startup — called by
// a client on the same host. That is the thing whose speed I control. The p95 target is a
// statement about the service, not about the internet between a laptop in Bangkok and a
// machine in Singapore.
//
//   predict_latency_ms  p95 < 50 ms    the service budget
//   predict_latency_ms  p99 < 120 ms
//   predict_failures    rate < 0.5%    the error budget
//
// Why 50 ms. Scoring one row of six floats through a small sklearn classifier is
// compute-trivial; Task 1 measured p95 = 15.4 ms on a single worker with no concurrency.
// 50 ms is about 3x that, which is the headroom I would defend in a design review: it
// absorbs GIL contention and scheduler noise at 10 concurrent callers, and if the number
// goes past it the cause is queueing, not scoring — the service has run out of workers and
// the fix is replicas, not optimisation. Choosing a number comfortably above the measured
// floor is the point: a target set at the floor tells you nothing when it fails.
//
// Why 120 ms at p99. One scheduler or GC hiccup per hundred requests is normal and should
// not count as a breach. 120 ms keeps even that tail inside the 200 ms end-to-end budget
// below, so the slowest 1% of callers are still served within the API's promise.
//
// Why 0.5% and not the 1% the scaffold suggested. The readiness gate exists precisely so
// that traffic is never routed to an unloaded model, so a 5xx under steady load is not a
// startup race — it is the service dropping work. 1% would let 30 dropped requests per
// minute at 50 VUs pass as healthy.
//
// Two budgets deliberately NOT expressed as k6 thresholds, because they are about a
// different subject and folding them in here would make one failing number ambiguous:
//
//   * End-to-end, in-region: p95 < 200 ms for a caller inside asia-southeast1. The
//     scaffold's number, kept. It is the promise to a client; the 50 ms above is the share
//     of it the container is allowed to spend.
//   * Cold start: reported as its own figure, never inside the p95. It is paid once per
//     replica at deploy time, not once per request, and averaging a 7-second model load
//     into a 15-millisecond request distribution is how a slow load hides behind a large
//     denominator. Budget: under 60 s, because that is when a managed endpoint's readiness
//     probe gives up and restarts the container.
//
// Known to be outside all of these before measuring: the Bangkok-to-Singapore round trip,
// measured at 451–629 ms in Task 2's smoke test. Runs against the deployed endpoint are
// reported against that floor and are not evidence about the service's own speed.
// ---------------------------------------------------------------------------------------

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

const latency = new Trend('predict_latency_ms');
const failures = new Rate('predict_failures');
// Per-row cost, so a 100-row batch and 100 single calls can be compared on the same axis.
const perRow = new Trend('per_row_ms');
// One iteration of MODE=singles is ROWS sequential calls; this is the wall clock for the
// whole group, which is what a caller with 100 rows to score actually waits.
const groupMs = new Trend('group_ms');
const bytesSent = new Counter('request_bytes');

// MODE=single   one row  -> /predict            (the default; the three-level sweep)
// MODE=batch    ROWS rows -> /predict/batch     (experiment 1, the batch side)
// MODE=singles  ROWS sequential /predict calls  (experiment 1, the control)
// MODE=parse    one row + PAD junk bytes        (experiment 2; expects 422, see below)
const MODE = __ENV.MODE || 'single';
const ROWS = Number(__ENV.ROWS || 100);
const PAD = Number(__ENV.PAD || 0);
// FLAVOUR=vertex speaks the managed endpoint's envelope instead of the neutral routes.
// The service itself never learns about this; the translation lives out here, the same way
// it lives in cloudlayer/ and not in service/ in the repository proper.
const FLAVOUR = __ENV.FLAVOUR || 'neutral';

export const options = {
  vus: Number(__ENV.VUS || 10),
  duration: __ENV.DURATION || '60s',
  thresholds: {
    'predict_latency_ms': ['p(95)<50', 'p(99)<120'],
    'predict_failures': ['rate<0.005'],
  },
  // Percentiles k6 does not report by default. p99 is a stated target, so it has to be in
  // the summary of every run rather than computed afterwards from a mean.
  summaryTrendStats: ['min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'avg', 'count'],
  discardResponseBodies: false,
};

function row() {
  return {
    temp_c: 60 + Math.random() * 35,
    vibration_mm_s: 1 + Math.random() * 7,
    pressure_kpa: 280 + Math.random() * 70,
    hours_since_service: Math.random() * 9000,
    load_pct: 20 + Math.random() * 80,
    ambient_humidity: 30 + Math.random() * 55,
  };
}

// The endpoint wants {"instances": [...]} and answers {"predictions": [...]}; the neutral
// service wants the row itself and answers {"probability": ...}.
function envelope(rows) {
  if (FLAVOUR === 'vertex') return JSON.stringify({ instances: rows });
  if (MODE === 'batch') return JSON.stringify({ rows: rows });
  return JSON.stringify(rows[0]);
}

const headers = { 'Content-Type': 'application/json' };
if (__ENV.TOKEN) headers['Authorization'] = `Bearer ${__ENV.TOKEN}`;

function post(body, expect) {
  const res = http.post(__ENV.TARGET, body, { headers: headers });
  latency.add(res.timings.duration);
  bytesSent.add(body.length);
  failures.add(res.status !== expect);
  return res;
}

export default function () {
  if (MODE === 'singles') {
    // The control for experiment 1: score ROWS rows the naive way, one request each, and
    // time the whole group. Comparing this group total against one batch call is the only
    // fair comparison — per-request latency alone would flatter the singles.
    const started = Date.now();
    let ok = true;
    for (let i = 0; i < ROWS; i++) {
      const res = post(JSON.stringify(row()), 200);
      if (res.status !== 200) ok = false;
    }
    const total = Date.now() - started;
    groupMs.add(total);
    perRow.add(total / ROWS);
    check(null, { 'every call in the group returned 200': () => ok });
    return;
  }

  if (MODE === 'parse') {
    // Experiment 2 isolates serialisation from scoring. The request schema is
    // extra='forbid', so a padded body is rejected at validation having never reached the
    // model. Comparing this against MODE=batch at the same body size separates "the cost
    // of reading N bytes of JSON" from "the cost of scoring what was in them" — which is
    // the actual question behind "where does serialisation start to dominate".
    const body = JSON.stringify(Object.assign(row(), { pad: 'x'.repeat(PAD) }));
    const res = post(body, 422);
    check(res, {
      'rejected at validation, as designed': (r) => r.status === 422,
      'error body names the offending field': (r) =>
        r.status === 422 && String(r.body).includes('pad'),
    });
    return;
  }

  const rows = MODE === 'batch' ? Array.from({ length: ROWS }, row) : [row()];
  const res = post(envelope(rows), 200);
  perRow.add(res.timings.duration / rows.length);

  if (FLAVOUR === 'vertex') {
    check(res, {
      'status is 200': (r) => r.status === 200,
      'prediction present': (r) => r.status === 200 && r.json('predictions.0') !== undefined,
      // Vertex attests which deployment answered. The container's own claim about its
      // version is not evidence; this header-equivalent in the body is.
      'platform named the model version': (r) =>
        r.status === 200 && r.json('modelVersionId') !== undefined,
    });
    return;
  }

  check(res, {
    'status is 200': (r) => r.status === 200,
    'probability present': (r) =>
      r.status === 200 &&
      (MODE === 'batch' ? r.json('probabilities.0') : r.json('probability')) !== undefined,
    'version reported': (r) => r.headers['X-Model-Version'] !== undefined,
    'request id echoed': (r) => r.headers['X-Request-Id'] !== undefined,
  });
}
