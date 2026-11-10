"""Regression tests for the second code review's findings (portal side)."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import fakehost
from app import auth, cleanup, config_store as cs, filesystems, folders, schedule
from conftest import CONFIG, HOST, STATE, csrf_of

PORTAL = os.path.join(os.path.dirname(__file__), "..", "..", "portal")


def login(client, nxt, ip="10.0.0.5"):
    token = csrf_of(client)
    return client.post("/login?next=" + nxt, data={"username": "gozz", "password": fakehost.PASSWORDS["gozz"],
                                                   "csrf": token}, environ_base={"REMOTE_ADDR": ip})


# --------------------------------------------------------------------------- #1 open redirect
@pytest.mark.parametrize("nxt", ["/%5Cevil.example.com", "/%5C%5Cevil.example.com", "/%09/evil.example.com",
                                 "/%0A/evil.example.com", "%2F%2Fevil.example.com", "/%5C/evil.example.com"])
def test_login_redirect_never_leaves_the_site(client, nxt):
    r = login(client, nxt)
    assert r.status_code == 302
    assert r.headers["Location"] == "/", f"{nxt!r} redirected to {r.headers['Location']!r}"


def test_login_redirect_to_a_local_path_still_works(client):
    assert login(client, "/history?days=90").headers["Location"] == "/history?days=90"


# --------------------------------------------------------------------------- #2 CSRF with no session
@pytest.mark.parametrize("method,url", [("POST", "/login"), ("POST", "/logout"), ("POST", "/api/action/run"),
                                        ("PUT", "/api/config"), ("POST", "/api/cleanup/plan")])
def test_unsafe_request_without_session_or_token_is_rejected(client, method, url):
    """compare_digest('', '') is True: an empty session token must not count as a match."""
    r = client.open(url, method=method, data={"username": "gozz", "password": fakehost.PASSWORDS["gozz"]})
    assert r.status_code == 400


def test_login_csrf_from_another_site_is_rejected(client):
    """A cross-site form post (no session cookie, no token) must not log the browser in."""
    r = client.post("/login", data={"username": "gozz", "password": fakehost.PASSWORDS["gozz"], "csrf": ""})
    assert r.status_code == 400
    with client.session_transaction() as s:
        assert "user" not in s


def test_api_errors_are_json(authed):
    r = authed.post("/api/action/run", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 400 and r.is_json
    assert "CSRF" in r.get_json()["error"]
    r = authed.get("/api/history/run/nope")
    assert r.status_code == 404 and r.is_json


def test_page_errors_stay_html(client):
    r = client.post("/logout")
    assert r.status_code == 400 and not r.is_json


# --------------------------------------------------------------------------- #3 concurrent saves
def test_concurrent_saves_one_wins_file_stays_valid(host, monkeypatch):
    cfg = cs.load()
    body = lambda folders: {"backups": [dict(b) for b in cfg["backups"]], "required_mount_points": cfg["required_mount_points"],  # noqa: E731
                            "rsync_opts": cfg["rsync_opts"]}
    # widen the window between the version check and the write
    real_copy = cs._write_backup_copy
    monkeypatch.setattr(cs, "_write_backup_copy", lambda *a: (time.sleep(0.3), real_copy(*a)))
    results = []

    def save(folder):
        b = body(folder)
        b["backups"][0].update(mode="exclude", folders=[folder])
        try:
            cs.save(b, cfg["version"], force=True)
            results.append(("ok", folder))
        except cs.ConflictError:
            results.append(("conflict", folder))

    threads = [threading.Thread(target=save, args=(n,)) for n in ("Big Show", "Small Show")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r[0] for r in results) == ["conflict", "ok"]
    winner = next(r[1] for r in results if r[0] == "ok")
    assert cs.load()["backups"][0]["folders"] == [winner]
    assert not [n for n in os.listdir(CONFIG.parent) if n.endswith(".tmp")]


# --------------------------------------------------------------------------- #4 hung mount
def test_hung_mount_does_not_exhaust_the_pool(host, monkeypatch):
    release = threading.Event()
    calls = {"disk1": 0}
    real = filesystems._statvfs

    def statvfs(mp):
        if mp == "/mnt/disk1":
            calls["disk1"] += 1
            release.wait(30)  # hangs like a dead NFS/FUSE mount
        return real(mp)

    monkeypatch.setattr(filesystems, "_statvfs", statvfs)
    monkeypatch.setattr(filesystems, "STATVFS_TIMEOUT", 0.3)
    filesystems.reset_cache()
    try:
        started = time.time()
        for _ in range(20):
            fs = {e["mount"]: e for e in filesystems.usage(max_age=0)}
            assert fs["/mnt/disk1"]["ok"] is False and fs["/mnt/disk1"]["error"] == "not responding"
            assert fs["/mnt/disk2"]["ok"] and fs["/mnt/src"]["ok"], "other mounts keep working"
        assert calls["disk1"] == 1, "a stuck mount is not re-submitted"
        assert time.time() - started < 5, "only the first call waits for the stuck mount"
    finally:
        release.set()
        filesystems.reset_cache()


def test_usage_is_cached_between_calls(host, monkeypatch):
    n = {"calls": 0}
    real = filesystems._statvfs
    monkeypatch.setattr(filesystems, "_statvfs", lambda mp: (n.__setitem__("calls", n["calls"] + 1), real(mp))[1])
    filesystems.reset_cache()
    filesystems.usage()
    first = n["calls"]
    filesystems.usage()
    filesystems.usage()
    assert n["calls"] == first, "repeat calls within the cache window don't re-measure"
    filesystems.usage(max_age=0)
    assert n["calls"] == 2 * first


def test_callers_cannot_corrupt_the_cache(host):
    filesystems.reset_cache()
    a = filesystems.usage()
    a[0]["mount"] = "changed"
    a[0]["roles"] = ["x"]
    assert "roles" not in filesystems.usage()[0] and filesystems.usage()[0]["mount"] != "changed"


# --------------------------------------------------------------------------- #5 folder scan caps
def test_folder_scan_caps_walks_per_request(host, monkeypatch):
    for i in range(30):
        fakehost.write_tree(HOST / f"mnt/src/TV/Extra {i:02d}", {"e.mkv": b"x"})
    monkeypatch.setattr(folders, "MAX_WALKS_PER_REQUEST", 5)
    d = folders.scan("/mnt/src/TV", "/mnt/disk1")
    sized = [f for f in d["folders"] if f["in_source"] and f["src_size"] is not None]
    assert len(sized) <= 5 and d["partial"] is True
    for _ in range(20):  # later calls measure the rest
        d = folders.scan("/mnt/src/TV", "/mnt/disk1")
    assert d["partial"] is False


def test_folder_scan_with_many_folders(host):
    for i in range(1200):
        (HOST / f"mnt/src/TV/Many {i:04d}").mkdir(parents=True)
    d = folders.scan("/mnt/src/TV", "/mnt/disk1")
    assert sum(1 for f in d["folders"] if f["in_source"]) == 1204
    assert folders.scan("/mnt/src/TV", "/mnt/disk1")["partial"] is False  # served from the chunked cache query


# --------------------------------------------------------------------------- #6, #15 cleanup plans
def test_cleanup_plan_deadline_is_short():
    import inspect
    assert inspect.signature(cleanup.plan).parameters["deadline_s"].default <= 60


def test_old_cleanup_plans_are_pruned(host):
    os.makedirs(cleanup.PLAN_DIR, exist_ok=True)
    old, new = os.path.join(cleanup.PLAN_DIR, "a" * 32 + ".json"), os.path.join(cleanup.PLAN_DIR, "b" * 32 + ".json")
    for p in (old, new):
        with open(p, "w") as f:
            f.write("{}")
    t = time.time() - 31 * 86400
    os.utime(old, (t, t))
    assert cleanup.prune_old_plans() == 1
    assert not os.path.exists(old) and os.path.exists(new)


def test_old_fs_snapshots_are_pruned(app):
    from app.history import connect
    with connect() as c:
        c.execute("INSERT INTO fs_snapshots VALUES(?,?,?,?,?)", (time.time() - 800 * 86400, "/mnt/disk1", 1, 1, 0))
        c.execute("INSERT INTO fs_snapshots VALUES(?,?,?,?,?)", (time.time() - 10 * 86400, "/mnt/disk1", 1, 1, 0))
    filesystems.snapshot()
    with connect() as c:
        ages = [r[0] for r in c.execute("SELECT ts FROM fs_snapshots WHERE mount='/mnt/disk1'")]
    assert len(ages) == 2 and min(ages) > time.time() - 700 * 86400


# --------------------------------------------------------------------------- #16 crypt, #17 failure table
def test_missing_crypt_module_fails_loudly_at_startup():
    code = ("import sys; sys.modules['crypt'] = None; sys.path.insert(0, sys.argv[1]);\n"
            "try:\n import app.auth\nexcept ImportError as e:\n print('ERR', e)")
    r = subprocess.run([sys.executable, "-c", code, PORTAL], capture_output=True, text=True,
                       env=dict(os.environ, HOST_ROOT="/nonexistent"))
    assert "ERR" in r.stdout and "Python 3.12" in r.stdout, r.stdout + r.stderr


def test_failure_table_is_pruned_globally(host, monkeypatch):
    for i in range(500):
        auth.record_failure(f"10.1.{i // 250}.{i % 250}")
    assert len(auth._failures) == 500
    real = time.time
    monkeypatch.setattr("app.auth.time.time", lambda: real() + auth.WINDOW_SECONDS + 1)
    auth.rate_limited("10.9.9.9")
    assert len(auth._failures) <= 1


# --------------------------------------------------------------------------- #21 duplicate schedule blocks
def test_two_schedule_blocks_are_reported_broken():
    blk = f"{schedule.BEGIN}\n0 2 * * * bash /opt/backup_sets/backup.sh\n{schedule.END}\n"
    assert schedule.parse_crontab(blk + blk)["broken"] is True
    assert schedule.parse_crontab(f"{schedule.END}\n")["broken"] is True
    assert schedule.parse_crontab(blk)["broken"] is False


# --------------------------------------------------------------------------- P3: public helpers
def test_helpers_used_across_modules_are_public():
    assert callable(cs.norm_path) and callable(auth.shadow_entry)
    src = open(os.path.join(PORTAL, "app", "routes.py")).read()
    assert "._norm(" not in src and "._shadow_entry(" not in src
