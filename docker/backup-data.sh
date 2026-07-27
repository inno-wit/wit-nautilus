#!/usr/bin/env bash
# Nightly backup of data/ (docs/BUILD_PLAN.md's Phase N8: "data/ is the one stateful
# volume that matters" - journal.jsonl, dream_state.json, KILL_SWITCH). Everything else
# in this repo is either stateless or reproducible from source. Meant to be run from cron
# on the VPS, NOT inside a container - it only needs read access to the bind-mounted
# data/ directory, and a cron job outlives any single container restart.
#
# Install (as the deploy user, not root - matches vps-setup.sh's non-root posture):
#   crontab -e
#   0 3 * * * /path/to/wit-nautilus/docker/backup-data.sh >> /path/to/wit-nautilus/data/.backup.log 2>&1
#
# Usage: ./backup-data.sh [backup-dir]   (default: ../backups, sibling to data/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$REPO_ROOT/data"
BACKUP_DIR="${1:-$REPO_ROOT/backups}"
RETENTION_DAYS=30

if [[ ! -d "$DATA_DIR" ]]; then
    echo "No data/ directory at $DATA_DIR - nothing to back up." >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DIR/data-$stamp.tar.gz"

# --exclude the backup dir itself, in case it was ever nested under data/ by mistake -
# tar-ing a growing archive into itself is exactly the kind of failure mode worth a
# one-line guard against, cheap as it is to write.
#
# tar exits 1 (not 0) when a file it's reading changes size mid-read (its own "file
# changed as we read it" warning) - journal.jsonl is exactly such a file, actively
# appended to by a running node at 3am (Phase N8 audit finding M4). set -e would abort
# the whole script on that exit code and skip the retention prune below over a backup
# that in fact succeeded. Exit codes >1 (2: fatal error, e.g. disk full) still abort.
tar -czf "$archive" -C "$REPO_ROOT" --exclude="backups" data/ || {
    status=$?
    if [[ $status -ne 1 ]]; then
        echo "tar failed with exit $status" >&2
        exit "$status"
    fi
    echo "tar warned (exit 1, likely a file changed mid-read - e.g. the live journal); continuing." >&2
}

echo "Backed up $DATA_DIR -> $archive ($(du -h "$archive" | cut -f1))"

# Prune anything older than RETENTION_DAYS - a nightly cron with no retention policy is
# a slow disk-space leak, not a backup strategy.
find "$BACKUP_DIR" -name 'data-*.tar.gz' -mtime "+$RETENTION_DAYS" -print -delete
