# Backup Sets Script - Usage Guide

## Overview
This script performs incremental backups of multiple dataset folders to different USB-connected disks using rsync. It supports concurrent backups with bandwidth control and comprehensive logging. All configuration is managed through a YAML file for easy editing.

## Installation

### 1. Install yq (YAML parser)

The script supports both common versions of yq:

**Option 1: Python-based yq (kislyuk/yq)**
```bash
pip install yq
# or
sudo apt install yq
```

**Option 2: Go-based yq (mikefarah/yq)**
```bash
# Using snap
sudo snap install yq

# Or direct download
sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
sudo chmod +x /usr/local/bin/yq
```

The script will automatically detect which version you have and use the appropriate syntax.

### 2. Install the script and configuration
```bash
# Create installation directory (or use your preferred location)
sudo mkdir -p /usr/local/bin/backup_scripts

# Copy both files to the same directory
sudo cp backup.sh /usr/local/bin/backup_scripts/
sudo cp config.yaml /usr/local/bin/backup_scripts/

# Make script executable
sudo chmod +x /usr/local/bin/backup_scripts/backup.sh

# Optional: Create a symlink for easier access
sudo ln -s /usr/local/bin/backup_scripts/backup.sh /usr/local/bin/backup.sh
```

### 3. Create the log file
```bash
sudo touch /var/log/backup_sets.log
sudo chown gozz:gozz /var/log/backup_sets.log
```

## Configuration

The configuration file `config.yaml` must be in the same directory as `backup.sh`. Edit this file to change backup behavior:

```bash
# If installed to /usr/local/bin/backup_scripts/
sudo nano /usr/local/bin/backup_scripts/config.yaml
```

### Configuration Options

#### log_file
Location of the log file
```yaml
log_file: /var/log/backup_sets.log
```

#### rsync_opts
Options passed to rsync (space-separated string)
```yaml
rsync_opts: "-av --stats"
```
- `-a`: Archive mode (preserves permissions, timestamps)
- `-v`: Verbose output
- `--stats`: Show transfer statistics
- Add `--progress` for detailed progress
- Add `--bwlimit=10000` to limit bandwidth to 10MB/s

#### running_flag / fail_flag / stop_flag
Paths to the flag files used to control and monitor the script. Customize these when running multiple instances simultaneously — each instance must use a unique set of flag file paths.
```yaml
running_flag: /mnt/.backup_running
fail_flag: /mnt/.backup_failed
stop_flag: /mnt/.backup_stop
```

#### backups
List of source-to-destination mappings
```yaml
backups:
  - source: /mnt/SharedVol/Movies/Set0
    destination: /mnt/usb_chassis/disk1

  - source: /mnt/SharedVol/Movies/Set1
    destination: /mnt/usb_chassis/disk2

  - source: /mnt/OtherVol/Photos
    destination: /mnt/usb_chassis/disk3
```

Each backup entry requires:
- `source`: Full path to the directory to backup
- `destination`: Base path where the backup will be stored

The script will create a folder with the same name as the source folder inside the destination.

## Usage

### Manual Execution
```bash
# If you created the symlink:
sudo backup.sh

# Or run directly from installation directory:
sudo /usr/local/bin/backup_scripts/backup.sh
```

You'll see real-time progress in the terminal and it will also be logged.

### Running Multiple Instances Simultaneously

To run two separate backup jobs at the same time, create a separate config for each instance with unique flag file paths:

**config_set_a.yaml:**
```yaml
running_flag: /mnt/.backup_a_running
fail_flag: /mnt/.backup_a_failed
stop_flag: /mnt/.backup_a_stop
# ... other settings and backups ...
```

**config_set_b.yaml:**
```yaml
running_flag: /mnt/.backup_b_running
fail_flag: /mnt/.backup_b_failed
stop_flag: /mnt/.backup_b_stop
# ... other settings and backups ...
```

The script reads `config.yaml` from its own directory by default. To use a different config file, either copy/symlink the script to a different directory alongside its own config, or modify `CONFIG_FILE` at the top of `backup.sh`.

### Automated Execution (Crontab)

Run daily at 2 AM:
```bash
sudo crontab -e
```

Add this line (with proper output redirection for cron):
```
0 2 * * * /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1
```

Or if you created the symlink:
```
0 2 * * * /usr/local/bin/backup.sh >> /var/log/backup_sets_cron.log 2>&1
```

Run every Sunday at 3 AM:
```
0 3 * * 0 /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1
```

**Note:** The `>> /var/log/backup_sets_cron.log 2>&1` part ensures cron doesn't hang waiting for output.

## Control Flags

Flag file paths are set in `config.yaml` (`running_flag`, `fail_flag`, `stop_flag`). The examples below use the default paths.

### Check if Backup is Running
```bash
ls -la /mnt/.backup_running
```
- If file exists: backup is currently running
- If file doesn't exist: no backup running

### Prevent Backup from Starting
```bash
sudo touch /mnt/.backup_stop
```
This prevents the script from running (useful during maintenance).

To allow backups again:
```bash
sudo rm /mnt/.backup_stop
```

### Check for Failures
```bash
ls -la /mnt/.backup_failed
```
- If file exists: last backup failed (check logs)
- Remove it before trying again: `sudo rm /mnt/.backup_failed`

## Monitoring

### View Logs
```bash
# View entire log with live updates
sudo tail -f /var/log/backup_sets.log

# View last 50 lines
sudo tail -50 /var/log/backup_sets.log

# Search for errors
sudo grep ERROR /var/log/backup_sets.log

# View today's backups
sudo grep "$(date '+%Y-%m-%d')" /var/log/backup_sets.log
```

### Check Backup Status
```bash
# Is backup running?
[ -f /mnt/.backup_running ] && echo "Running" || echo "Not running"

# Did last backup fail?
[ -f /mnt/.backup_failed ] && echo "Failed" || echo "OK"

# Is backup stopped?
[ -f /mnt/.backup_stop ] && echo "Stopped" || echo "Enabled"
```

## Adding New Backups

When you create new datasets or want to backup additional locations, simply edit the YAML config file in the same directory as the script:

```bash
# Edit the config file (adjust path to your installation location)
sudo nano /usr/local/bin/backup_scripts/config.yaml
```

Add new entries to the `backups` section:
```yaml
backups:
  - source: /mnt/SharedVol/Movies/default
    destination: /mnt/usb_chassis/disk1

  - source: /mnt/SharedVol/Movies/Set0
    destination: /mnt/usb_chassis/disk1

  # ... existing entries ...

  # NEW ENTRIES
  - source: /mnt/SharedVol/Movies/Set6
    destination: /mnt/usb_chassis/disk4

  - source: /mnt/OtherVol/Photos/Archive
    destination: /mnt/usb_chassis/disk5

  - source: /home/data/Documents
    destination: /mnt/usb_chassis/disk6
```

No need to restart anything - the script reads the config file each time it runs.

## Tuning Performance

### Limit Bandwidth
Add bandwidth limit to rsync_opts (in KB/s):
```yaml
rsync_opts: "-av --stats --bwlimit=10000"
```
This limits to 10MB/s (10000 KB/s)

### Show Progress During Manual Runs
```yaml
rsync_opts: "-av --stats --progress"
```

## Troubleshooting

### Script won't start
1. Check if yq is installed: `yq --version`
2. Check if already running: `ls /mnt/.backup_running`
3. Check if stopped: `ls /mnt/.backup_stop`
4. Check for previous failure: `ls /mnt/.backup_failed`
5. Check logs: `sudo tail -100 /var/log/backup_sets.log`
6. Verify config file exists in same directory as script
7. Check config file path in logs (script shows where it's looking)

### Configuration errors
```bash
# Navigate to your installation directory
cd /usr/local/bin/backup_scripts

# Validate YAML syntax
# For Python yq:
yq '.' config.yaml

# For Go yq:
yq eval '.' config.yaml

# Check specific values (Python yq syntax):
yq -r '.running_flag' config.yaml
yq -r '.backups | length' config.yaml
yq -r '.backups[0].source' config.yaml

# Check specific values (Go yq syntax):
yq eval '.running_flag' config.yaml
yq eval '.backups | length' config.yaml
yq eval '.backups[0].source' config.yaml
```

### Backup failed
1. Check the log for ERROR messages
2. Verify all USB disks are mounted
3. Check disk space on destinations
4. Verify source paths exist
5. Remove fail flag: `sudo rm /mnt/.backup_failed`
6. Try running manually to see real-time errors

### Performance is slow
1. Check USB connection (USB 2.0 vs 3.0 vs 3.1)
2. Check if drives are healthy
3. Add `--bwlimit` to `rsync_opts` in config

## Example Output

### Successful Run:
```
[2025-02-28 02:00:01] ==========================================
[2025-02-28 02:00:01] Backup script started
[2025-02-28 02:00:01] ==========================================
[2025-02-28 02:00:01] Loaded configuration from: /usr/local/bin/backup_scripts/config.yaml
[2025-02-28 02:00:01] LOG_FILE: /var/log/backup_sets.log
[2025-02-28 02:00:01] RSYNC_OPTS: -av --stats
[2025-02-28 02:00:01] RUNNING_FLAG: /mnt/.backup_running
[2025-02-28 02:00:01] FAIL_FLAG: /mnt/.backup_failed
[2025-02-28 02:00:01] STOP_FLAG: /mnt/.backup_stop
[2025-02-28 02:00:01] Created running flag: /mnt/.backup_running
[2025-02-28 02:00:01] Starting backup process (sequential mode)
[2025-02-28 02:00:01] Found 4 backup(s) to process
[2025-02-28 02:00:01] Starting backup: Data
...
[2025-02-28 02:45:23] All backup jobs completed successfully
[2025-02-28 02:45:23] ==========================================
[2025-02-28 02:45:23] All backups completed successfully!
[2025-02-28 02:45:23] ==========================================
```

## Configuration File Example

Complete example of `config.yaml` (keep in same directory as script):

```yaml
# Backup Configuration File

# Log file location
log_file: /var/log/backup_sets.log

# Rsync options
rsync_opts: "-av --stats"

# Flag files - customize per instance when running multiple simultaneously
running_flag: /mnt/.backup_running
fail_flag: /mnt/.backup_failed
stop_flag: /mnt/.backup_stop

# Backup mappings
backups:
  # Data volume
  - source: /mnt/SharedVol/Data
    destination: /mnt/usb_chassis/disk1

  # Movies on disk1
  - source: /mnt/SharedVol/Movies/Set1
    destination: /mnt/usb_chassis/disk1

  # Movies on disk2
  - source: /mnt/SharedVol/Movies/Set2
    destination: /mnt/usb_chassis/disk2

  # Movies on disk3
  - source: /mnt/SharedVol/Movies/Set3
    destination: /mnt/usb_chassis/disk3

  # Photos on disk5 (different source location!)
  - source: /mnt/OtherVol/Photos
    destination: /mnt/usb_chassis/disk5
```

## Safety Features

1. **Single instance**: Script won't run if already running (per flag file)
2. **Stop control**: Touch the `stop_flag` path to prevent execution
3. **Failure detection**: Creates `fail_flag` on errors
4. **Comprehensive logging**: All actions logged with timestamps
5. **Automatic cleanup**: Removes running flag on exit/error
6. **Error propagation**: If any backup fails, all stop and flag is set
7. **Configuration validation**: Checks for required settings and valid YAML
8. **Dependency check**: Verifies yq is installed before running
