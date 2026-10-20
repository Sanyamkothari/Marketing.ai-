#!/usr/bin/env bash
# Put a backup tarball back into the pilot's data volume. Destructive: the volume's current contents
# are replaced. A safety tarball of what is there now is written first.
#
#   ./restore.sh backups/marketing-ai-c_acme_telecom_1-20261020-020000.tgz
#   ./restore.sh <tarball> --yes        no question asked (for scripts)
#
# From the off-VM copy first:   rclone copy <remote>:<path>/<name>.tgz* backups/     (or scp)
# On a NEW VM: install Docker, clone the repo, put the SAME .env in deploy/pilot, run
# `docker compose build api`, then this script, then `docker compose up -d`.
#
# The check: the checksum file (when present), gzip, the archive lists and holds a database; after
# extracting, every SQLite file passes PRAGMA integrity_check. If that fails the API is left stopped
# and the safety tarball path is printed.
set -euo pipefail
cd "$(dirname "$0")" || exit 2
# shellcheck source=lib.sh
. ./lib.sh

[ $# -ge 1 ] || die "usage: $0 <tarball> [--yes]"
tarball=$1
assume_yes=0
[ "${2:-}" = --yes ] && assume_yes=1
[ -f "$tarball" ] || die "$tarball not found"
require_setup

log "Checking $tarball"
if [ -f "$tarball.sha256" ]; then
  (cd "$(dirname "$tarball")" && sha256sum -c "$(basename "$tarball").sha256") || die "checksum does not match: the file is damaged"
else
  echo "WARNING: no $tarball.sha256 next to it; only gzip and tar are checked." >&2
fi
gzip -t "$tarball" || die "not a valid gzip file"
listing=$(tar -tzf "$tarball") || die "not a valid tar archive"
has_database "$listing" || die "the archive holds no .db file; it is not a backup of this product"
echo "  $(grep -c . <<<"$listing") entries; databases: $(grep '\.db$' <<<"$listing" | xargs -n1 basename | tr '\n' ' ')"

if [ "$assume_yes" = 0 ]; then
  read -r -p "Replace the current data of this deployment with this backup? [y/N] " answer
  case "$answer" in y | Y | yes) ;; *) die "cancelled" ;; esac
fi

log "Stopping the API"
docker compose stop api >/dev/null

safety="$BACKUP_DIR/pre-restore-$(date -u +%Y%m%d-%H%M%S).tgz"
log "Safety copy of the current data: $safety"
in_data_volume tar -czf - -C "$DATA_PATH" . >"$safety"

log "Replacing the data"
in_data_volume sh -c "find '$DATA_PATH' -mindepth 1 -delete && tar -xzf - -C '$DATA_PATH'" <"$tarball"

log "Integrity check of every database"
if ! in_data_volume python -c "$INTEGRITY_PY"; then
  echo "A database failed its integrity check. The API is left STOPPED." >&2
  echo "Put things back with:  ./restore.sh $safety --yes" >&2
  exit 1
fi

log "Starting the API"
docker compose up -d api >/dev/null
wait_healthy || die "the API did not become healthy in 5 minutes: docker compose logs api"
log "Healthy. Next: ./smoke_test.sh"
echo "Restore done. Anything erased (DPDP erasure request) after this backup was taken is back in the"
echo "restored data and must be erased again by hand: the audit log (Admin > Audit log) lists the requests."
