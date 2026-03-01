#!/bin/bash

################################################################################
# Backup Script for Multiple Dataset Sets
# Uses rsync with archive mode for incremental backups
# Configuration is loaded from backup_config.yaml
# Runs backups sequentially (one at a time)
################################################################################

#------------------------------------------------------------------------------
# CONFIGURATION FILE LOCATION
#------------------------------------------------------------------------------

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Path to YAML configuration file (in same directory as script)
CONFIG_FILE="${SCRIPT_DIR}/backup_config.yaml"

# Flag files
RUNNING_FLAG="/mnt/.backup_running"
FAIL_FLAG="/mnt/.backup_failed"
STOP_FLAG="/mnt/.backup_stop"

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
    
    # Validate required values
    if [ -z "$LOG_FILE" ] || [ "$LOG_FILE" = "null" ]; then
        echo "ERROR: log_file not defined in $CONFIG_FILE"
        exit 1
    fi
    
    log "Loaded configuration from: $CONFIG_FILE"
    log "LOG_FILE: $LOG_FILE"
    log "RSYNC_OPTS: $RSYNC_OPTS"
}

# Logging function with timestamp
log() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $1" | tee -a "$LOG_FILE"
}

# Error logging and cleanup
error_exit() {
    log "ERROR: $1"
    touch "$FAIL_FLAG"
    cleanup
    exit 1
}

# Cleanup function
cleanup() {
    if [ -f "$RUNNING_FLAG" ]; then
        rm -f "$RUNNING_FLAG"
        log "Removed running flag"
    fi
}

# Check if script should run
check_stop_flag() {
    if [ -f "$STOP_FLAG" ]; then
        log "Stop flag detected at $STOP_FLAG - exiting without running backups"
        log "Remove the stop flag to allow backups: rm $STOP_FLAG"
        exit 0
    fi
}

# Check if script is already running
check_running() {
    if [ -f "$RUNNING_FLAG" ]; then
        log "Backup script is already running (flag exists: $RUNNING_FLAG)"
        log "If this is incorrect, remove the flag: rm $RUNNING_FLAG"
        exit 1
    fi
}

# Create running flag
set_running_flag() {
    touch "$RUNNING_FLAG"
    log "Created running flag: $RUNNING_FLAG"
}

# Perform backup for a single folder
backup_folder() {
    local source_path="$1"
    local dest_base="$2"
    
    # Extract just the folder name from the source path for destination
    local folder_name=$(basename "$source_path")
    local dest_path="${dest_base}/${folder_name}"
    
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
    
    # Run rsync
    log "Running: rsync $RSYNC_OPTS $source_path/ $dest_path/"
    
    if rsync $RSYNC_OPTS "$source_path/" "$dest_path/" >> "$LOG_FILE" 2>&1; then
        log "Successfully completed backup: $folder_name"
        return 0
    else
        error_exit "Rsync failed for: $source_path"
    fi
}

# Process all backups sequentially
process_backups() {
    log "Starting backup process (sequential mode)"
    
    # Get the number of backup entries
    local backup_count=$(yq_read '.backups | length' "$CONFIG_FILE")
    
    if [ "$backup_count" -eq 0 ] || [ "$backup_count" = "null" ] || [ -z "$backup_count" ]; then
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
        backup_folder "$source_path" "$destination"
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

# Load configuration
load_config

# Set trap to cleanup on interrupt
trap 'cleanup; exit 1' INT TERM

# Start logging
log "=========================================="
log "Backup script started"
log "=========================================="

# Check if stop flag exists
check_stop_flag

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

# Success
log "=========================================="
log "All backups completed successfully!"
log "=========================================="

exit 0
