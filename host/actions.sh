#!/bin/bash
################################################################################
# Backup portal action bridge (runs on the HOST, as root, via systemd path unit)
#
# The portal container cannot run rsync or write to /mnt. Instead it drops a
# request file into $REQUEST_DIR; this script validates and executes it.
#
# Requests (file names):
#   run, stop_on, stop_off, clear_fail   content: "user=<name>"
#   remove-<32 hex>                      content: JSON {user, source, destination, folder}
#       Deletes <destination>/<basename source>/<folder> from a backup disk.
#       Re-validated here against the SAVED config.yaml: the folder must be
#       deselected for that backup entry (or no longer exist in the source).
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$SCRIPT_DIR")}"
CONFIG_FILE="${BACKUP_CONFIG:-${BACKUP_DIR}/config.yaml}"
STATE_DIR="${STATE_DIR:-/var/lib/backup_sets}"
REQUEST_DIR="${REQUEST_DIR:-$STATE_DIR/requests}"
REMOVAL_DIR="${REMOVAL_DIR:-$STATE_DIR/removals}"
CRON_LOG="${CRON_LOG:-/var/log/backup_sets_cron.log}"
SYSTEMD_RUN="${SYSTEMD_RUN:-systemd-run}"   # overridable for tests

yq_read() { yq -r "$@" < "$CONFIG_FILE" 2>/dev/null; }

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

# Write the status file the portal polls. Args: id status message [extra json fields]
removal_status() {
    local id="$1" st="$2" msg="$3"
    mkdir -p "$REMOVAL_DIR"
    jq -n --arg id "$id" --arg status "$st" --arg message "$msg" --arg user "$R_USER" \
        --arg source "$R_SOURCE" --arg destination "$R_DEST" --arg folder "$R_FOLDER" \
        --arg target "$R_TARGET" --argjson ts "$(date +%s)" \
        '{id:$id, status:$status, message:$message, user:$user, source:$source,
          destination:$destination, folder:$folder, target:$target, updated:$ts}' \
        > "$REMOVAL_DIR/$id.json.tmp" && mv -f "$REMOVAL_DIR/$id.json.tmp" "$REMOVAL_DIR/$id.json"
}

handle_remove() {
    local id="$1" json="$2"
    R_USER=$(jq -r '.user // "unknown"' <<< "$json" 2>/dev/null)
    R_SOURCE=$(jq -r '.source // ""' <<< "$json" 2>/dev/null)
    R_DEST=$(jq -r '.destination // ""' <<< "$json" 2>/dev/null)
    R_FOLDER=$(jq -r '.folder // ""' <<< "$json" 2>/dev/null)
    R_TARGET=""
    [[ "$R_USER" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || R_USER="unknown"

    refuse() {
        log "removal of '$R_FOLDER' from $R_DEST refused (requested by $R_USER): $1"
        removal_status "$id" "failed" "$1"
    }

    # --- folder name: a single plain path component
    if [ -z "$R_FOLDER" ] || [ "$R_FOLDER" = "." ] || [ "$R_FOLDER" = ".." ] \
        || [[ "$R_FOLDER" == */* ]] || [[ "$R_FOLDER" == *$'\n'* ]]; then
        refuse "invalid folder name"; return
    fi
    if valid_path "$RUNNING_FLAG" && [ -f "$RUNNING_FLAG" ]; then
        refuse "a backup is running - try again when it finishes"; return
    fi

    # --- the backup entry must exist in the saved config
    local n idx="" i
    n=$(yq_read '.backups | length')
    for (( i=0; i<${n:-0}; i++ )); do
        if [ "$(yq_read ".backups[$i].source")" = "$R_SOURCE" ] && \
           [ "$(yq_read ".backups[$i].destination")" = "$R_DEST" ]; then
            idx=$i; break
        fi
    done
    if [ -z "$idx" ] || [ -z "$R_SOURCE" ] || [ -z "$R_DEST" ]; then
        refuse "no backup entry with this source and destination in config.yaml"; return
    fi

    # --- the folder must not be selected for backup (or must be gone from the source)
    local in_exc in_inc inc_n allowed=""
    in_exc=$(yq_read --arg f "$R_FOLDER" ".backups[$idx].exclude // [] | any(. == \$f)")
    in_inc=$(yq_read --arg f "$R_FOLDER" ".backups[$idx].include // [] | any(. == \$f)")
    inc_n=$(yq_read ".backups[$idx].include // [] | length")
    if [ "$in_exc" = "true" ]; then
        allowed="excluded"
    elif [ "${inc_n:-0}" -gt 0 ] && [ "$in_inc" != "true" ]; then
        allowed="not included"
    elif [ ! -e "$R_SOURCE/$R_FOLDER" ]; then
        allowed="no longer in source"
    fi
    if [ -z "$allowed" ]; then
        refuse "folder is still selected for backup - deselect it and save first"; return
    fi

    # --- resolve the target and make sure it is directly inside the backup folder
    local base real_base real_target
    base="$R_DEST/$(basename "$R_SOURCE")"
    R_TARGET="$base/$R_FOLDER"
    if [ -L "$R_TARGET" ] || [ ! -d "$R_TARGET" ]; then
        refuse "not a folder on the backup disk: $R_TARGET"; return
    fi
    real_base=$(realpath -e -- "$base" 2>/dev/null)
    real_target=$(realpath -e -- "$R_TARGET" 2>/dev/null)
    if [ -z "$real_base" ] || [ "$(dirname -- "$real_target")" != "$real_base" ]; then
        refuse "target resolves outside the backup folder"; return
    fi

    log "removing $R_TARGET ($allowed) - requested by $R_USER"
    removal_status "$id" "running" "Deleting…"
    # Run in its own transient unit: large folders take a while, and this
    # oneshot service must not hold the request queue.
    "$SYSTEMD_RUN" --quiet --collect --unit="backup-sets-remove-${id:0:12}" \
        --setenv=R_ID="$id" --setenv=R_TARGET="$real_target" --setenv=R_USER="$R_USER" \
        --setenv=R_SOURCE="$R_SOURCE" --setenv=R_DEST="$R_DEST" --setenv=R_FOLDER="$R_FOLDER" \
        --setenv=LOG_FILE="$LOG_FILE" --setenv=REMOVAL_DIR="$REMOVAL_DIR" \
        /bin/bash -c '
            if rm -rf -- "$R_TARGET"; then st=done; msg="Removed"; else st=failed; msg="rm failed (exit $?)"; fi
            jq -n --arg id "$R_ID" --arg status "$st" --arg message "$msg" --arg user "$R_USER" \
                --arg source "$R_SOURCE" --arg destination "$R_DEST" --arg folder "$R_FOLDER" \
                --arg target "$R_TARGET" --argjson ts "$(date +%s)" \
                "{id:\$id, status:\$status, message:\$message, user:\$user, source:\$source,
                  destination:\$destination, folder:\$folder, target:\$target, updated:\$ts}" \
                > "$REMOVAL_DIR/$R_ID.json.tmp" && mv -f "$REMOVAL_DIR/$R_ID.json.tmp" "$REMOVAL_DIR/$R_ID.json"
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Portal action: removal of $R_TARGET $st (requested by $R_USER)" >> "$LOG_FILE"
        '
}

shopt -s nullglob
for req in "$REQUEST_DIR"/*; do
    [ -f "$req" ] || { rm -rf -- "$req"; continue; }
    action=$(basename "$req")
    content=$(head -c 65536 "$req" 2>/dev/null)
    rm -f -- "$req"
    user=$(grep -m1 -oE '^user=[A-Za-z0-9._-]{1,64}$' <<< "$content" | cut -d= -f2)
    user="${user:-unknown}"

    case "$action" in
        run)
            if valid_path "$RUNNING_FLAG" && [ -f "$RUNNING_FLAG" ]; then
                log "run requested by $user ignored - backup already running"
                continue
            fi
            log "run requested by $user - starting backup"
            "$SYSTEMD_RUN" --quiet --collect --unit="backup-sets-portal-$(date +%s)" \
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
        remove-*)
            if [[ "$action" =~ ^remove-([0-9a-f]{32})$ ]]; then
                handle_remove "${BASH_REMATCH[1]}" "$content"
            else
                log "ignored malformed request '$action'"
            fi
            ;;
        *)
            log "ignored unknown request '$action'"
            ;;
    esac
done
exit 0
