#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Confluence Cloud Weekly Backup — systemd wrapper
#
# Sources config from /etc/m365-backup/confluence.env, runs the Python CLI,
# uploads with rclone to multiple destinations, verifies, and cleans up.
# -----------------------------------------------------------------------------
set -euo pipefail

# ---------- config -----------------------------------------------------------
ENV_FILE="${ENV_FILE:-/etc/m365-backup/confluence.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file not found: $ENV_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$ENV_FILE"

# Required variables
: "${ATLASSIAN_SITE_URL:?missing ATLASSIAN_SITE_URL}"
: "${ATLASSIAN_EMAIL:?missing ATLASSIAN_EMAIL}"
: "${ATLASSIAN_API_TOKEN:?missing ATLASSIAN_API_TOKEN}"
: "${LOCAL_SNAPSHOT_ROOT:?missing LOCAL_SNAPSHOT_ROOT}"
: "${CONFLUENCE_DESTS:?missing CONFLUENCE_DESTS}"
: "${LOG_FILE:?missing LOG_FILE}"

DATE="$(date -u +%Y-%m-%d)"
SNAPSHOT="${LOCAL_SNAPSHOT_ROOT}/${DATE}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

# CLI script location (same repo by default, overridable)
CLI_SCRIPT="${CLI_SCRIPT:-/opt/atlassian-cloud-backups/confluence_backup_cli.py}"
PYTHON="${PYTHON:-/opt/atlassian-cloud-backups/.venv/bin/python3}"

# ---------- logging ----------------------------------------------------------
mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "===== Confluence backup started at $(date -u --iso-8601=seconds) ====="

# ---------- snapshot dir -----------------------------------------------------
mkdir -p "$SNAPSHOT"

# ---------- run backup CLI ---------------------------------------------------
echo "Running Python backup CLI ..."
"$PYTHON" "$CLI_SCRIPT" \
    --site-url "$ATLASSIAN_SITE_URL" \
    --email "$ATLASSIAN_EMAIL" \
    --api-token "$ATLASSIAN_API_TOKEN" \
    --output-dir "$SNAPSHOT"

# ---------- rclone upload to all destinations --------------------------------
IFS=',' read -ra DESTS <<< "$CONFLUENCE_DESTS"

for DEST_BASE in "${DESTS[@]}"; do
    DEST_BASE="$(echo "$DEST_BASE" | xargs)"  # trim whitespace
    DEST="${DEST_BASE}/${DATE}"

    echo "--- Uploading snapshot to ${DEST} ---"
    rclone copy "$SNAPSHOT" "$DEST" --progress

    echo "--- Verifying ${DEST} ---"
    rclone check "$SNAPSHOT" "$DEST" --one-way --size-only

    echo "--- ${DEST} verified ---"
done

# ---------- local cleanup ----------------------------------------------------
echo "Removing local snapshots older than ${RETENTION_DAYS} days ..."
find "$LOCAL_SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -exec rm -rf {} +

echo "===== Confluence backup finished at $(date -u --iso-8601=seconds) ====="
