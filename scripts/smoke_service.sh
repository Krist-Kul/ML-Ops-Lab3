#!/usr/bin/env bash
# Lab 3 Task 1 — exercise every route against a running service and print what came back.
#
#   bash scripts/smoke_service.sh http://127.0.0.1:8080
#
# Evidence, not a test: it prints status codes and bodies so a reader can see the version
# in each response, the 503 before readiness, and the 422 message for a bad row. The
# assertions live in tests/test_service.py, which CI runs.
set -uo pipefail
TARGET="${1:-http://127.0.0.1:8080}"
ROW='{"temp_c":78.4,"vibration_mm_s":3.1,"pressure_kpa":315.2,"hours_since_service":4200.0,"load_pct":68.0,"ambient_humidity":55.0}'
BAD='{"temp_c":78.4,"vibration_mm_s":3.1,"pressure_kpa":315.2,"hours_since_service":4200.0,"load_pct":250.0,"ambient_humidity":55.0}'

call() {  # call <label> <method> <path> [body]
  local label="$1" method="$2" path="$3" body="${4:-}"
  local rid="smoke-$(date +%s%N | tail -c 7)"
  echo "--- $label  ($method $path, x-request-id: $rid)"
  if [ -n "$body" ]; then
    curl -sS -o /tmp/smoke.out -w '%{http_code} in %{time_total}s\n' \
      -H 'content-type: application/json' -H "x-request-id: $rid" \
      -X "$method" "$TARGET$path" -d "$body"
  else
    curl -sS -o /tmp/smoke.out -w '%{http_code} in %{time_total}s\n' \
      -H "x-request-id: $rid" -X "$method" "$TARGET$path"
  fi
  python3 -m json.tool < /tmp/smoke.out 2>/dev/null || cat /tmp/smoke.out
  echo
}

echo "target: $TARGET   at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
call "liveness"        GET  /health
call "readiness"       GET  /ready
call "single row"      POST /predict "$ROW"
call "batch of 3"      POST /predict/batch "{\"rows\":[$ROW,$ROW,$ROW]}"
call "out of range"    POST /predict "$BAD"
call "unknown field"   POST /predict "$(echo "$ROW" | sed 's/}$/,"surprise":1}/')"
call "batch over 100"  POST /predict/batch "{\"rows\":[$(python3 -c "print(','.join(['$ROW']*101))")]}"
