"""backup.sh: copying, history records, flags, mount checks, failures, folder selection."""
import glob
import os
import signal
import time

import pytest

from helpers import tree, write_tree

SHOW_FILES = {
    "Chicago Fire/Season 1/e1.mkv": "cf1",
    "Chicago Fire/Season 2/e1.mkv": "cf2",
    "Gossip Girl/s1.mkv": "gg",
    "Show [2020]/a.mkv": "brackets",
    "Star*Trek/a.mkv": "star",
    "It's Always Sunny/a.mkv": "quote",
    "A&B/a.mkv": "amp",
    "Trailing /a.mkv": "trailing space",
    "Plain/Chicago Fire/nested.txt": "nested folder with an excluded name",
    "Plaintext/a.txt": "prefix of Plain",
    "loose.txt": "top-level file",
}


def make_tv(bk):
    tv = bk.src / "TV"
    write_tree(tv, SHOW_FILES)
    return tv


def copied(bk, name="TV"):
    """Set of top-level names present in the backup copy."""
    d = bk.dst / name
    return set(os.listdir(d)) if d.exists() else set()


# --------------------------------------------------------------------------- basics
def test_full_backup_copies_everything_and_records_history(bk):
    tv = make_tv(bk)
    bk.add(tv)
    r = bk.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert tree(bk.dst / "TV") == tree(tv)

    (job,) = bk.jobs()
    run = bk.last_run()
    assert job["status"] == "success" and job["rc"] == 0
    assert job["source"] == str(tv) and job["dest"] == str(bk.dst / "TV")
    assert job["run_id"] == run["run_id"]
    assert job["stats"]["total_size"] == sum(len(v) for v in SHOW_FILES.values())
    assert job["stats"]["transferred_files"] == len(SHOW_FILES)
    assert job["selection"] == {"mode": "all", "count": 0}
    assert run["status"] == "success" and run["jobs_completed"] == 1
    assert run["trigger"] == "cron"  # no terminal attached
    assert not bk.running_flag.exists()
    assert not bk.fail_flag.exists()


def test_second_run_is_incremental(bk):
    tv = make_tv(bk)
    bk.add(tv)
    assert bk.run().returncode == 0
    assert bk.run().returncode == 0
    second = bk.jobs()[-1]
    assert second["stats"]["transferred_files"] == 0
    assert second["stats"]["transferred_bytes"] == 0


def test_trigger_label_from_environment(bk):
    bk.add(make_tv(bk))
    assert bk.run(BACKUP_TRIGGER="portal").returncode == 0
    assert bk.last_run()["trigger"] == "portal"


def test_backups_run_in_config_order(bk):
    for name in ["Zed", "Alpha", "Mid"]:
        write_tree(bk.src / name, {"f": name})
        bk.add(bk.src / name)
    assert bk.run().returncode == 0
    assert [os.path.basename(j["source"]) for j in bk.jobs()] == ["Zed", "Alpha", "Mid"]
    assert bk.last_run()["jobs_completed"] == 3


def test_does_not_delete_files_removed_from_source(bk):
    """The configured options have no --delete: removing a source file keeps the backup copy."""
    tv = make_tv(bk)
    bk.add(tv)
    assert bk.run().returncode == 0
    (tv / "loose.txt").unlink()
    assert bk.run().returncode == 0
    assert (bk.dst / "TV" / "loose.txt").exists()


def test_successful_run_clears_old_fail_flag(bk):
    bk.add(make_tv(bk))
    bk.fail_flag.write_text("")
    assert bk.run().returncode == 0
    assert not bk.fail_flag.exists()


def test_no_temp_files_left_behind(bk):
    before = set(glob.glob("/tmp/backup_sets*"))
    bk.add(make_tv(bk))
    bk.add(bk.src / "missing")
    bk.run()
    assert set(glob.glob("/tmp/backup_sets*")) == before, "nothing is written to the world-readable /tmp"
    assert os.listdir(bk.history_path.parent / "tmp") == []


def test_temp_files_live_next_to_the_history_file(bk):
    bk.add(make_tv(bk))
    bk.run()
    tmp = bk.history_path.parent / "tmp"
    assert tmp.is_dir() and oct(tmp.stat().st_mode & 0o777) == "0o700"


def test_stale_temp_files_from_a_killed_run_are_swept(bk):
    tmp = bk.history_path.parent / "tmp"
    tmp.mkdir(parents=True)
    old, new = tmp / "backup_sets_stats.OLD", tmp / "backup_sets_stats.NEW"
    old.write_text("x")
    new.write_text("x")
    t = time.time() - 2 * 86400
    os.utime(old, (t, t))
    bk.add(make_tv(bk))
    bk.run()
    assert not old.exists() and new.exists()


def test_log_lines_go_to_the_log_not_to_cron_output(bk):
    """Regression: every log line was also printed, so the cron log got a copy of everything."""
    bk.add(make_tv(bk))
    r = bk.run()
    assert "Backup script started" in bk.log_text()
    assert "Backup script started" not in r.stdout


def test_pause_during_the_last_set_is_noted(bk):
    write_tree(bk.src / "Big", {"big.bin": os.urandom(600_000)})
    bk.add(bk.src / "Big")
    bk.rsync_opts = "-a --stats --bwlimit=200"
    p = bk.popen()
    for _ in range(50):
        if bk.running_flag.exists():
            break
        time.sleep(0.1)
    bk.stop_flag.write_text("")
    p.wait(60)
    assert "last backup set had already started, so it finished" in bk.log_text()


def test_history_problems_never_block_the_backup(bk):
    bk.add(make_tv(bk))
    bk.extra["history_file"] = "/proc/not-writable/history.jsonl"
    r = bk.run()
    assert r.returncode == 0
    assert copied(bk) == set(os.listdir(bk.src / "TV"))
    assert "history records disabled" in bk.log_text()


# --------------------------------------------------------------------------- failures
def test_missing_source_stops_run_and_sets_fail_flag(bk):
    write_tree(bk.src / "First", {"f": "1"})
    write_tree(bk.src / "Third", {"f": "3"})
    bk.add(bk.src / "First")
    bk.add(bk.src / "Missing")
    bk.add(bk.src / "Third")
    r = bk.run()
    assert r.returncode == 1
    assert (bk.dst / "First" / "f").exists()
    assert not (bk.dst / "Third").exists(), "later backups must not run after a failure"
    assert bk.fail_flag.exists()
    assert not bk.running_flag.exists()
    run = bk.last_run()
    assert run["status"] == "failed" and "Source directory does not exist" in run["reason"]
    assert run["jobs_completed"] == 1
    assert [j["status"] for j in bk.jobs()] == ["success", "failed"]


def test_missing_destination_fails(bk):
    bk.add(make_tv(bk), destination=bk.root / "no-such-disk")
    r = bk.run()
    assert r.returncode == 1
    assert "Destination base directory does not exist" in bk.last_run()["reason"]
    assert bk.fail_flag.exists()


def test_rsync_error_is_recorded(bk):
    bk.add(make_tv(bk))
    bk.rsync_opts = "-a --stats --no-such-option"
    r = bk.run()
    assert r.returncode == 1
    (job,) = bk.jobs()
    assert job["status"] == "failed" and job["rc"] not in (0, None)
    assert bk.last_run()["status"] == "failed"
    assert bk.fail_flag.exists()


def test_missing_config_file(bk):
    r = bk.run(write=False)
    assert r.returncode == 1
    assert "Configuration file not found" in r.stdout


@pytest.mark.parametrize("key", ["log_file", "running_flag", "fail_flag", "stop_flag"])
def test_required_config_keys(bk, key):
    bk.add(make_tv(bk))
    bk.extra[key] = None
    r = bk.run()
    assert r.returncode == 1
    assert f"{key} not defined" in r.stdout
    assert not (bk.dst / "TV").exists()


def test_invalid_backup_entry(bk):
    bk.backups.append({"destination": str(bk.dst)})
    r = bk.run()
    assert r.returncode == 1
    assert "Invalid source path" in bk.last_run()["reason"]


# --------------------------------------------------------------------------- flags & mounts
def test_stop_flag_skips_without_copying(bk):
    bk.add(make_tv(bk))
    bk.stop_flag.write_text("")
    r = bk.run()
    assert r.returncode == 0
    assert not (bk.dst / "TV").exists()
    assert bk.stop_flag.exists()
    assert bk.last_run()["status"] == "skipped" and bk.last_run()["reason"] == "Stop flag present"


def test_already_running_skips_and_leaves_flag(bk):
    bk.add(make_tv(bk))
    bk.running_flag.write_text("other run")
    r = bk.run()
    assert r.returncode == 1
    assert not (bk.dst / "TV").exists()
    assert bk.running_flag.read_text() == "other run"
    assert bk.last_run()["status"] == "skipped"


def test_mount_failure_does_not_remove_another_runs_flag(bk):
    """Regression: a run whose mount check fails used to delete the flag of the run in progress."""
    bk.add(make_tv(bk))
    bk.mounts = [bk.root]  # a plain directory, not a mount point
    bk.running_flag.write_text("other run")
    r = bk.run()
    assert r.returncode == 1
    assert bk.running_flag.exists() and bk.running_flag.read_text() == "other run"


def test_unmounted_required_mount_prevents_backup(bk):
    bk.add(make_tv(bk))
    bk.mounts = ["/", bk.root]
    r = bk.run()
    assert r.returncode == 1
    assert not (bk.dst / "TV").exists()
    assert bk.fail_flag.exists()
    assert "Mount point not mounted" in bk.last_run()["reason"]


def test_mounted_required_mount_passes(bk):
    bk.add(make_tv(bk))
    bk.mounts = ["/"]
    assert bk.run().returncode == 0
    assert "Mount point verified: /" in bk.log_text()


def _slow_source(bk):
    write_tree(bk.src / "Big", {"big.bin": os.urandom(600_000)})
    bk.add(bk.src / "Big")
    bk.rsync_opts = "-a --stats --bwlimit=200"  # ~3 s


def test_concurrent_starts_only_one_runs(bk):
    _slow_source(bk)
    bk.write_config()
    a = bk.popen(write=False)
    b = bk.popen(write=False)
    a.wait(60)
    b.wait(60)
    statuses = sorted(r["status"] for r in bk.runs())
    assert statuses == ["skipped", "success"]
    assert not bk.running_flag.exists()


def test_skipped_run_does_not_remove_flag_of_active_run(bk):
    _slow_source(bk)
    first = bk.popen()
    for _ in range(50):
        if bk.running_flag.exists():
            break
        time.sleep(0.1)
    assert bk.running_flag.exists()
    second = bk.run(write=False)
    assert second.returncode == 1
    assert bk.running_flag.exists(), "the active run's flag must survive"
    first.wait(60)
    assert first.returncode == 0
    assert not bk.running_flag.exists()


def test_interrupt_records_failure_and_cleans_up(bk):
    _slow_source(bk)
    p = bk.popen()
    for _ in range(50):
        if bk.running_flag.exists():
            break
        time.sleep(0.1)
    os.killpg(p.pid, signal.SIGTERM)
    p.wait(30)
    assert p.returncode != 0
    assert not bk.running_flag.exists()
    assert bk.last_run()["status"] == "failed"


# --------------------------------------------------------------------------- folder selection
def test_exclude_skips_only_listed_top_level_folders(bk):
    tv = make_tv(bk)
    excluded = ["Chicago Fire", "Show [2020]", "Star*Trek", "It's Always Sunny", "A&B", "Trailing "]
    bk.add(tv, exclude=excluded)
    assert bk.run().returncode == 0
    assert copied(bk) == {"Gossip Girl", "Plain", "Plaintext", "loose.txt"}
    # a nested folder with an excluded name is still copied
    assert (bk.dst / "TV" / "Plain" / "Chicago Fire" / "nested.txt").exists()
    assert bk.jobs()[-1]["selection"] == {"mode": "exclude", "count": len(excluded)}
    assert "Selection: excluding 6 folder(s)" in bk.log_text()


def test_exclude_wildcard_names_are_literal(bk):
    """'Star*Trek' must not match 'StarXTrek'; 'Show [2020]' must not match 'Show 2'."""
    write_tree(bk.src / "TV", {"Star*Trek/a": "1", "StarXTrek/a": "2", "Show [2020]/a": "3", "Show 2/a": "4"})
    bk.add(bk.src / "TV", exclude=["Star*Trek", "Show [2020]"])
    assert bk.run().returncode == 0
    assert copied(bk) == {"StarXTrek", "Show 2"}


def test_include_copies_only_listed_folders_plus_loose_files(bk):
    tv = make_tv(bk)
    bk.add(tv, include=["Show [2020]", "Star*Trek", "Trailing "])
    assert bk.run().returncode == 0
    assert copied(bk) == {"Show [2020]", "Star*Trek", "Trailing ", "loose.txt"}
    assert tree(bk.dst / "TV" / "Show [2020]") == tree(tv / "Show [2020]")
    assert bk.jobs()[-1]["selection"] == {"mode": "include", "count": 3}


def test_include_without_loose_files(bk):
    tv = make_tv(bk)
    bk.add(tv, include=["A&B"], include_root_files=False)
    assert bk.run().returncode == 0
    assert copied(bk) == {"A&B"}


def test_include_copies_whole_subtree(bk):
    tv = make_tv(bk)
    bk.add(tv, include=["Chicago Fire"])
    assert bk.run().returncode == 0
    assert tree(bk.dst / "TV" / "Chicago Fire") == tree(tv / "Chicago Fire")


def test_empty_lists_mean_everything(bk):
    tv = make_tv(bk)
    bk.add(tv, exclude=[], include=[])
    assert bk.run().returncode == 0
    assert copied(bk) == set(os.listdir(tv))
    assert bk.jobs()[-1]["selection"]["mode"] == "all"


def test_new_folders_backed_up_automatically_in_exclude_mode(bk):
    tv = make_tv(bk)
    bk.add(tv, exclude=["Chicago Fire"])
    assert bk.run().returncode == 0
    write_tree(tv, {"Brand New Show/e1.mkv": "new"})
    assert bk.run().returncode == 0
    assert "Brand New Show" in copied(bk)


def test_new_folders_not_backed_up_in_include_mode(bk):
    tv = make_tv(bk)
    bk.add(tv, include=["Gossip Girl"])
    assert bk.run().returncode == 0
    write_tree(tv, {"Brand New Show/e1.mkv": "new"})
    assert bk.run().returncode == 0
    assert "Brand New Show" not in copied(bk)


def test_deselected_copy_is_left_on_destination(bk):
    """Deselecting never deletes anything by itself."""
    tv = make_tv(bk)
    bk.add(tv)
    assert bk.run().returncode == 0
    bk.backups[0]["exclude"] = ["Chicago Fire"]
    assert bk.run().returncode == 0
    assert "Chicago Fire" in copied(bk)


def test_excluded_folder_protected_even_with_delete_option(bk):
    tv = make_tv(bk)
    bk.add(tv)
    assert bk.run().returncode == 0
    bk.backups[0]["exclude"] = ["Chicago Fire"]
    bk.rsync_opts = "-a --stats --delete"
    assert bk.run().returncode == 0
    assert (bk.dst / "TV" / "Chicago Fire" / "Season 1" / "e1.mkv").exists()


def test_selection_applies_per_entry(bk):
    write_tree(bk.src / "One", {"keep/a": "1", "skip/a": "2"})
    write_tree(bk.src / "Two", {"keep/a": "3", "skip/a": "4"})
    bk.add(bk.src / "One", exclude=["skip"])
    bk.add(bk.src / "Two")
    assert bk.run().returncode == 0
    assert copied(bk, "One") == {"keep"}
    assert copied(bk, "Two") == {"keep", "skip"}


def test_include_and_exclude_together_is_an_error(bk):
    bk.add(make_tv(bk), include=["A&B"], exclude=["Plain"])
    r = bk.run()
    assert r.returncode == 1
    assert not (bk.dst / "TV").exists() or not os.listdir(bk.dst / "TV")
    assert "both include and exclude" in bk.last_run()["reason"]
    assert bk.fail_flag.exists()


@pytest.mark.parametrize("bad", ["../etc", "a/b", ".", "..", ""])
def test_invalid_folder_names_are_rejected(bk, bad):
    bk.add(make_tv(bk), exclude=[bad])
    r = bk.run()
    assert r.returncode == 1
    assert "Invalid folder name" in bk.last_run()["reason"]
    assert not (bk.dst / "TV").exists() or not os.listdir(bk.dst / "TV")


# --------------------------------------------------------------------------- review fixes
import subprocess  # noqa: E402


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid  # finished, so no such process now


def test_running_flag_contains_pid(bk):
    _slow_source(bk)
    p = bk.popen()
    for _ in range(50):
        if bk.running_flag.exists() and bk.running_flag.read_text().strip():
            break
        time.sleep(0.1)
    assert bk.running_flag.read_text().strip().isdigit()
    p.wait(60)


def test_stale_running_flag_is_cleared_and_backup_runs(bk):
    """Regression: a run that crashed (power loss, kill -9) left its flag behind and every
    later backup was skipped as 'already running' forever."""
    bk.add(make_tv(bk))
    bk.running_flag.write_text(f"{_dead_pid()}\n")
    r = bk.run()
    assert r.returncode == 0, r.stdout
    assert bk.last_run()["status"] == "success"
    assert "Removing stale running flag" in bk.log_text()
    assert not bk.running_flag.exists()


def test_flag_whose_pid_is_now_another_program_is_stale(bk):
    """PIDs get reused: a live process that isn't backup.sh doesn't hold the flag."""
    bk.add(make_tv(bk))
    other = subprocess.Popen(["sleep", "30"])
    try:
        bk.running_flag.write_text(f"{other.pid}\n")
        assert bk.run().returncode == 0
        assert bk.last_run()["status"] == "success"
    finally:
        other.kill()


def test_flag_without_pid_is_still_respected(bk):
    bk.add(make_tv(bk))
    bk.running_flag.write_text("")
    assert bk.run().returncode == 1
    assert bk.last_run()["status"] == "skipped"
    assert bk.running_flag.exists()


def test_running_flag_folder_missing_is_an_error_not_already_running(bk):
    bk.add(make_tv(bk))
    bk.running_flag = bk.root / "no-such-dir" / ".running"
    r = bk.run()
    assert r.returncode == 1
    assert "Cannot create running flag" in bk.last_run()["reason"]
    assert not (bk.dst / "TV").exists()


def test_fail_flag_that_cannot_be_created_is_logged(bk):
    bk.add(bk.src / "missing")
    bk.fail_flag = bk.root / "no-such-dir" / ".failed"
    assert bk.run().returncode == 1
    assert "could not create fail flag" in bk.log_text()


def test_pause_during_run_skips_remaining_sets(bk):
    write_tree(bk.src / "Big", {"big.bin": os.urandom(600_000)})
    write_tree(bk.src / "Second", {"f": "2"})
    write_tree(bk.src / "Third", {"f": "3"})
    bk.add(bk.src / "Big")
    bk.add(bk.src / "Second")
    bk.add(bk.src / "Third")
    bk.rsync_opts = "-a --stats --bwlimit=200"
    p = bk.popen()
    for _ in range(50):
        if bk.running_flag.exists():
            break
        time.sleep(0.1)
    bk.stop_flag.write_text("")  # paused while the first set is copying
    p.wait(60)
    assert p.returncode == 0
    assert (bk.dst / "Big" / "big.bin").exists(), "the set in progress finishes"
    assert not (bk.dst / "Second").exists() and not (bk.dst / "Third").exists()
    run = bk.last_run()
    assert run["status"] == "stopped" and "2 backup set(s) not run" in run["reason"]
    assert not bk.running_flag.exists() and not bk.fail_flag.exists()


def test_rsync_options_are_not_wildcard_expanded(bk, monkeypatch, tmp_path):
    """Regression: unquoted $RSYNC_OPTS was glob-expanded, so '--exclude=*.tmp' could turn
    into '--exclude=a.tmp' if a file of that name existed in the working directory."""
    write_tree(bk.src / "Set", {"keep.txt": "k", "a.tmp": "a", "b.tmp": "b"})
    bk.add(bk.src / "Set")
    bk.rsync_opts = "-a --stats --exclude=*.tmp"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "--exclude=a.tmp").write_text("")  # bait for glob expansion
    monkeypatch.chdir(cwd)
    assert bk.run().returncode == 0
    assert sorted(os.listdir(bk.dst / "Set")) == ["keep.txt"]
