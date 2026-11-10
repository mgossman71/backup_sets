#!/bin/bash

################################################################################
# Backup Script for Multiple Dataset Sets
# Uses rsync with archive mode for incremental backups
# Configuration is loaded from config.yaml
# Runs backups sequentially (one at a time)
# Writes structured run/job records (JSON lines) to history_file for the portal
################################################################################

#------------------------------------------------------------------------------
# CONFIGURATION FILE LOCATION
#------------------------------------------------------------------------------

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Path to YAML configuration file (in same directory as script).
# Can be overridden with the BACKUP_CONFIG environment variable.
CONFIG_FILE="${BACKUP_CONFIG:-${SCRIPT_DIR}/config.yaml}"

# Default location for structured history records (override with history_file)
DEFAULT_HISTORY_FILE="/var/lib/backup_sets/history.jsonl"

# Run identification / trigger source (cron, portal, manual)
RUN_ID="$(date '+%Y%m%dT%H%M%S')-$$"
RUN_START="$(date -Iseconds)"
if [ -n "$BACKUP_TRIGGER" ]; then
    TRIGGER="$BACKUP_TRIGGER"
elif [ -t 1 ]; then
    TRIGGER="manual"
else
    TRIGGER="cron"
fi

# State used for history records
RUN_RECORDED=0
CURRENT_JOB_SOURCE=""
CURRENT_JOB_DEST=""
CURRENT_JOB_START=""
JOBS_DONE=0
FILTER_FILE=""
OWN_RUNNING_FLAG=0   # 1 once THIS run has created the running flag
SELECTION_MODE="all"
SELECTION_COUNT=0

#------------------------------------------------------------------------------
# FUNCTIONS
#------------------------------------------------------------------------------

# Read YAML value - works with kislyuk/yq (Python-based)
yq_read() {
    local query="$1"
    local file="$2"
    cat "$file" | yq -r "$query" 2>/dev/null
}

# Load configuration from YAML
load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "ERROR: Configuration file not found: $CONFIG_FILE"
        exit 1
    fi

    # Read configuration values
    LOG_FILE=$(yq_read '.log_file' "$CONFIG_FILE")
    RSYNC_OPTS=$(yq_read '.rsync_opts' "$CONFIG_FILE")
    RUNNING_FLAG=$(yq_read '.running_flag' "$CONFIG_FILE")
    FAIL_FLAG=$(yq_read '.fail_flag' "$CONFIG_FILE")
    STOP_FLAG=$(yq_read '.stop_flag' "$CONFIG_FILE")
    HISTORY_FILE=$(yq_read '.history_file' "$CONFIG_FILE")

    # Validate required values
    if [ -z "$LOG_FILE" ] || [ "$LOG_FILE" = "null" ]; then
        echo "ERROR: log_file not defined in $CONFIG_FILE"
        exit 1
    fi

    if [ -z "$RUNNING_FLAG" ] || [ "$RUNNING_FLAG" = "null" ]; then
        echo "ERROR: running_flag not defined in $CONFIG_FILE"
        exit 1
    fi

    if [ -z "$FAIL_FLAG" ] || [ "$FAIL_FLAG" = "null" ]; then
        echo "ERROR: fail_flag not defined in $CONFIG_FILE"
        exit 1
    fi

    if [ -z "$STOP_FLAG" ] || [ "$STOP_FLAG" = "null" ]; then
        echo "ERROR: stop_flag not defined in $CONFIG_FILE"
        exit 1
    fi

    # history_file is optional
    if [ -z "$HISTORY_FILE" ] || [ "$HISTORY_FILE" = "null" ]; then
        HISTORY_FILE="$DEFAULT_HISTORY_FILE"
    fi

    log "Loaded configuration from: $CONFIG_FILE"
    log "LOG_FILE: $LOG_FILE"
    log "RSYNC_OPTS: $RSYNC_OPTS"
    log "RUNNING_FLAG: $RUNNING_FLAG"
    log "FAIL_FLAG: $FAIL_FLAG"
    log "STOP_FLAG: $STOP_FLAG"
    log "HISTORY_FILE: $HISTORY_FILE"
    log "RUN_ID: $RUN_ID (trigger: $TRIGGER)"

    init_history
}

# Logging function with timestamp
log() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $1" | tee -a "$LOG_FILE"
}

#------------------------------------------------------------------------------
# HISTORY RECORDS (JSON lines, consumed by the web portal)
# Failures here never stop a backup - history is best-effort.
#------------------------------------------------------------------------------

init_history() {
    if ! command -v jq &> /dev/null; then
        log "WARNING: 'jq' not installed - history records disabled"
        HISTORY_FILE=""
        return
    fi
    local dir
    dir=$(dirname "$HISTORY_FILE")
    if ! mkdir -p "$dir" 2>/dev/null || ! touch "$HISTORY_FILE" 2>/dev/null; then
        log "WARNING: cannot write history file $HISTORY_FILE - history records disabled"
        HISTORY_FILE=""
    fi
}

# Print a number with commas removed, or "null" if empty
num_or_null() {
    local v="${1//,/}"
    if [[ "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "$v"
    else
        echo "null"
    fi
}

# Extract the first number after "<label>:" from an rsync --stats output file
rsync_stat() {
    local label="$1"
    local file="$2"
    grep -m1 "^${label}:" "$file" 2>/dev/null | sed -E 's/^[^:]*: *([0-9,.]+).*/\1/'
}

# Append a job record. Args: status rc stats_file reason
record_job() {
    [ -z "$HISTORY_FILE" ] && return 0
    local status="$1" rc="$2" stats_file="$3" reason="$4"
    local files="" created="" deleted="" xfer_count="" total_size="" xfer_size="" sent="" rate=""

    if [ -n "$stats_file" ] && [ -f "$stats_file" ]; then
        files=$(rsync_stat "Number of files" "$stats_file")
        created=$(rsync_stat "Number of created files" "$stats_file")
        deleted=$(rsync_stat "Number of deleted files" "$stats_file")
        xfer_count=$(rsync_stat "Number of regular files transferred" "$stats_file")
        total_size=$(rsync_stat "Total file size" "$stats_file")
        xfer_size=$(rsync_stat "Total transferred file size" "$stats_file")
        # "sent 13,277,019,594 bytes  received 429 bytes  94,498,363.15 bytes/sec"
        sent=$(grep -m1 '^sent ' "$stats_file" 2>/dev/null | awk '{print $2}')
        rate=$(grep -m1 '^sent ' "$stats_file" 2>/dev/null | awk '{print $(NF-1)}')
    fi

    jq -cn \
        --arg run_id "$RUN_ID" \
        --arg source "$CURRENT_JOB_SOURCE" \
        --arg dest "$CURRENT_JOB_DEST" \
        --arg start "$CURRENT_JOB_START" \
        --arg end "$(date -Iseconds)" \
        --arg status "$status" \
        --arg reason "$reason" \
        --argjson rc "$(num_or_null "$rc")" \
        --argjson files "$(num_or_null "$files")" \
        --argjson created "$(num_or_null "$created")" \
        --argjson deleted "$(num_or_null "$deleted")" \
        --argjson transferred_files "$(num_or_null "$xfer_count")" \
        --argjson total_size "$(num_or_null "$total_size")" \
        --argjson transferred_bytes "$(num_or_null "$xfer_size")" \
        --argjson sent_bytes "$(num_or_null "$sent")" \
        --argjson bytes_per_sec "$(num_or_null "$rate")" \
        --arg sel_mode "$SELECTION_MODE" \
        --argjson sel_count "$(num_or_null "$SELECTION_COUNT")" \
        '{type:"job", run_id:$run_id, source:$source, dest:$dest, start:$start, end:$end,
          status:$status, rc:$rc, reason:(if $reason == "" then null else $reason end),
          selection:{mode:$sel_mode, count:$sel_count},
          stats:{files:$files, created:$created, deleted:$deleted,
                 transferred_files:$transferred_files, total_size:$total_size,
                 transferred_bytes:$transferred_bytes, sent_bytes:$sent_bytes,
                 bytes_per_sec:$bytes_per_sec}}' \
        >> "$HISTORY_FILE" 2>/dev/null || true
}

# Append the run record (only once per run). Args: status reason
record_run() {
    [ -z "$HISTORY_FILE" ] && return 0
    [ "$RUN_RECORDED" -eq 1 ] && return 0
    RUN_RECORDED=1
    local status="$1" reason="$2"
    jq -cn \
        --arg run_id "$RUN_ID" \
        --arg start "$RUN_START" \
        --arg end "$(date -Iseconds)" \
        --arg status "$status" \
        --arg reason "$reason" \
        --arg trigger "$TRIGGER" \
        --arg config "$CONFIG_FILE" \
        --argjson jobs "$JOBS_DONE" \
        '{type:"run", run_id:$run_id, start:$start, end:$end, status:$status,
          reason:(if $reason == "" then null else $reason end),
          trigger:$trigger, config:$config, jobs_completed:$jobs}' \
        >> "$HISTORY_FILE" 2>/dev/null || true
}

# Error logging and cleanup
# Args: message [rsync_rc] [stats_file]
error_exit() {
    log "ERROR: $1"
    touch "$FAIL_FLAG"
    if [ -n "$CURRENT_JOB_SOURCE" ]; then
        record_job "failed" "${2:-}" "${3:-}" "$1"
        [ -n "${3:-}" ] && rm -f "$3"
    fi
    record_run "failed" "$1"
    cleanup
    exit 1
}

#------------------------------------------------------------------------------
# FOLDER SELECTION (per backup entry: include / exclude top-level folders)
#------------------------------------------------------------------------------

# Escape an rsync filter pattern. rsync only honours backslash escapes when the
# pattern contains a wildcard, so escape only in that case.
rsync_escape() {
    local n="$1"
    if [[ "$n" == *[\*\?\[]* ]]; then
        n="${n//\\/\\\\}"
        n="${n//\*/\\*}"
        n="${n//\?/\\?}"
        n="${n//\[/\\[}"
    fi
    printf '%s' "$n"
}

# Validate a folder name from the config and append "<prefix> /<name>/" to the filter file
add_filter_rule() {
    local prefix="$1" name="$2"
    if [ -z "$name" ] || [ "$name" = "." ] || [ "$name" = ".." ] || [[ "$name" == */* ]]; then
        error_exit "Invalid folder name in selection for $CURRENT_JOB_SOURCE: '$name'"
    fi
    printf '%s /%s/\n' "$prefix" "$(rsync_escape "$name")" >> "$FILTER_FILE"
}

# Build the rsync filter file for backup entry <index>. Sets FILTER_FILE,
# SELECTION_MODE (all|include|exclude) and SELECTION_COUNT.
build_filter_file() {
    local i="$1"
    local inc_n exc_n name
    inc_n=$(yq_read ".backups[$i].include // [] | length" "$CONFIG_FILE")
    exc_n=$(yq_read ".backups[$i].exclude // [] | length" "$CONFIG_FILE")
    inc_n=${inc_n:-0}; exc_n=${exc_n:-0}

    SELECTION_MODE="all"
    SELECTION_COUNT=0
    FILTER_FILE=$(mktemp /tmp/backup_sets_filter.XXXXXX)

    if [ "$inc_n" -gt 0 ] && [ "$exc_n" -gt 0 ]; then
        error_exit "Backup entry $i has both include and exclude lists - use only one"
    fi

    if [ "$exc_n" -gt 0 ]; then
        SELECTION_MODE="exclude"
        SELECTION_COUNT=$exc_n
        while IFS= read -r name; do
            add_filter_rule "-" "$name"
        done < <(yq_read ".backups[$i].exclude[]" "$CONFIG_FILE")
        log "Selection: excluding $exc_n folder(s)"
    elif [ "$inc_n" -gt 0 ]; then
        SELECTION_MODE="include"
        SELECTION_COUNT=$inc_n
        while IFS= read -r name; do
            add_filter_rule "+" "$name"
        done < <(yq_read ".backups[$i].include[]" "$CONFIG_FILE")
        # everything else at the top level: other folders are skipped ...
        echo "- /*/" >> "$FILTER_FILE"
        # ... and loose top-level files too, unless include_root_files is true (default)
        if [ "$(yq_read ".backups[$i].include_root_files" "$CONFIG_FILE")" = "false" ]; then
            echo "- /*" >> "$FILTER_FILE"
            log "Selection: only $inc_n folder(s), no loose top-level files"
        else
            log "Selection: only $inc_n folder(s) plus loose top-level files"
        fi
    fi
}

remove_filter_file() {
    if [ -n "$FILTER_FILE" ]; then
        rm -f "$FILTER_FILE"
        FILTER_FILE=""
    fi
}

# Cleanup function
cleanup() {
    remove_filter_file
    # Only remove the flag if this run created it - never another run's flag
    if [ "$OWN_RUNNING_FLAG" -eq 1 ] && [ -f "$RUNNING_FLAG" ]; then
        rm -f "$RUNNING_FLAG"
        OWN_RUNNING_FLAG=0
        log "Removed running flag"
    fi
}

# Verify required mount points are mounted
verify_mount_points() {
    log "Verifying mount points..."

    # Get the number of mount points
    local mount_count=$(yq_read '.required_mount_points | length' "$CONFIG_FILE")

    # If no mount points defined, skip verification
    if [ -z "$mount_count" ] || [ "$mount_count" = "null" ] || [ "$mount_count" -eq 0 ]; then
        log "WARNING: No required_mount_points defined in configuration"
        return 0
    fi

    # Check each mount point
    for (( i=0; i<$mount_count; i++ )); do
        local mount_point=$(yq_read ".required_mount_points[$i]" "$CONFIG_FILE")

        if [ -z "$mount_point" ] || [ "$mount_point" = "null" ]; then
            error_exit "Invalid mount_point in configuration at index $i"
        fi

        # Use mountpoint command to check if directory is a mount point
        if ! mountpoint -q "$mount_point" 2>/dev/null; then
            error_exit "Mount point not mounted: $mount_point - Backups cannot proceed"
        fi

        log "✓ Mount point verified: $mount_point"
    done

    log "All mount points verified successfully"
}

# Check if script should run
check_stop_flag() {
    if [ -f "$STOP_FLAG" ]; then
        log "Stop flag detected at $STOP_FLAG - exiting without running backups"
        log "Remove the stop flag to allow backups: rm $STOP_FLAG"
        record_run "skipped" "Stop flag present"
        exit 0
    fi
}

# Check if script is already running
check_running() {
    if [ -f "$RUNNING_FLAG" ]; then
        log "Backup script is already running (flag exists: $RUNNING_FLAG)"
        log "If this is incorrect, remove the flag: rm $RUNNING_FLAG"
        record_run "skipped" "Already running"
        exit 1
    fi
}

# Create running flag
# Create running flag atomically (noclobber), so two runs starting at the same
# moment cannot both proceed.
set_running_flag() {
    if ! ( set -o noclobber; : > "$RUNNING_FLAG" ) 2>/dev/null; then
        log "Backup script is already running (flag exists: $RUNNING_FLAG)"
        record_run "skipped" "Already running"
        exit 1
    fi
    OWN_RUNNING_FLAG=1
    log "Created running flag: $RUNNING_FLAG"
}

# Perform backup for a single folder
backup_folder() {
    local source_path="$1"
    local dest_base="$2"
    local index="$3"

    # Extract just the folder name from the source path for destination
    local folder_name=$(basename "$source_path")
    local dest_path="${dest_base}/${folder_name}"

    CURRENT_JOB_SOURCE="$source_path"
    CURRENT_JOB_DEST="$dest_path"
    CURRENT_JOB_START="$(date -Iseconds)"

    log "========================================"
    log "Starting backup: $folder_name"
    log "Source: $source_path"
    log "Destination: $dest_path"
    log "========================================"

    # Check if source exists
    if [ ! -d "$source_path" ]; then
        error_exit "Source directory does not exist: $source_path"
    fi

    # Check if destination base exists
    if [ ! -d "$dest_base" ]; then
        error_exit "Destination base directory does not exist: $dest_base"
    fi

    # Create destination folder if it doesn't exist
    if [ ! -d "$dest_path" ]; then
        log "Creating destination directory: $dest_path"
        mkdir -p "$dest_path" || error_exit "Failed to create destination: $dest_path"
    fi

    # Folder selection (include/exclude lists) for this entry
    build_filter_file "$index"

    # Run rsync - full output goes to the log; the tail (with --stats) is kept for history
    log "Running: rsync $RSYNC_OPTS --filter=\"merge <selection>\" $source_path/ $dest_path/"

    local stats_file
    stats_file=$(mktemp /tmp/backup_sets_stats.XXXXXX)

    rsync $RSYNC_OPTS --filter="merge $FILTER_FILE" "$source_path/" "$dest_path/" 2>&1 \
        | tee -a "$LOG_FILE" | tail -n 100 > "$stats_file"
    local rc=${PIPESTATUS[0]}
    remove_filter_file

    if [ "$rc" -eq 0 ]; then
        log "Successfully completed backup: $folder_name"
        record_job "success" "$rc" "$stats_file" ""
        rm -f "$stats_file"
        JOBS_DONE=$((JOBS_DONE + 1))
        CURRENT_JOB_SOURCE=""
        return 0
    else
        error_exit "Rsync failed for: $source_path (exit code $rc)" "$rc" "$stats_file"
    fi
}

# Process all backups sequentially
process_backups() {
    log "Starting backup process (sequential mode)"

    # Get the number of backup entries
    local backup_count=$(yq_read '.backups | length' "$CONFIG_FILE")

    if [ -z "$backup_count" ] || [ "$backup_count" = "null" ] || [ "$backup_count" -eq 0 ]; then
        log "WARNING: No backups defined in configuration file"
        return 0
    fi

    log "Found $backup_count backup(s) to process"

    # Process each backup entry sequentially
    for (( i=0; i<$backup_count; i++ )); do
        local source_path=$(yq_read ".backups[$i].source" "$CONFIG_FILE")
        local destination=$(yq_read ".backups[$i].destination" "$CONFIG_FILE")

        # Validate entries
        if [ -z "$source_path" ] || [ "$source_path" = "null" ]; then
            error_exit "Invalid source path in backup entry $i"
        fi

        if [ -z "$destination" ] || [ "$destination" = "null" ]; then
            error_exit "Invalid destination in backup entry $i"
        fi

        # Run backup (blocking - waits for completion)
        backup_folder "$source_path" "$destination" "$i"
    done

    log "All backup jobs completed successfully"
}

#------------------------------------------------------------------------------
# MAIN SCRIPT
#------------------------------------------------------------------------------

# Check if yq is installed
if ! command -v yq &> /dev/null; then
    echo "ERROR: 'yq' is not installed. Please install it:"
    echo "  pip install yq"
    echo "  or: sudo apt install yq"
    exit 1
fi

# Load configuration (sets LOG_FILE and all flag paths)
load_config

# Set trap to cleanup on interrupt
trap 'record_run "failed" "Interrupted by signal"; cleanup; exit 1' INT TERM

# Start logging
log "=========================================="
log "Backup script started"
log "=========================================="

# Check if stop flag exists
check_stop_flag

# Verify all required mount points are mounted
verify_mount_points

# Check if already running
check_running

# Set running flag
set_running_flag

# Remove any old fail flags
if [ -f "$FAIL_FLAG" ]; then
    rm -f "$FAIL_FLAG"
    log "Removed old fail flag"
fi

# Process all backups
process_backups

# Cleanup and exit
cleanup

record_run "success" ""

# Success
log "=========================================="
log "All backups completed successfully!"
log "=========================================="

exit 0
