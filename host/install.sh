#!/bin/bash
################################################################################
# Host-side installer for the Backup Sets web portal.
# Safe to re-run. Must be run as root.
#   - creates /var/lib/backup_sets (history, requests, tls)
#   - installs/enables the portal action bridge (systemd path unit)
#   - generates a self-signed TLS certificate if none exists
#   - backfills history from the existing text log (first run only)
#   - installs logrotate config for the backup logs
################################################################################
set -euo pipefail

HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$(dirname "$HOST_DIR")"
STATE_DIR=/var/lib/backup_sets

[ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }

# ---- check every dependency first, so a missing one stops us before anything changes
missing=()
for cmd in rsync jq yq python3 openssl systemctl docker crontab; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if command -v yq >/dev/null 2>&1 && { yq --version 2>&1 | grep -qi mikefarah || \
        ! echo 'a: [1]' | yq -r '.a | length' 2>/dev/null | grep -qx 1; }; then
    missing+=("yq (the Python yq is required - 'apt install yq'; the Go yq from snap is not supported)")
fi
if command -v python3 >/dev/null 2>&1 && ! python3 -c 'import yaml' 2>/dev/null; then
    missing+=("python3-yaml")
fi
if command -v docker >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
    missing+=("docker compose plugin")
fi
if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing dependencies - nothing was changed:"
    printf '  - %s\n' "${missing[@]}"
    echo "On Ubuntu: sudo apt install rsync jq yq python3-yaml openssl docker.io docker-compose-v2 cron"
    exit 1
fi

echo "==> State directory: $STATE_DIR"
install -d -m 0755 "$STATE_DIR"
install -d -m 0700 "$STATE_DIR/requests" "$STATE_DIR/tls"
touch "$STATE_DIR/history.jsonl"

echo "==> Action bridge systemd units"
install -m 0644 "$HOST_DIR/backup-portal-actions.path"    /etc/systemd/system/
install -m 0644 "$HOST_DIR/backup-portal-actions.service" /etc/systemd/system/
chmod 0755 "$HOST_DIR/actions.sh"
systemctl daemon-reload
systemctl enable --now backup-portal-actions.path

if [ ! -f "$STATE_DIR/tls/cert.pem" ]; then
    echo "==> Generating self-signed TLS certificate"
    HOST_IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | sed 's/^/IP:/' | paste -sd, -)
    SAN="DNS:$(hostname),DNS:localhost${HOST_IPS:+,$HOST_IPS}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$STATE_DIR/tls/key.pem" -out "$STATE_DIR/tls/cert.pem" \
        -subj "/CN=$(hostname)" -addext "subjectAltName=$SAN" 2>/dev/null
    chmod 0600 "$STATE_DIR/tls/key.pem"
fi

LOG_FILE=$(yq -r '.log_file' < "$BACKUP_DIR/config.yaml")
if [ ! -s "$STATE_DIR/history_backfill.jsonl" ] && [ -f "$LOG_FILE" ]; then
    echo "==> Backfilling history from $LOG_FILE (one time)"
    python3 -I "$BACKUP_DIR/tools/backfill_history.py" --log "$LOG_FILE" \
        --out "$STATE_DIR/history_backfill.jsonl"
fi

echo "==> Logrotate config"
install -m 0644 "$HOST_DIR/logrotate.backup_sets" /etc/logrotate.d/backup_sets

echo "==> Done. Start the portal with:  cd $BACKUP_DIR && docker compose up -d --build"
