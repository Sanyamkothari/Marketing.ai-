#!/usr/bin/env bash
# Back up the pilot's data volume to a dated tarball, then copy it off this VM.
#
#   ./backup.sh              tarball in ./backups, copied to BACKUP_TARGET (from .env)
#   ./backup.sh --local-only tarball in ./backups only (a rehearsal; NOT a backup on its own)
#
# Why it stops the API: the data is SQLite files plus run folders, and copying a SQLite file while
# it is being written can capture half a transaction. Stopping the API for the length of the copy
# (about a minute for a few GB) is the simple way to get a consistent file set. The API is started
# again as soon as the tarball is written, before the upload, and also if anything below fails.
# A training run in progress is lost when the API stops: schedule it at night (README, cron).
#
# The tarball holds the client's data but NOT the secrets (they live in .env, which you keep in the
# password manager). Send it only to an encrypted bucket or an rclone "crypt" remote.
# Exit status: 0 done, 1 failed (or no BACKUP_TARGET), 2 --local-only (the copy is still on this VM).
set -euo pipefail
cd "$(dirname "$0")" || exit 2
# shellcheck source=lib.sh
. ./lib.sh

local_only=0
case "${1:-}" in
  --local-only) local_only=1 ;;
  "") ;;
  *) die "usage: $0 [--local-only]" ;;
esac

require_setup
target=$(env_value BACKUP_TARGET)
keep_days=$(env_value BACKUP_KEEP_DAYS)
keep_days=${keep_days:-14}
case "$keep_days" in '' | *[!0-9]*) die "BACKUP_KEEP_DAYS must be a number of days" ;; esac
if [ "$local_only" = 0 ] && [ -z "$target" ]; then
  die "BACKUP_TARGET is not set in .env, so the backup would stay on this VM. Set it, or pass --local-only for a rehearsal."
fi
case "$target" in
  "" | rclone:* | scp:*) ;;
  *) die "BACKUP_TARGET must start with rclone: or scp: (see .env.example)" ;;
esac

client=$(env_value MARKETING_AI_CLIENT_ID)
name="marketing-ai-${client:-pilot}-$(date -u +%Y%m%d-%H%M%S).tgz"
final="$BACKUP_DIR/$name"
partial="$final.partial"

was_running=0
if api_running; then was_running=1; fi

restart_api() {
  if [ "$was_running" = 1 ]; then
    was_running=0
    log "Starting the API again"
    docker compose start api >/dev/null
  fi
}
cleanup() {
  status=$?
  restart_api || echo "WARNING: could not restart the API: run 'docker compose up -d'" >&2
  rm -f "$partial"
  exit "$status"
}
trap cleanup EXIT

if [ "$was_running" = 1 ]; then
  log "Stopping the API for a consistent copy"
  docker compose stop api >/dev/null
fi
log "Writing $name"
in_data_volume tar -czf - -C "$DATA_PATH" . >"$partial"
restart_api

# The check: gzip is intact, the archive lists, and it holds at least one database.
gzip -t "$partial"
tar -tzf "$partial" | grep -q '\.db$' || die "the tarball holds no .db file; refusing to call it a backup"
mv "$partial" "$final"
(cd "$BACKUP_DIR" && sha256sum "$name" >"$name.sha256")
log "Local copy: $final ($(du -h "$final" | cut -f1))"

# --- off the VM -------------------------------------------------------------
case "$target" in
  rclone:*)
    spec=${target#rclone:}
    log "Copying to $spec with rclone"
    command -v rclone >/dev/null || die "rclone is not installed (README step: backups)"
    rclone copyto "$final" "$spec/$name" --checksum
    rclone copyto "$final.sha256" "$spec/$name.sha256"
    rclone delete "$spec" --min-age "${keep_days}d" --include 'marketing-ai-*.tgz*'
    ;;
  scp:*)
    spec=${target#scp:}
    host=${spec%%:*}
    dir=${spec#*:}
    log "Copying to $host:$dir with scp"
    command -v scp >/dev/null || die "scp is not installed"
    scp -q -o BatchMode=yes "$final" "$final.sha256" "$host:$dir/"
    ssh -o BatchMode=yes "$host" "find '$dir' -name 'marketing-ai-*.tgz*' -mtime +$keep_days -delete"
    ;;
esac

# --- local retention --------------------------------------------------------
find "$BACKUP_DIR" -name 'marketing-ai-*.tgz*' -mtime "+$keep_days" -delete

if [ -z "$target" ]; then
  echo "NOTE: --local-only: this copy is on the same VM as the data. It is not a backup." >&2
  exit 2
fi
log "Backup done: $name"
