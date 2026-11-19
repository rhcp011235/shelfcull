#!/bin/bash
# Start the demo stack on a spare port, run the browser test against it, stop it.
#   tests/run_ui_test.sh [screenshot dir]        env: PLAYWRIGHT=/path/to/playwright-core  CHROMIUM=/path/to/chromium
set -u
cd "$(dirname "$0")/.." || exit 1
port=${UI_TEST_PORT:-8791}
PORT=$port DEMO_DEFAULT_VIEW=movies python3 tests/demo.py >/dev/null 2>&1 &
pid=$!
for _ in $(seq 1 40); do curl -fs "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 0.25; done
node tests/ui_test.js "http://127.0.0.1:$port/" "${1:-}"
rc=$?
kill "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
exit $rc
