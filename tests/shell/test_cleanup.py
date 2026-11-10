"""Backup Folder Cleanup on the host: host/cleanup.py, started through host/actions.sh.

Plans are written here the way the portal writes them; the host must re-check every
item and delete only what is really gone from the source."""
import json
import os
import time

import pytest

from helpers import tree, write_tree
from test_actions import Bridge

SOURCE = {
    "Keep/s1/e1.mkv": "k1",
    "Keep/s1/e2.mkv": "k2",
    "Partly/s1/e1.mkv": "p1",
    "Protected/e1.mkv": "x",
    "Show [x]/e.mkv": "y",
    "loose.nfo": "n",
}
EXTRA = {  # only in the backup
    "Keep/s1/old.mkv": "old episode",
    "Partly/s2/e1.mkv": "deleted season",
    "Partly/s2/e2.mkv": "deleted season",
    "GoneShow/e1.mkv": "gone",
    "Protected/only-in-backup.mkv": "protected - must stay",
    "old-loose.nfo": "old",
}


class CleanupBridge(Bridge):
    def __init__(self, root):
        super().__init__(root)
        # replace the default trees from Bridge with the cleanup scenario
        import shutil
        shutil.rmtree(self.tv)
        shutil.rmtree(self.dst / "TV")
        write_tree(self.tv, SOURCE)
        write_tree(self.dst / "TV", SOURCE)
        write_tree(self.dst / "TV", EXTRA)
        self.plans = self.root / "cleanup"
        self.plans.mkdir()
        self.n = 0

    def plan(self, items, created=None, source=None, destination=None):
        """Write a plan file like the portal does; returns its id."""
        self.n += 1
        pid = f"{self.n:032x}"
        p = {"id": pid, "created": created or time.time(), "source": str(source or self.tv),
             "destination": str(destination or self.dst), "items": [{"path": i, "size": 1} for i in items]}
        (self.plans / f"{pid}.json").write_text(json.dumps(p))
        self.request(f"cleanup-{pid}", json.dumps({"user": "gozz"}))
        return pid

    def result(self, pid):
        return json.loads((self.plans / f"{pid}.status.json").read_text())

    def backup(self):
        return tree(self.dst / "TV")


ALL_EXTRA = ["Keep/s1/old.mkv", "Partly/s2", "GoneShow", "old-loose.nfo"]


@pytest.fixture
def cb(tmp_path):
    b = CleanupBridge(tmp_path)
    b.add(b.tv, exclude=["Protected"])
    return b


def test_deletes_exactly_what_is_gone_from_the_source(cb):
    src_before = tree(cb.tv)
    pid = cb.plan(ALL_EXTRA)
    cb.process()
    r = cb.result(pid)
    assert r["status"] == "done", r
    assert r["deleted"] == 4 and r["skipped"] == 0
    expected = tree(cb.tv)
    expected["Protected/only-in-backup.mkv"] = b"protected - must stay"
    assert cb.backup() == expected
    assert tree(cb.tv) == src_before, "the source is never touched"
    assert (cb.plans / f"{pid}.deleted.txt").read_text().splitlines() == ALL_EXTRA
    assert "cleanup of" in cb.log_text() and "finished" in cb.log_text()


def test_protected_folders_are_never_touched(cb):
    pid = cb.plan(["Protected/only-in-backup.mkv", "Protected"])
    cb.process()
    r = cb.result(pid)
    assert r["deleted"] == 0 and r["skipped"] == 2
    assert {s["reason"] for s in r["skipped_items"]} == {"protected (not selected for backup)"}
    assert (cb.dst / "TV/Protected/only-in-backup.mkv").exists()


def test_include_mode_protects_unlisted_folders_still_in_source(cb):
    cb.backups[0] = {"source": str(cb.tv), "destination": str(cb.dst), "include": ["Keep", "Partly"]}
    pid = cb.plan(["Protected/only-in-backup.mkv", "GoneShow", "Partly/s2"])
    cb.process()
    r = cb.result(pid)
    assert r["deleted"] == 2  # GoneShow is gone from the source, so it is not protected
    assert (cb.dst / "TV/Protected/only-in-backup.mkv").exists()


def test_items_back_in_the_source_are_kept(cb):
    pid = cb.plan(["GoneShow", "Keep/s1/e1.mkv", "Keep"])
    write_tree(cb.tv, {"GoneShow/e1.mkv": "it came back"})
    cb.process()
    r = cb.result(pid)
    assert r["deleted"] == 0
    assert {s["reason"] for s in r["skipped_items"]} == {"exists in the source again"}
    assert (cb.dst / "TV/GoneShow/e1.mkv").exists() and (cb.dst / "TV/Keep/s1/e1.mkv").exists()


@pytest.mark.parametrize("bad", ["../TV", "/etc", "Keep/../GoneShow", "Keep//x", "./GoneShow", "", "a\0b"])
def test_invalid_paths_are_skipped(cb, bad):
    pid = cb.plan([bad])
    cb.process()
    r = cb.result(pid)
    assert r["deleted"] == 0 and r["skipped_items"][0]["reason"] == "invalid path"
    assert cb.backup() == tree(cb.dst / "TV")  # nothing changed
    assert (cb.dst / "TV/GoneShow").exists()


def test_does_not_follow_symlinked_parent_out_of_backup(cb, tmp_path):
    victim = tmp_path / "victim"
    write_tree(victim, {"precious.txt": "keep me"})
    os.symlink(victim, cb.dst / "TV" / "Sneaky")
    pid = cb.plan(["Sneaky/precious.txt"])
    cb.process()
    r = cb.result(pid)
    assert r["deleted"] == 0 and r["skipped_items"][0]["reason"] == "a parent is not a real folder"
    assert (victim / "precious.txt").read_text() == "keep me"


def test_extraneous_symlink_is_removed_but_not_its_target(cb, tmp_path):
    victim = tmp_path / "victim"
    write_tree(victim, {"precious.txt": "keep me"})
    os.symlink(victim, cb.dst / "TV" / "OldLink")
    pid = cb.plan(["OldLink"])
    cb.process()
    assert cb.result(pid)["deleted"] == 1
    assert not os.path.lexists(cb.dst / "TV" / "OldLink")
    assert (victim / "precious.txt").read_text() == "keep me"


def test_symlink_inside_deleted_folder_does_not_delete_target(cb, tmp_path):
    victim = tmp_path / "victim"
    write_tree(victim, {"precious.txt": "keep me"})
    os.symlink(victim, cb.dst / "TV" / "GoneShow" / "link")
    pid = cb.plan(["GoneShow"])
    cb.process()
    assert cb.result(pid)["deleted"] == 1
    assert (victim / "precious.txt").read_text() == "keep me"


def test_wildcard_names_are_literal(cb):
    write_tree(cb.dst / "TV", {"Show [x]/old.mkv": "old"})
    pid = cb.plan(["Show [x]/old.mkv"])
    cb.process()
    assert cb.result(pid)["deleted"] == 1
    assert (cb.dst / "TV/Show [x]/e.mkv").exists()


def test_already_gone_items_are_skipped(cb):
    pid = cb.plan(["GoneShow", "Nope/never.mkv"])
    import shutil
    shutil.rmtree(cb.dst / "TV/GoneShow")
    cb.process()
    r = cb.result(pid)
    assert r["status"] == "done" and r["deleted"] == 0 and r["skipped"] == 2


# --------------------------------------------------------------------------- refusals
def _refused(cb, pid, fragment):
    r = cb.result(pid)
    assert r["status"] == "failed" and fragment in r["message"], r
    assert r["deleted"] == 0
    assert (cb.dst / "TV/GoneShow").exists()
    assert "refused" in cb.log_text()


def test_refuses_when_source_is_empty(cb):
    """An empty source usually means its disk is not mounted: cleanup would wipe the backup."""
    import shutil
    shutil.rmtree(cb.tv)
    cb.tv.mkdir()
    pid = cb.plan(["GoneShow"] + list(SOURCE))
    cb.process()
    _refused(cb, pid, "is empty")
    assert (cb.dst / "TV/Keep/s1/e1.mkv").exists()


def test_refuses_when_source_is_missing(cb):
    import shutil
    shutil.rmtree(cb.tv)
    pid = cb.plan(["GoneShow"])
    cb.process()
    _refused(cb, pid, "not found")


def test_refuses_stale_plan(cb):
    pid = cb.plan(["GoneShow"], created=time.time() - 3601)
    cb.process()
    _refused(cb, pid, "more than an hour old")


def test_refuses_while_backup_running(cb):
    cb.running_flag.write_text("")
    pid = cb.plan(["GoneShow"])
    cb.process()
    _refused(cb, pid, "backup is running")


def test_refuses_unknown_backup_entry(cb):
    pid = cb.plan(["GoneShow"], destination="/etc")
    cb.process()
    _refused(cb, pid, "no backup entry")


def test_refuses_symlinked_backup_folder(cb, tmp_path):
    other = tmp_path / "dst2"
    other.mkdir()
    os.symlink(cb.dst / "TV", other / "TV")
    cb.add(cb.tv, destination=other)
    pid = cb.plan(["GoneShow"], destination=other)
    cb.process()
    _refused(cb, pid, "not a real backup folder")


def test_refuses_missing_or_garbage_plan(cb):
    cb.request("cleanup-" + "f" * 32, json.dumps({"user": "gozz"}))
    cb.process()
    assert cb.result("f" * 32)["status"] == "failed"
    pid = cb.plan(["GoneShow"])
    (cb.plans / f"{pid}.json").write_text("{not json")
    cb.process()
    _refused(cb, pid, "plan not found or unreadable")


@pytest.mark.parametrize("name", ["cleanup-123", "cleanup-" + "Z" * 32, "cleanup-" + "a" * 33, "cleanup-.." ])
def test_malformed_cleanup_requests_ignored(cb, name):
    cb.request(name, "{}")
    cb.process()
    assert list(cb.plans.glob("*.status.json")) == []
    assert (cb.dst / "TV/GoneShow").exists()


def test_user_is_sanitised(cb):
    pid = cb.plan(["GoneShow"])
    (cb.req / f"cleanup-{pid}").write_text(json.dumps({"user": "$(touch /tmp/pwned)"}))
    cb.process()
    assert cb.result(pid)["user"] == "unknown"
    assert not os.path.exists("/tmp/pwned")


def test_cleanup_allowed_despite_stale_running_flag(cb):
    import subprocess as sp
    p = sp.Popen(["true"])
    p.wait()
    cb.running_flag.write_text(f"{p.pid}\n")
    pid = cb.plan(["GoneShow"])
    cb.process()
    assert cb.result(pid)["status"] == "done"


# --------------------------------------------------------------------------- review 2: malformed plans
@pytest.mark.parametrize("change,fragment", [
    ({"created": None}, "no valid creation time"),
    ({"created": "yesterday"}, "no valid creation time"),
    ({"items": "GoneShow"}, "items must be a list"),
    ({"items": [1, 2]}, "items must be a list"),
    ({"source": None}, "source/destination missing"),
    ({"destination": 5}, "source/destination missing"),
])
def test_malformed_plan_reports_failure_instead_of_crashing(cb, change, fragment):
    """Regression: {"created": null} raised a TypeError, wrote no status file, and the portal
    showed the cleanup as queued forever."""
    pid = cb.plan(["GoneShow"])
    p = cb.plans / f"{pid}.json"
    data = json.loads(p.read_text())
    data.update(change)
    p.write_text(json.dumps(data))
    cb.process()
    r = cb.result(pid)
    assert r["status"] == "failed" and fragment in r["message"]
    assert (cb.dst / "TV/GoneShow").exists()


def test_plan_that_is_not_an_object(cb):
    pid = cb.plan(["GoneShow"])
    (cb.plans / f"{pid}.json").write_text("[1, 2, 3]")
    cb.process()
    assert cb.result(pid)["status"] == "failed"


def test_audit_list_path(cb):
    pid = cb.plan(["GoneShow"])
    cb.process()
    assert (cb.plans / f"{pid}.deleted.txt").read_text() == "GoneShow\n"


# --------------------------------------------------------------------------- review 2: cleanup holds the flag
def _holder(cb, tag):
    """A live process whose command line names `tag`, holding the running flag via cleanup's helper."""
    import subprocess
    import sys
    from helpers import REPO
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import cleanup; "
            "assert cleanup.take_running_flag(sys.argv[2]); print('held', flush=True); time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code, str(REPO / "host"), str(cb.running_flag), tag],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_backup_is_skipped_while_cleanup_holds_the_flag(cb):
    holder = _holder(cb, "host/cleanup.py")
    try:
        assert cb.running_flag.read_text().split() == [str(holder.pid), "cleanup"]
        r = cb.run()
        assert r.returncode == 1 and cb.last_run()["status"] == "skipped"
        cb.request("run")
        cb.process()
        assert "ignored - backup already running" in cb.log_text()
    finally:
        holder.kill()
        holder.wait()
    # once the cleanup is gone its flag is stale and the next backup proceeds
    assert cb.run().returncode == 0 and cb.last_run()["status"] == "success"


def test_cleanup_waits_for_nothing_but_refuses_while_another_cleanup_holds_the_flag(cb):
    holder = _holder(cb, "host/cleanup.py")
    try:
        pid = cb.plan(["GoneShow"])
        cb.process()
        assert cb.result(pid)["status"] == "failed" and "backup is running" in cb.result(pid)["message"]
    finally:
        holder.kill()
        holder.wait()


def test_cleanup_releases_the_flag_and_never_removes_someone_elses(cb):
    pid = cb.plan(["GoneShow"])
    cb.process()
    assert cb.result(pid)["status"] == "done"
    assert not cb.running_flag.exists(), "flag released after the cleanup"
    import importlib.util
    from helpers import REPO
    spec = importlib.util.spec_from_file_location("hc", REPO / "host/cleanup.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    other = f"{os.getpid() + 1000000} cleanup\n"  # not this process (pytest may be PID 1 in a container)
    cb.running_flag.write_text(other)
    hc.release_running_flag(str(cb.running_flag))
    assert cb.running_flag.read_text() == other


def test_flag_is_held_while_deleting(cb, monkeypatch):
    """The running flag must exist (owned by the cleanup) at the moment items are deleted,
    so a scheduled backup can't start mid-cleanup - and be gone afterwards."""
    import importlib.util
    from helpers import REPO
    spec = importlib.util.spec_from_file_location("hc_hold", REPO / "host/cleanup.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    seen = []
    real_rmtree = hc.shutil.rmtree

    def watching_rmtree(path, *a, **kw):
        seen.append(cb.running_flag.read_text() if cb.running_flag.exists() else None)
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(hc.shutil, "rmtree", watching_rmtree)
    cb.write_config()
    pid = cb.plan(["GoneShow"])
    (cb.req / f"cleanup-{pid}").unlink()  # run it directly, not through the bridge
    rc = hc.Run(str(cb.plans / f"{pid}.json"), str(cb.plans / f"{pid}.status.json"), str(cb.config_path),
                str(cb.log), "gozz").main()
    assert rc == 0 and cb.result(pid)["status"] == "done"
    assert seen == [f"{os.getpid()} cleanup\n"], seen
    assert not cb.running_flag.exists()
