"""host/actions.sh: portal requests (run / pause / resume / clear) and the backup-disk
folder removal safety rules. systemd-run is replaced by a stub that runs the command
synchronously, so every effect can be checked right after the script returns."""
import json
import os
import pathlib
import subprocess

import pytest

from helpers import ACTIONS_SH, BackupEnv, tree, write_tree

STUB = """#!/bin/bash
# test stand-in for systemd-run: apply --setenv, ignore other options, run synchronously
echo "ARGS $*" >> "$STUB_LOG.args"
while [ $# -gt 0 ]; do
  case "$1" in
    --setenv=*) kv="${1#--setenv=}"; export "${kv%%=*}"="${kv#*=}" ;;
    --*) ;;
    *) break ;;
  esac
  shift
done
echo "$*" >> "$STUB_LOG"
"$@"
"""


class Bridge(BackupEnv):
    def __init__(self, root):
        super().__init__(root)
        self.req = self.root / "requests"
        self.removals = self.root / "removals"
        self.cron_log = self.root / "cron.log"
        self.stub_log = self.root / "stub.log"
        self.req.mkdir()
        stub = self.root / "systemd-run"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.stub = stub
        self.n = 0
        # A TV source with a backup copy already on the destination
        self.tv = self.src / "TV"
        shows = {"Keep/e.mkv": "k", "Drop/e.mkv": "d", "Show [x]/e.mkv": "x", "Show x/e.mkv": "y"}
        write_tree(self.tv, shows)
        write_tree(self.dst / "TV", shows)
        write_tree(self.dst / "TV", {"Orphan/e.mkv": "o"})

    def request(self, name, content="user=gozz\n"):
        (self.req / name).write_text(content)

    def remove_request(self, folder, source=None, destination=None, user="gozz", raw=None):
        self.n += 1
        rid = f"{self.n:032x}"
        body = raw if raw is not None else json.dumps(
            {"user": user, "source": str(source or self.tv), "destination": str(destination or self.dst),
             "folder": folder})
        self.request(f"remove-{rid}", body)
        return rid

    def process(self):
        self.write_config()
        env = self.env(REQUEST_DIR=self.req, REMOVAL_DIR=self.removals, CRON_LOG=self.cron_log,
                       SYSTEMD_RUN=self.stub, STUB_LOG=self.stub_log, STATE_DIR=self.root)
        r = subprocess.run(["bash", str(ACTIONS_SH)], env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def status(self, rid):
        return json.loads((self.removals / f"{rid}.json").read_text())

    def dest_names(self):
        return set(os.listdir(self.dst / "TV"))


@pytest.fixture
def br(tmp_path):
    b = Bridge(tmp_path)
    b.add(b.tv, exclude=["Drop", "Show [x]"])
    return b


# --------------------------------------------------------------------------- simple actions
def test_run_starts_backup_with_portal_trigger(br):
    br.request("run")
    br.process()
    assert br.last_run()["trigger"] == "portal"
    assert br.last_run()["status"] == "success"
    assert "backup.sh" in br.stub_log.read_text()
    assert "run requested by gozz" in br.log_text()
    assert list(br.req.iterdir()) == []


def test_run_ignored_while_backup_running(br):
    br.running_flag.write_text("")
    br.request("run")
    br.process()
    assert not br.stub_log.exists()
    assert "ignored - backup already running" in br.log_text()


def test_stop_on_off_and_clear_fail(br):
    br.request("stop_on")
    br.process()
    assert br.stop_flag.exists()
    br.request("stop_off")
    br.process()
    assert not br.stop_flag.exists()
    br.fail_flag.write_text("")
    br.request("clear_fail")
    br.process()
    assert not br.fail_flag.exists()
    log = br.log_text()
    assert "stop flag set by gozz" in log and "stop flag cleared by gozz" in log and "fail flag cleared by gozz" in log


@pytest.mark.parametrize("name", ["rm -rf", "bogus", "remove-123", "remove-" + "g" * 32, "run2"])
def test_unknown_or_malformed_requests_ignored(br, name):
    br.request(name)
    br.process()
    assert list(br.req.iterdir()) == []
    assert not br.stub_log.exists()
    assert br.dest_names() == {"Keep", "Drop", "Show [x]", "Show x", "Orphan"}


def test_request_user_is_sanitised(br):
    br.request("stop_on", "user=$(touch /tmp/pwned)\n")
    br.process()
    assert "stop flag set by unknown" in br.log_text()
    assert not os.path.exists("/tmp/pwned")


def test_directory_in_request_dir_is_discarded(br):
    (br.req / "run").mkdir()
    br.process()
    assert list(br.req.iterdir()) == []
    assert not br.stub_log.exists()


def test_several_requests_in_one_pass(br):
    br.request("stop_on")
    rid = br.remove_request("Drop")
    br.process()
    assert br.stop_flag.exists()
    assert br.status(rid)["status"] == "done"


# --------------------------------------------------------------------------- removal: allowed
def test_remove_excluded_folder(br):
    before_src = tree(br.tv)
    rid = br.remove_request("Drop")
    br.process()
    st = br.status(rid)
    assert st["status"] == "done", st
    assert st["user"] == "gozz" and st["folder"] == "Drop"
    assert br.dest_names() == {"Keep", "Show [x]", "Show x", "Orphan"}
    assert tree(br.tv) == before_src, "the source must never be touched"
    assert "removal of" in br.log_text() and "done (requested by gozz)" in br.log_text()


def test_remove_name_with_wildcards_removes_only_that_folder(br):
    rid = br.remove_request("Show [x]")
    br.process()
    assert br.status(rid)["status"] == "done"
    assert "Show x" in br.dest_names() and "Show [x]" not in br.dest_names()


def test_remove_orphan_folder(br):
    br.backups[0].pop("exclude")  # even with everything selected
    rid = br.remove_request("Orphan")
    br.process()
    assert br.status(rid)["status"] == "done"
    assert "Orphan" not in br.dest_names()


def test_remove_not_included_folder_in_include_mode(br):
    br.backups[0] = {"source": str(br.tv), "destination": str(br.dst), "include": ["Keep"]}
    rid = br.remove_request("Drop")
    br.process()
    assert br.status(rid)["status"] == "done"


# --------------------------------------------------------------------------- removal: refused
def _refused(br, rid, fragment):
    st = br.status(rid)
    assert st["status"] == "failed", st
    assert fragment in st["message"], st["message"]
    assert "refused" in br.log_text()


def test_refuses_selected_folder(br):
    rid = br.remove_request("Keep")
    br.process()
    _refused(br, rid, "still selected")
    assert "Keep" in br.dest_names()


def test_refuses_included_folder(br):
    br.backups[0] = {"source": str(br.tv), "destination": str(br.dst), "include": ["Keep"]}
    rid = br.remove_request("Keep")
    br.process()
    _refused(br, rid, "still selected")


def test_refuses_when_everything_is_selected(br):
    br.backups[0].pop("exclude")
    rid = br.remove_request("Drop")
    br.process()
    _refused(br, rid, "still selected")
    assert "Drop" in br.dest_names()


@pytest.mark.parametrize("folder", ["", ".", "..", "../TV", "Drop/../Keep", "a/b", "Drop\nKeep"])
def test_refuses_invalid_names(br, folder):
    rid = br.remove_request(folder)
    br.process()
    _refused(br, rid, "invalid folder name")
    assert br.dest_names() == {"Keep", "Drop", "Show [x]", "Show x", "Orphan"}


def test_refuses_unparseable_request(br):
    rid = br.remove_request(None, raw="this is not json")
    br.process()
    _refused(br, rid, "invalid folder name")


def test_refuses_unknown_backup_entry(br):
    rid = br.remove_request("Drop", destination="/etc")
    br.process()
    _refused(br, rid, "no backup entry")
    rid = br.remove_request("Drop", source=br.src / "Other")
    br.process()
    _refused(br, rid, "no backup entry")
    assert "Drop" in br.dest_names()


def test_refuses_while_backup_running(br):
    br.running_flag.write_text("")
    rid = br.remove_request("Drop")
    br.process()
    _refused(br, rid, "backup is running")
    assert "Drop" in br.dest_names()


def test_refuses_symlink_and_keeps_its_target(br, tmp_path):
    victim = tmp_path / "victim"
    write_tree(victim, {"important": "keep me"})
    os.symlink(victim, br.dst / "TV" / "Evil")
    br.backups[0]["exclude"].append("Evil")
    rid = br.remove_request("Evil")
    br.process()
    _refused(br, rid, "not a folder on the backup disk")
    assert (victim / "important").read_text() == "keep me"
    assert (br.dst / "TV" / "Evil").is_symlink()


def test_symlinked_backup_folder_removes_only_inside_its_target(br, tmp_path):
    """If dest/TV is itself a symlink, rsync writes the backup through it, so the copy
    really lives at the link target. Removal follows it - and touches nothing else."""
    elsewhere = tmp_path / "elsewhere"
    write_tree(elsewhere, {"Drop/e.mkv": "copy", "Keep/e.mkv": "copy"})
    other_dst = tmp_path / "dst2"
    other_dst.mkdir()
    os.symlink(elsewhere, other_dst / "TV")
    br.add(br.tv, destination=other_dst, exclude=["Drop"])
    rid = br.remove_request("Drop", destination=other_dst)
    br.process()
    assert br.status(rid)["status"] == "done"
    assert set(os.listdir(elsewhere)) == {"Keep"}
    assert (br.dst / "TV" / "Drop").exists(), "the other backup entry's copy is untouched"
    assert (br.tv / "Drop" / "e.mkv").exists(), "the source is untouched"


def test_refuses_file_instead_of_folder(br):
    (br.dst / "TV" / "notes.txt").write_text("x")
    br.backups[0]["exclude"].append("notes.txt")
    rid = br.remove_request("notes.txt")
    br.process()
    _refused(br, rid, "not a folder on the backup disk")
    assert (br.dst / "TV" / "notes.txt").exists()


def test_refuses_missing_folder(br):
    br.backups[0]["exclude"].append("Ghost")
    rid = br.remove_request("Ghost")
    br.process()
    _refused(br, rid, "not a folder on the backup disk")


def test_unsaved_deselection_is_not_enough(br):
    """The host checks the saved config.yaml, not what the portal claims."""
    rid = br.remove_request("Keep", raw=json.dumps({"user": "gozz", "source": str(br.tv),
                                                    "destination": str(br.dst), "folder": "Keep",
                                                    "mode": "exclude", "exclude": ["Keep"]}))
    br.process()
    _refused(br, rid, "still selected")


# --------------------------------------------------------------------------- stale running flag
def _dead_pid():
    import subprocess as sp
    p = sp.Popen(["true"])
    p.wait()
    return p.pid


def test_run_starts_despite_stale_running_flag(br):
    br.running_flag.write_text(f"{_dead_pid()}\n")
    br.request("run")
    br.process()
    assert br.last_run()["status"] == "success"
    assert "Removing stale running flag" in br.log_text()


def test_removal_allowed_despite_stale_running_flag(br):
    br.running_flag.write_text(f"{_dead_pid()}\n")
    rid = br.remove_request("Drop")
    br.process()
    assert br.status(rid)["status"] == "done"


def test_run_unit_names_are_unique_within_a_second(br):
    """Regression: units were named backup-sets-portal-<epoch seconds>, so two runs started in
    the same second collided and the second one silently failed to start."""
    for _ in range(3):
        br.request("run")
        br.process()
    units = [a for line in (br.root / "stub.log.args").read_text().splitlines()
             for a in line.split() if a.startswith("--unit=backup-sets-portal-")]
    assert len(units) == 3 and len(set(units)) == 3
