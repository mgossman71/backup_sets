"""Backup Folder Cleanup in the portal: the comparison (plan) and the confirmation step."""
import json
import os
import shutil
import time

import pytest

from conftest import HOST, STATE, host_path
from test_api import TV, DISK, requests_written, set_selection

EXTRA = {"Big Show/s1/e3-old.mkv": b"o" * 123, "old.nfo": b"n" * 7}


@pytest.fixture
def extra(host):
    for rel, data in EXTRA.items():
        p = HOST / "mnt/disk1/TV" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def make_plan(client, source=TV, destination=DISK):
    return client.post("/api/cleanup/plan", headers=client.H, json={"source": source, "destination": destination})


def items(r):
    return {i["path"]: (i["type"], i["size"], i["files"]) for i in r.get_json()["items"]}


# --------------------------------------------------------------------------- the comparison
def test_plan_lists_topmost_items_gone_from_source(authed, extra):
    r = make_plan(authed)
    assert r.status_code == 200, r.get_json()
    assert items(r) == {"Big Show/s1/e3-old.mkv": ("file", 123, 1),
                        "Gone Show": ("folder", 250, 1),
                        "old.nfo": ("file", 7, 1)}
    d = r.get_json()
    assert d["total_items"] == 3 and d["total_size"] == 380 and d["total_files"] == 3
    assert d["confirm_name"] == "TV" and d["dest_folder"] == DISK + "/TV" and d["protected"] == []
    assert 0 < d["share_of_backup"] < 0.1
    saved = json.loads((STATE / "cleanup" / f"{d['id']}.json").read_text())
    assert [i["path"] for i in saved["items"]] == ["Big Show/s1/e3-old.mkv", "Gone Show", "old.nfo"]


def test_plan_is_read_only(authed, extra):
    before = sorted(p for p in (HOST / "mnt").rglob("*"))
    make_plan(authed)
    assert sorted(p for p in (HOST / "mnt").rglob("*")) == before


def test_plan_in_sync(authed):
    shutil.rmtree(HOST / "mnt/disk1/TV/Gone Show")
    d = make_plan(authed).get_json()
    assert d["total_items"] == 0 and d["items"] == []


def test_excluded_folders_still_in_source_are_protected(authed, extra):
    set_selection(authed, 0, mode="exclude", folders=["Big Show", "Gone Show"])
    d = make_plan(authed).get_json()
    assert d["protected"] == ["Big Show"]  # Gone Show is not in the source any more
    assert set(items(make_plan(authed))) == {"Gone Show", "old.nfo"}


def test_include_mode_protects_unlisted_folders(authed, extra):
    set_selection(authed, 0, mode="include", folders=["Small Show"])
    d = make_plan(authed).get_json()
    assert d["protected"] == ["Big Show", "New Show", "Show [x]"]
    assert set(items(make_plan(authed))) == {"Gone Show", "old.nfo"}


def test_extraneous_symlink_is_an_item_not_followed(authed):
    os.symlink("/etc", HOST / "mnt/disk1/TV/OldLink")
    it = items(make_plan(authed))
    assert it["OldLink"][0] == "link"


def test_type_change_is_not_deleted(authed):
    """A file in the backup where the source now has a folder of that name: rsync handles it."""
    (HOST / "mnt/src/TV/loose.nfo").unlink()
    (HOST / "mnt/src/TV/loose.nfo").mkdir()
    (HOST / "mnt/src/TV/loose.nfo/x").write_text("x")
    assert "loose.nfo" not in items(make_plan(authed))


@pytest.mark.parametrize("setup,fragment", [
    ("empty_source", "is empty"),
    ("missing_source", "was not found"),
    ("missing_backup", "no backup folder"),
    ("symlink_backup", "symbolic link"),
])
def test_plan_refusals(authed, setup, fragment):
    src, bak = HOST / "mnt/src/TV", HOST / "mnt/disk1/TV"
    if setup == "empty_source":
        shutil.rmtree(src); src.mkdir()
    elif setup == "missing_source":
        shutil.rmtree(src)
    elif setup == "missing_backup":
        shutil.rmtree(bak)
    elif setup == "symlink_backup":
        shutil.move(bak, HOST / "mnt/disk1/TV-real"); os.symlink(HOST / "mnt/disk1/TV-real", bak)
    r = make_plan(authed)
    assert r.status_code == 409 and fragment in r.get_json()["error"]


def test_plan_requires_saved_backup_set(authed):
    r = make_plan(authed, source="/mnt/src/Movies", destination=DISK)
    assert r.status_code == 409 and "save first" in r.get_json()["error"]


def test_plan_requires_login_and_csrf(client, authed):
    assert client.application.test_client().post("/api/cleanup/plan", json={}).status_code in (400, 401)
    assert authed.post("/api/cleanup/plan", json={"source": TV, "destination": DISK}).status_code == 400


# --------------------------------------------------------------------------- confirmation & start
def run(client, plan_id, confirm="TV"):
    return client.post("/api/cleanup/run", headers=client.H, json={"plan_id": plan_id, "confirm": confirm})


@pytest.mark.parametrize("confirm", ["", "tv", "TV ", "Movies", None])
def test_run_needs_exact_folder_name(authed, extra, confirm):
    pid = make_plan(authed).get_json()["id"]
    r = run(authed, pid, confirm)
    assert r.status_code == 400 and "Type TV exactly" in r.get_json()["error"]
    assert requests_written() == {}


def test_run_writes_request_and_never_deletes_itself(authed, extra):
    pid = make_plan(authed).get_json()["id"]
    assert authed.get(f"/api/cleanup/{pid}").get_json()["status"] == "planned"
    r = run(authed, pid)
    assert r.status_code == 200
    assert requests_written() == {f"cleanup-{pid}": json.dumps({"user": "gozz"})}
    assert (HOST / "mnt/disk1/TV/Gone Show").exists(), "only the host deletes"
    assert authed.get(f"/api/cleanup/{pid}").get_json()["status"] == "queued"
    assert run(authed, pid).status_code == 409, "cannot start the same plan twice"


def test_status_comes_from_host(authed, extra):
    pid = make_plan(authed).get_json()["id"]
    (STATE / "cleanup" / f"{pid}.status.json").write_text(json.dumps(
        {"status": "done", "message": "Deleted 3 item(s)", "deleted": 3, "skipped": 0, "freed": 380}))
    s = authed.get(f"/api/cleanup/{pid}").get_json()
    assert s["status"] == "done" and s["deleted"] == 3 and s["total_items"] == 3


def test_run_refusals(authed, extra):
    pid = make_plan(authed).get_json()["id"]
    p = STATE / "cleanup" / f"{pid}.json"
    data = json.loads(p.read_text())
    data["created"] = time.time() - 3601
    p.write_text(json.dumps(data))
    assert run(authed, pid).status_code == 409                      # stale
    pid = make_plan(authed).get_json()["id"]
    host_path("/mnt/.backup_running").write_text("")
    assert run(authed, pid).status_code == 409                      # backup running
    host_path("/mnt/.backup_running").unlink()
    assert run(authed, "f" * 32).status_code == 404                 # unknown plan
    assert run(authed, "../../etc/passwd").status_code == 404       # not a plan id
    shutil.rmtree(HOST / "mnt/disk1/TV/Gone Show")
    for rel in EXTRA:
        (HOST / "mnt/disk1/TV" / rel).unlink()
    empty = make_plan(authed).get_json()["id"]
    assert run(authed, empty).status_code == 409                    # nothing to do
    assert requests_written() == {}


def test_run_refused_after_backup_set_removed(authed, extra):
    pid = make_plan(authed).get_json()["id"]
    cfg = authed.get("/api/config").get_json()
    authed.put("/api/config", headers=authed.H, json={
        "version": cfg["version"], "force": True, "required_mount_points": cfg["required_mount_points"],
        "rsync_opts": cfg["rsync_opts"], "backups": [cfg["backups"][1]]})
    assert run(authed, pid).status_code == 409
