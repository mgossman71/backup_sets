#!/bin/bash
################################################################################
# Backup portal action bridge (runs on the HOST, as root, via systemd path unit)
#
# The portal container cannot run rsync or write to /mnt. Instead it drops a
# request file into $REQUEST_DIR; this script validates and executes it.
#
# Allowed requests (file names): run, stop_on, stop_off, clear_fail
# Optional file content: "user=<name>" (for the audit log line)
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$SCRIPT_DIR")}"
CONFIG_FILE="${BACKUP_CONFIG:-${BACKUP_DIR}/config.yaml}"
REQUEST_DIR="${REQUEST_DIR:-/var/lib/backup_sets/requests}"
CRON_LOG="${CRON_LOG:-/var/log/backup_sets_cron.log}"

yq_read() { yq -r "$1" < "$CONFIG_FILE" 2>/dev/null; }

LOG_FILE=$(yq_read '.log_file')
STOP_FLAG=$(yq_read '.stop_flag')
FAIL_FLAG=$(yq_read '.fail_flag')
RUNNING_FLAG=$(yq_read '.running_flag')

log() {
    local line="[$(date '+%Y-%m-%d %H:%M:%S')] Portal action: $1"
    echo "$line"
    if [ -n "$LOG_FILE" ] && [ "$LOG_FILE" != "null" ]; then
        echo "$line" >> "$LOG_FILE"
    fi
}

valid_path() { [ -n "$1" ] && [ "$1" != "null" ]; }

shopt -s nullglob
for req in "$REQUEST_DIR"/*; do
    [ -f "$req" ] || { rm -rf -- "$req"; continue; }
    action=$(basename "$req")
    user=$(grep -m1 -oE '^user=[A-Za-z0-9._-]{1,64}$' "$req" 2>/dev/null | cut -d= -f2)
    user="${user:-unknown}"
    rm -f -- "$req"

    case "$action" in
        run)
            if valid_path "$RUNNING_FLAG" && [ -f "$RUNNING_FLAG" ]; then
                log "run requested by $user ignored - backup already running"
                continue
            fi
            log "run requested by $user - starting backup"
            systemd-run --quiet --collect --unit="backup-sets-portal-$(date +%s)" \
                --setenv=BACKUP_TRIGGER=portal --setenv=BACKUP_CONFIG="$CONFIG_FILE" \
                /bin/bash -c "exec /bin/bash '$BACKUP_DIR/backup.sh' >> '$CRON_LOG' 2>&1"
            ;;
        stop_on)
            valid_path "$STOP_FLAG" && touch "$STOP_FLAG" && log "stop flag set by $user ($STOP_FLAG)"
            ;;
        stop_off)
            valid_path "$STOP_FLAG" && rm -f "$STOP_FLAG" && log "stop flag cleared by $user ($STOP_FLAG)"
            ;;
        clear_fail)
            valid_path "$FAIL_FLAG" && rm -f "$FAIL_FLAG" && log "fail flag cleared by $user ($FAIL_FLAG)"
            ;;
        *)
            log "ignored unknown request '$action'"
            ;;
    esac
done
exit 0
