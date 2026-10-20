#!/usr/bin/env bash
# shellcheck shell=bash disable=SC2034
# Shared by backup.sh and restore.sh (sourced, not run). Both scripts cd to this directory first, so
# `docker compose` finds docker-compose.yml and .env.

DATA_PATH=/var/lib/marketing-ai
BACKUP_DIR="${BACKUP_DIR:-$PWD/backups}"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# env_value KEY: the value of KEY in .env (last one wins, surrounding quotes dropped), else empty.
env_value() {
  local line
  line=$(grep -E "^$1=" .env | tail -n 1 || true)
  line=${line#*=}
  line=${line%\"}; line=${line#\"}
  printf '%s' "$line"
}

require_setup() {
  [ -f .env ] || die ".env not found in $PWD. Run this from the deploy/pilot directory of the checkout."
  command -v docker >/dev/null || die "docker is not installed"
  mkdir -p "$BACKUP_DIR"
}

api_running() { docker compose ps --status running --services 2>/dev/null | grep -qx api; }

# in_data_volume CMD ARGS...: run a command in a throw-away container of the api image with the data
# volume mounted at $DATA_PATH. The API must be stopped first. stdin/stdout are passed through
# untouched (-T), so it can carry a tarball in or out. The image's own tar, sh and python are used:
# nothing else has to be pulled.
in_data_volume() {
  local entrypoint=$1
  shift
  docker compose run --rm -T --no-deps --entrypoint "$entrypoint" api "$@"
}

# wait_healthy: poll until the api container reports healthy, up to 5 minutes (the image's
# start period is 120 s because AutoGluon's import is slow).
wait_healthy() {
  local status
  for _ in $(seq 1 60); do
    status=$(docker compose ps api --format '{{.Health}}' 2>/dev/null || true)
    [ "$status" = healthy ] && return 0
    sleep 5
  done
  return 1
}

# Every SQLite file under the data directory must pass PRAGMA integrity_check.
INTEGRITY_PY='
import pathlib, sqlite3, sys
bad = 0
for path in sorted(pathlib.Path("'"$DATA_PATH"'").rglob("*.db")):
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    result = con.execute("PRAGMA integrity_check").fetchone()[0]
    print("  %-40s %s" % (path.name, result))
    bad += result != "ok"
sys.exit(1 if bad else 0)
'
