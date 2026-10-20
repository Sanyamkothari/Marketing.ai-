#!/usr/bin/env bash
# Is the pilot deployment answering, and is it locked down the way it should be? Needs only curl and
# sends no credential, uploads nothing, starts no run: safe to run at any time, as often as you like.
#
#   ./smoke_test.sh                              the DOMAIN from .env
#   ./smoke_test.sh https://pilot-acme.example.com
#   INSECURE=1 ./smoke_test.sh https://localhost   accept a self-signed certificate (a rehearsal only)
#
# Exit status: 0 every check passed, 1 at least one failed, 2 could not even start.
set -uo pipefail
cd "$(dirname "$0")" || exit 2

base=${1:-}
if [ -z "$base" ] && [ -f .env ]; then
  domain=$(grep -E '^DOMAIN=' .env | tail -n 1 | cut -d= -f2- | tr -d '"')
  [ -n "$domain" ] && base="https://$domain"
fi
if [ -z "$base" ]; then
  echo "usage: $0 https://<domain>" >&2
  exit 2
fi
base=${base%/}
command -v curl >/dev/null || { echo "curl is not installed" >&2; exit 2; }

curl_opts=(--silent --show-error --max-time 20)
[ "${INSECURE:-0}" = 1 ] && curl_opts+=(--insecure)

failures=0
pass() { printf '  [  ok  ] %s\n' "$1"; }
fail() { printf '  [FAILED] %s\n' "$1"; failures=$((failures + 1)); }

# status URL [curl args...]: the HTTP status code, or 000 when nothing answered.
status() {
  local url=$1
  shift
  curl "${curl_opts[@]}" --output /dev/null --write-out '%{http_code}' "$@" "$url" 2>/dev/null || true
}
# headers URL [curl args...]: the response headers, lower-cased, CR removed.
headers() {
  local url=$1
  shift
  curl "${curl_opts[@]}" --dump-header - --output /dev/null "$@" "$url" 2>/dev/null | tr -d '\r' | tr '[:upper:]' '[:lower:]'
}

echo "Smoke test of $base"

# 1. Health: the one route that answers with no sign-in.
body=$(curl "${curl_opts[@]}" "$base/healthz" 2>/dev/null || true)
if echo "$body" | grep -q '"status" *: *"ok"'; then
  pass "GET /healthz answers status ok ($(echo "$body" | tr -d '\n '))"
else
  fail "GET /healthz did not answer status ok (got: ${body:-nothing})"
fi

# 2. The page a person opens is served: the HTML and the script that draws the sign-in form.
page=$(curl "${curl_opts[@]}" "$base/ui/" 2>/dev/null || true)
if echo "$page" | grep -qi '<html' && [ "$(status "$base/ui/app.js")" = 200 ]; then
  pass "GET /ui/ serves the app (the sign-in form is drawn by /ui/app.js)"
else
  fail "GET /ui/ or /ui/app.js is not served"
fi

# 3. Data is behind sign-in: ENV=prod with AUTH_MODE=local answers 401, never data (503 would mean
#    sign-in is switched off and the deployment fails closed; 200 would be a leak).
for route in /datasets /clients /auth/me; do
  code=$(status "$base$route")
  if [ "$code" = 401 ]; then
    pass "GET $route without a token is refused (401)"
  else
    fail "GET $route without a token answered $code, expected 401 (503 = AUTH_MODE is not local; 200 = open to everyone)"
  fi
done

# 4. Security headers, on a normal answer and on a refusal.
for route in /healthz /datasets; do
  h=$(headers "$base$route")
  missing=""
  for want in 'x-content-type-options: nosniff' 'x-frame-options: deny' 'referrer-policy: no-referrer' 'content-security-policy: frame-ancestors'; do
    echo "$h" | grep -q "^$want" || missing="$missing [$want]"
  done
  case "$base" in
    https://*) echo "$h" | grep -q '^strict-transport-security: ' || missing="$missing [strict-transport-security]" ;;
  esac
  if [ -z "$missing" ]; then pass "security headers present on $route"; else fail "security headers missing on $route:$missing"; fi
done

# 5. CORS: another origin gets no permission, on a request and on a preflight.
evil=https://evil.example.invalid
h=$(headers "$base/healthz" -H "Origin: $evil")
if ! echo "$h" | grep -q '^http/'; then
  fail "a request from $evil got no answer"
elif echo "$h" | grep -q '^access-control-allow-origin:'; then
  fail "a request from $evil got an access-control-allow-origin header"
else
  pass "a request from another origin gets no CORS permission"
fi
h=$(headers "$base/datasets" -X OPTIONS -H "Origin: $evil" -H 'Access-Control-Request-Method: GET')
if ! echo "$h" | grep -q '^http/'; then
  fail "a preflight from $evil got no answer"
elif echo "$h" | grep -q '^access-control-allow-origin:'; then
  fail "a preflight from $evil got an access-control-allow-origin header"
else
  pass "a preflight from another origin gets no CORS permission"
fi

# ...and the configured origin is allowed (only when this checkout's .env is at hand).
if [ -f .env ]; then
  allowed=$(grep -E '^MARKETING_AI_CORS_ORIGINS=' .env | tail -n 1 | cut -d= -f2- | tr -d '"' | cut -d, -f1)
  if [ -n "$allowed" ]; then
    h=$(headers "$base/healthz" -H "Origin: $allowed")
    if echo "$h" | grep -q "^access-control-allow-origin: $(echo "$allowed" | tr '[:upper:]' '[:lower:]')\$"; then
      pass "the configured origin $allowed is allowed"
    else
      fail "the configured origin $allowed (MARKETING_AI_CORS_ORIGINS) got no matching access-control-allow-origin"
    fi
  fi
fi

# 6. Plain http redirects to https (skipped when the address names a port or is not https).
case "$base" in
  https://*:*) ;;
  https://*)
    code=$(status "http://${base#https://}/healthz" --max-redirs 0)
    case "$code" in
      301 | 302 | 307 | 308) pass "http:// redirects to https ($code)" ;;
      *) fail "http://${base#https://}/healthz answered $code, expected a redirect to https" ;;
    esac
    ;;
esac

echo
if [ "$failures" = 0 ]; then
  echo "All checks passed."
  echo "Not covered here (needs a person): sign in as each role, upload, train, approve, score, download."
else
  echo "$failures check(s) FAILED."
  exit 1
fi
