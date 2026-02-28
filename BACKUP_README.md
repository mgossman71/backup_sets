# Backup Sets Script - Usage Guide

## Overview
This script performs incremental backups of multiple dataset folders to different USB-connected disks using rsync. It supports concurrent backups with bandwidth control and comprehensive logging. All configuration is managed through a YAML file for easy editing.

## Installation

### 1. Install yq (YAML parser)
```bash
# Option 1: Using snap (recommended)
sudo snap install yq

# Option 2: Direct download
sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
sudo chmod +x /usr/local/bin/yq
```

### 2. Install the script and configuration
```bash
# Copy the script
sudo cp backup_sets.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/backup_sets.sh

# Copy the configuration file
sudo cp backup_config.yaml /etc/
sudo chmod 644 /etc/backup_config.yaml
```

### 3. Create the log file
```bash
sudo touch /var/log/backup_sets.log
sudo chown gozz:gozz /var/log/backup_sets.log
```

## Configuration

All settings are in `/etc/backup_config.yaml`. Edit this file to change backup behavior:

```bash
sudo nano /etc/backup_config.yaml
```

### Configuration Options

#### max_concurrent
Number of simultaneous backup jobs (default: 2)
```yaml
max_concurrent: 2
```
- Increase if you have more bandwidth
- Decrease if backups are too slow

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
sudo /usr/local/bin/backup_sets.sh
```

You'll see real-time progress in the terminal and it will also be logged.

### Automated Execution (Crontab)

Run daily at 2 AM:
```bash
sudo crontab -e
```

Add this line:
```
0 2 * * * /usr/local/bin/backup_sets.sh
```

Run every Sunday at 3 AM:
```
0 3 * * 0 /usr/local/bin/backup_sets.sh
```

## Control Flags

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

When you create new datasets or want to backup additional locations, simply edit the YAML config:

```bash
sudo nano /etc/backup_config.yaml
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

### Bandwidth Issues?
Edit `/etc/backup_config.yaml` and reduce max_concurrent:
```yaml
max_concurrent: 1
```

### Fast Drives?
Increase max_concurrent:
```yaml
max_concurrent: 3
```

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
6. Verify config file exists: `ls -la /etc/backup_config.yaml`

### Configuration errors
```bash
# Validate YAML syntax
yq eval '.' /etc/backup_config.yaml

# Check specific values
yq eval '.max_concurrent' /etc/backup_config.yaml
yq eval '.backups | length' /etc/backup_config.yaml
yq eval '.backups[0].source' /etc/backup_config.yaml
```

### Backup failed
1. Check the log for ERROR messages
2. Verify all USB disks are mounted
3. Check disk space on destinations
4. Verify source paths exist
5. Remove fail flag: `sudo rm /mnt/.backup_failed`
6. Try running manually to see real-time errors

### Performance is slow
1. Reduce `max_concurrent` to 1 in config
2. Check USB connection (USB 2.0 vs 3.0 vs 3.1)
3. Check if drives are healthy
4. Add `--bwlimit` to `rsync_opts` in config

## Example Output

### Successful Run:
```
[2025-02-28 02:00:01] ==========================================
[2025-02-28 02:00:01] Backup script started
[2025-02-28 02:00:01] ==========================================
[2025-02-28 02:00:01] Loaded configuration from: /etc/backup_config.yaml
[2025-02-28 02:00:01] MAX_CONCURRENT: 2
[2025-02-28 02:00:01] LOG_FILE: /var/log/backup_sets.log
[2025-02-28 02:00:01] RSYNC_OPTS: -av --stats
[2025-02-28 02:00:01] Created running flag: /mnt/.backup_running
[2025-02-28 02:00:01] Starting backup process with MAX_CONCURRENT=2
[2025-02-28 02:00:01] Found 7 backup(s) to process
[2025-02-28 02:00:01] Starting backup: /mnt/SharedVol/Movies/Set0 -> /mnt/usb_chassis/disk1/Set0
[2025-02-28 02:00:01] Started background job for Set0 (PID: 12345, Active jobs: 1)
[2025-02-28 02:00:01] Starting backup: /mnt/SharedVol/Movies/Set1 -> /mnt/usb_chassis/disk2/Set1
[2025-02-28 02:00:01] Started background job for Set1 (PID: 12346, Active jobs: 2)
...
[2025-02-28 02:45:23] All backup jobs completed successfully
[2025-02-28 02:45:23] ==========================================
[2025-02-28 02:45:23] All backups completed successfully!
[2025-02-28 02:45:23] ==========================================
```

## Configuration File Example

Complete example of `/etc/backup_config.yaml`:

```yaml
# Backup Configuration File

# Maximum concurrent backups (tune for your USB bandwidth)
max_concurrent: 2

# Log file location
log_file: /var/log/backup_sets.log

# Rsync options
rsync_opts: "-av --stats"

# Backup mappings
backups:
  # Movies on disk1
  - source: /mnt/SharedVol/Movies/default
    destination: /mnt/usb_chassis/disk1
  
  - source: /mnt/SharedVol/Movies/Set0
    destination: /mnt/usb_chassis/disk1
  
  # Movies on disk2
  - source: /mnt/SharedVol/Movies/Set1
    destination: /mnt/usb_chassis/disk2
  
  - source: /mnt/SharedVol/Movies/Set2
    destination: /mnt/usb_chassis/disk2
  
  # Movies on disk3
  - source: /mnt/SharedVol/Movies/Set3
    destination: /mnt/usb_chassis/disk3
  
  - source: /mnt/SharedVol/Movies/Set4
    destination: /mnt/usb_chassis/disk3
  
  # Movies on disk4
  - source: /mnt/SharedVol/Movies/Set5
    destination: /mnt/usb_chassis/disk4
  
  # Photos on disk5 (different source location!)
  - source: /mnt/OtherVol/Photos
    destination: /mnt/usb_chassis/disk5
```

## Safety Features

1. **Single instance**: Script won't run if already running
2. **Stop control**: Touch `/mnt/.backup_stop` to prevent execution
3. **Failure detection**: Creates `/mnt/.backup_failed` on errors
4. **Comprehensive logging**: All actions logged with timestamps
5. **Automatic cleanup**: Removes running flag on exit/error
6. **Error propagation**: If any backup fails, all stop and flag is set
7. **Configuration validation**: Checks for required settings and valid YAML
8. **Dependency check**: Verifies yq is installed before running
