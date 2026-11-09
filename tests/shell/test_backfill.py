"""tools/backfill_history.py: turning the old text log into history records."""
import json
import subprocess
import sys

from helpers import BACKFILL

STATS = """Number of files: 3,279 (reg: 2,976, dir: 303)
Number of created files: 5 (reg: 4, dir: 1)
Number of deleted files: 0
Number of regular files transferred: 4
Total file size: 5,275,041,058,709 bytes
Total transferred file size: 13,273,601,011 bytes
Literal data: 13,273,601,011 bytes
sent 13,277,019,594 bytes  received 429 bytes  94,498,363.15 bytes/sec
total size is 5,275,041,058,709  speedup is 397.31
"""

LOG = f"""[2026-11-09 02:00:01] Loaded configuration from: /opt/backup_sets/config.yaml
[2026-11-09 02:00:01] ==========================================
[2026-11-09 02:00:01] Backup script started
[2026-11-09 02:00:01] ==========================================
[2026-11-09 02:00:01] Verifying mount points...
[2026-11-09 02:00:01] ✓ Mount point verified: /mnt/usb_chassis/pool
[2026-11-09 02:00:01] Created running flag: /mnt/.backup_running
[2026-11-09 02:00:01] Starting backup process (sequential mode)
[2026-11-09 02:00:05] Starting backup: TV-Shows
[2026-11-09 02:00:05] Source: /mnt/SharedVol/TV-Shows
[2026-11-09 02:00:05] Destination: /mnt/usb_chassis/singleusb/TV-Shows
sending incremental file list
Show/ep1.mkv
         32,768   0%    0.00kB/s    0:00:00      434,536,448  16%  414.38MB/s
[2026-11-09 02:05:01] Backup script started
[2026-11-09 02:05:01] Backup script is already running (flag exists: /mnt/.backup_running)
{STATS}[2026-11-09 02:10:25] Successfully completed backup: TV-Shows
[2026-11-09 02:10:25] All backup jobs completed successfully
[2026-11-09 02:10:25] Removed running flag
[2026-11-09 02:10:25] All backups completed successfully!
[2026-11-10 13:35:01] Backup script started
[2026-11-10 13:35:01] Verifying mount points...
[2026-11-10 13:35:01] ERROR: Mount point not mounted: /mnt/usb_chassis/singleusb - Backups cannot proceed
[2026-11-10 13:40:01] Backup script started
[2026-11-10 13:40:01] Stop flag detected at /mnt/.backup_stop - exiting without running backups
[2026-11-11 02:00:01] Backup script started
[2026-11-11 02:00:01] Created running flag: /mnt/.backup_running
[2026-11-11 02:00:02] Starting backup: Data
[2026-11-11 02:00:02] Source: /mnt/SharedVol/Data
[2026-11-11 02:00:02] Destination: /mnt/usb_chassis/singleusb/Data
[2026-11-11 02:00:09] ERROR: Rsync failed for: /mnt/SharedVol/Data
[2026-11-12 02:00:01] Backup script started
[2026-11-12 02:00:01] Created running flag: /mnt/.backup_running
[2026-11-12 02:00:02] Starting backup: Movies
[2026-11-13 02:00:00] RUN_ID: 20261113T020000-1 (trigger: cron)
[2026-11-13 02:00:00] Backup script started
[2026-11-13 02:00:00] Created running flag: /mnt/.backup_running
[2026-11-13 02:00:01] Starting backup: Movies
[2026-11-13 02:00:09] Successfully completed backup: Movies
[2026-11-13 02:00:09] All backups completed successfully!
"""


def backfill(tmp_path, text=LOG):
    log = tmp_path / "backup.log"
    log.write_text(text)
    out = tmp_path / "out.jsonl"
    r = subprocess.run([sys.executable, "-I", str(BACKFILL), "--log", str(log), "--out", str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    recs = [json.loads(l) for l in out.read_text().splitlines()]
    return json.loads(r.stdout), [x for x in recs if x["type"] == "run"], [x for x in recs if x["type"] == "job"]


def test_counts(tmp_path):
    counts, runs, jobs = backfill(tmp_path)
    assert counts == {"success": 1, "failed": 2, "skipped": 2, "incomplete": 1, "native_skipped": 1}
    assert len(runs) == 6


def test_successful_run_with_stats(tmp_path):
    _, runs, jobs = backfill(tmp_path)
    ok = next(r for r in runs if r["status"] == "success")
    assert ok["start"].startswith("2026-11-09T02:00:01") and ok["end"].startswith("2026-11-09T02:10:25")
    assert ok["backfilled"] is True and ok["jobs_completed"] == 1
    job = next(j for j in jobs if j["run_id"] == ok["run_id"])
    assert job["name"] == "TV-Shows" and job["status"] == "success"
    assert job["source"] == "/mnt/SharedVol/TV-Shows"
    assert job["dest"] == "/mnt/usb_chassis/singleusb/TV-Shows"
    assert job["stats"] == {"files": 3279, "created": 5, "deleted": 0, "transferred_files": 4,
                            "total_size": 5275041058709, "transferred_bytes": 13273601011,
                            "sent_bytes": 13277019594, "bytes_per_sec": 94498363.15}


def test_interleaved_already_running_attempt_is_separate(tmp_path):
    """A cron attempt that starts during a long rsync is its own skipped run and
    does not end or steal the stats of the run in progress."""
    _, runs, jobs = backfill(tmp_path)
    skipped = [r for r in runs if r["status"] == "skipped"]
    assert {r["reason"] for r in skipped} == {"Already running", "Stop flag present"}
    assert not any(j["run_id"] in {r["run_id"] for r in skipped} for j in jobs)


def test_failures(tmp_path):
    _, runs, jobs = backfill(tmp_path)
    failed = {r["reason"] for r in runs if r["status"] == "failed"}
    assert "Mount point not mounted: /mnt/usb_chassis/singleusb - Backups cannot proceed" in failed
    assert "Rsync failed for: /mnt/SharedVol/Data" in failed
    data_job = next(j for j in jobs if j["name"] == "Data")
    assert data_job["status"] == "failed"


def test_run_without_result_is_incomplete(tmp_path):
    _, runs, _ = backfill(tmp_path)
    inc = [r for r in runs if r["status"] == "incomplete"]
    assert len(inc) == 1 and inc[0]["start"].startswith("2026-11-12")


def test_native_runs_are_not_duplicated(tmp_path):
    """Runs from the new backup.sh (RUN_ID line) already have native records."""
    _, runs, _ = backfill(tmp_path)
    assert not any(r["start"].startswith("2026-11-13") for r in runs)


def test_rerun_produces_identical_output(tmp_path):
    a = backfill(tmp_path)
    b = backfill(tmp_path)
    assert a == b


def test_empty_log(tmp_path):
    counts, runs, jobs = backfill(tmp_path, "")
    assert runs == [] and jobs == []
