#!/usr/bin/env bash
# Run one jsdom prototype suite N times in a row and report how many runs passed (M56, DEC-876).
#
#   scripts/loop_prototype_tests.sh [COUNT] [TEST_FILE]
#   LOOP_LOAD=4 scripts/loop_prototype_tests.sh 50 tests/prototype/uplift.test.mjs
#
# COUNT defaults to 10; TEST_FILE defaults to tests/prototype/uplift.test.mjs and is resolved from the
# repository root. LOOP_LOAD=N starts N busy loops for the length of the measurement and stops them
# on exit, so "under load" means the same thing every time the number is taken. Each failing run's
# TAP output is kept under LOOP_LOG_DIR (default: a fresh temporary directory) for a post-mortem.
# The exit status is 0 only when every run passed.
set -euo pipefail

count="${1:-10}"
file="${2:-tests/prototype/uplift.test.mjs}"
load="${LOOP_LOAD:-0}"
root="$(cd "$(dirname "$0")/.." && pwd)"
log_dir="${LOOP_LOG_DIR:-$(mktemp -d)}"
mkdir -p "$log_dir"

case "$file" in /*) abs="$file" ;; *) abs="$root/$file" ;; esac
[ -f "$abs" ] || { echo "no such test file: $file" >&2; exit 2; }
suite_dir="$(dirname "$abs")"
[ -d "$suite_dir/node_modules" ] || { echo "run 'npm ci' in $suite_dir first" >&2; exit 2; }

busy=()
stop_load() { for p in "${busy[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap stop_load EXIT
for _ in $(seq 1 "$load"); do
  ( while :; do :; done ) &
  busy+=("$!")
done

pass=0
fail=0
for i in $(seq 1 "$count"); do
  out="$log_dir/run-$i.tap"
  if (cd "$suite_dir" && node --test "$(basename "$abs")") >"$out" 2>&1; then
    pass=$((pass + 1))
    rm -f "$out"
    echo "run $i/$count: pass"
  else
    fail=$((fail + 1))
    echo "run $i/$count: FAIL ($(grep -c '^not ok' "$out" || true) failing, log: $out)"
    grep -E '^    not ok|^not ok' "$out" | sed 's/^/    /' || true
  fi
done

echo "passed $pass of $count runs of $file (busy loops: $load, failures kept in $log_dir)"
[ "$fail" -eq 0 ]
