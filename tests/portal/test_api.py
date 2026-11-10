"""Portal HTTP API: access control, status, actions, browsing, history, filesystems,
folder sizes and backup-disk removal requests."""
import json
import os
import time

import pytest

import fakehost
from app import filesystems
from conftest import HOST, STATE, host_path

PAGES = ["/", "/selections", "/history", "/filesystems"]
GET_APIS = ["/api/status", "/api/log", "/api/config", "/api/browse?path=/mnt", "/api/history/summary",
            "/api/history/runs", "/api/history/run/x", "/api/history/growth", "/api/filesystems",
            "/api/filesystems/trend", "/api/folders?source=/mnt/src/TV&destination=/mnt/disk1", "/api/removals"]
UNSAFE = [("POST", "/api/action/run"), ("POST", "/api/action/stop_on"), ("PUT", "/api/config"),
          ("POST", "/api/config/validate"), ("POST", "/api/remove-dest-folder")]

TV, DISK = "/mnt/src/TV", "/mnt/disk1"


def requests_written():
    d = STATE / "requests"
    return {p.name: p.read_text() for p in d.iterdir()} if d.exists() else {}


def set_selection(client, idx, **sel):
    cfg = client.get("/api/config").get_json()
    backups = [{k: b[k] for k in ("source", "destination", "mode", "folders", "include_root_files")}
               for b in cfg["backups"]]
    backups[idx].update(sel)
    r = client.put("/api/config", headers=client.H, json={
        "version": cfg["version"], "backups": backups, "force": True,
        "required_mount_points": cfg["required_mount_points"], "rsync_opts": cfg["rsync_opts"]})
    assert r.status_code == 200, r.get_json()


# --------------------------------------------------------------------------- access control
@pytest.mark.parametrize("url", GET_APIS)
def test_api_requires_login(client, url):
    assert client.get(url).status_code == 401


@pytest.mark.parametrize("method,url", UNSAFE)
def test_unsafe_api_requires_login(client, method, url):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    r = client.open(url, method=method, headers={"X-CSRF-Token": token}, json={})
    assert r.status_code == 401
    assert requests_written() == {}


@pytest.mark.parametrize("url", PAGES)
def test_pages_redirect_to_login(client, url):
    r = client.get(url)
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")


@pytest.mark.parametrize("method,url", UNSAFE)
def test_unsafe_requests_need_csrf_token(authed, method, url):
    for headers in ({}, {"X-CSRF-Token": "wrong"}):
        r = authed.open(url, method=method, headers=headers, json={})
        assert r.status_code == 400
    assert requests_written() == {}


@pytest.mark.parametrize("url", PAGES)
def test_pages_render(authed, url):
    r = authed.get(url)
    assert r.status_code == 200
    assert b'name="csrf-token"' in r.data


def test_security_headers(authed):
    h = authed.get("/").headers
    assert h["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "script-src 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("url", PAGES + ["/api/status", "/api/config"])
def test_pages_and_api_are_never_cached(authed, url):
    """After a portal update a normal reload must fetch the new page (and so the new scripts)."""
    assert authed.get(url).headers["Cache-Control"] == "no-store"


def test_scripts_are_version_stamped(authed):
    html = authed.get("/selections").get_data(as_text=True)
    assert "folderpicker.js?v=" in html and "app.js?v=" in html


def test_healthz_is_public(client):
    assert client.get("/healthz").data == b"ok"


# --------------------------------------------------------------------------- status & log
def test_status_reflects_flag_files(authed):
    s = authed.get("/api/status").get_json()
    assert s["flags"] == {"running": None, "stale_running": None, "failed": None, "stopped": None}
    for f in ("running", "failed", "stop"):
        host_path(f"/mnt/.backup_{f}").write_text("")
    s = authed.get("/api/status").get_json()
    assert all(s["flags"][k] for k in ("running", "failed", "stopped"))
    assert s["flags"]["running"]["path"] == "/mnt/.backup_running"


def test_status_schedule_from_root_crontab(authed):
    (sch,) = authed.get("/api/status").get_json()["schedules"]
    assert sch["expr"] == "0 2 * * *"
    nxt = time.localtime(sch["next"])
    assert (nxt.tm_hour, nxt.tm_min) == (2, 0) and 0 < sch["next"] - time.time() <= 86400


def test_status_with_backup_job_commented_out(authed):
    host_path("/var/spool/cron/crontabs/root").write_text("#0 2 * * * bash /opt/backup_sets/backup.sh\n")
    assert authed.get("/api/status").get_json()["schedules"] == []


def test_log_tail_hides_progress_counters(authed):
    lines = authed.get("/api/log").get_json()["lines"]
    assert "[2026-11-09 02:00:09] ERROR: something went wrong" in lines
    assert "Big Show/s1/e1.mkv" in lines
    assert not any("%" in line for line in lines)


def test_log_tail_missing_log(authed):
    host_path("/var/log/backup_sets.log").unlink()
    assert authed.get("/api/log").get_json()["lines"] == []


# --------------------------------------------------------------------------- actions
@pytest.mark.parametrize("name", ["run", "stop_on", "stop_off", "clear_fail"])
def test_actions_write_request_files(authed, name):
    r = authed.post(f"/api/action/{name}", headers=authed.H)
    assert r.status_code == 200
    assert requests_written() == {name: "user=gozz\n"}
    assert not list(STATE.glob(".req-*")), "temp files must be renamed into place"


@pytest.mark.parametrize("name", ["rm", "remove-" + "0" * 32, "..%2Fx", "run;ls"])
def test_unknown_actions_rejected(authed, name):
    assert authed.post(f"/api/action/{name}", headers=authed.H).status_code == 404
    assert requests_written() == {}


def test_run_refused_while_running(authed):
    host_path("/mnt/.backup_running").write_text("")
    assert authed.post("/api/action/run", headers=authed.H).status_code == 409
    assert requests_written() == {}


# --------------------------------------------------------------------------- browse
def test_browse_lists_only_real_directories(authed):
    d = authed.get(f"/api/browse?path={TV}").get_json()
    assert d["dirs"] == ["Big Show", "New Show", "Show [x]", "Small Show"]  # no files, no symlinks
    assert d["parent"] == "/mnt/src"


@pytest.mark.parametrize("path", ["/etc", "/", "/mnt/../etc", "/mntx", "/mnt/src/../../etc"])
def test_browse_outside_roots_forbidden(authed, path):
    assert authed.get(f"/api/browse?path={path}").status_code == 403


def test_browse_missing_dir(authed):
    assert authed.get("/api/browse?path=/mnt/nope").status_code == 404


def test_browse_root_has_no_parent(authed):
    assert authed.get("/api/browse?path=/mnt").get_json()["parent"] is None


# --------------------------------------------------------------------------- config API
def test_config_api_and_save(authed):
    cfg = authed.get("/api/config").get_json()
    assert cfg["backups"][0]["dest_fs"]["mount"] == "/mnt/disk1"
    assert "/mnt/disk1" in cfg["mounts"]
    set_selection(authed, 0, mode="exclude", folders=["Big Show"])
    assert authed.get("/api/config").get_json()["backups"][0]["folders"] == ["Big Show"]


def test_config_save_conflict_and_validation(authed):
    cfg = authed.get("/api/config").get_json()
    body = {"version": "stale", "backups": cfg["backups"], "required_mount_points": [], "rsync_opts": "-a"}
    assert authed.put("/api/config", headers=authed.H, json=body).status_code == 409
    body["version"] = cfg["version"]
    body["backups"] = [{"source": "/mnt/src/Nope", "destination": DISK}]
    r = authed.put("/api/config", headers=authed.H, json=body)
    assert r.status_code == 422 and r.get_json()["errors"]
    r = authed.put("/api/config", headers=authed.H, json=dict(body, backups=cfg["backups"], rsync_opts="-a --delete"))
    assert r.status_code == 422 and r.get_json()["errors"] == [] and r.get_json()["warnings"]


def test_validate_endpoint(authed):
    r = authed.post("/api/config/validate", headers=authed.H, json={
        "backups": [{"source": TV, "destination": DISK, "mode": "include", "folders": []}],
        "required_mount_points": ["/mnt/disk1"], "rsync_opts": "-avessh"}).get_json()
    assert any("no folders are selected" in e for e in r["errors"])
    assert any("-e (--rsh)" in e for e in r["errors"])


# --------------------------------------------------------------------------- history
RUNS = [
    {"type": "job", "run_id": "r1", "source": TV, "dest": DISK + "/TV", "start": "2026-11-01T02:00:00+00:00",
     "end": "2026-11-01T02:01:00+00:00", "status": "success", "rc": 0,
     "stats": {"files": 5, "total_size": 1000, "transferred_bytes": 300, "transferred_files": 2}},
    {"type": "run", "run_id": "r1", "start": "2026-11-01T02:00:00+00:00", "end": "2026-11-01T02:01:00+00:00",
     "status": "success", "trigger": "cron", "jobs_completed": 1},
    {"type": "run", "run_id": "r2", "start": "2026-11-01T02:05:00+00:00", "end": "2026-11-01T02:05:00+00:00",
     "status": "skipped", "reason": "Already running", "trigger": "cron", "jobs_completed": 0},
    {"type": "job", "run_id": "r3", "source": TV, "dest": DISK + "/TV", "start": "2026-11-02T02:00:00+00:00",
     "end": "2026-11-02T02:00:10+00:00", "status": "success", "rc": 0,
     "stats": {"files": 5, "total_size": 1500, "transferred_bytes": 0, "transferred_files": 0}},
    {"type": "run", "run_id": "r3", "start": "2026-11-02T02:00:00+00:00", "end": "2026-11-02T02:00:10+00:00",
     "status": "success", "trigger": "portal", "jobs_completed": 1},
    {"type": "run", "run_id": "r4", "start": "2026-11-03T02:00:00+00:00", "end": "2026-11-03T02:00:01+00:00",
     "status": "failed", "reason": "Mount point not mounted", "trigger": "cron", "jobs_completed": 0},
    {"type": "job", "run_id": "r5", "source": TV, "dest": DISK + "/TV", "start": "2026-11-04T02:00:00+00:00",
     "end": "2026-11-04T02:00:05+00:00", "status": "success", "rc": 0, "stats": {"transferred_bytes": 5}},
]


@pytest.fixture
def with_history(app):
    (STATE / "history.jsonl").write_text(fakehost.history_lines(RUNS))
    app.extensions["history"].ingest()


def test_history_runs_and_filters(authed, with_history):
    d = authed.get("/api/history/runs?days=3650").get_json()
    assert d["total"] == 5
    assert [r["run_id"] for r in d["runs"]] == ["r5", "r4", "r3", "r2", "r1"]
    assert d["runs"][0]["status"] == "incomplete", "a job without a run record and no running flag"
    only = authed.get("/api/history/runs?days=3650&only_changes=1").get_json()
    assert {r["run_id"] for r in only["runs"]} == {"r1", "r2", "r4", "r5"}
    failed = authed.get("/api/history/runs?days=3650&status=failed").get_json()
    assert [r["run_id"] for r in failed["runs"]] == ["r4"]
    assert authed.get("/api/history/runs?status=bogus").status_code == 400


def test_history_in_progress_run_shows_running(authed, with_history):
    host_path("/mnt/.backup_running").write_text("")
    assert authed.get("/api/history/run/r5").get_json()["status"] == "running"


def test_history_pagination(authed, with_history):
    p1 = authed.get("/api/history/runs?days=3650&per_page=10&page=1").get_json()
    p2 = authed.get("/api/history/runs?days=3650&per_page=10&page=2").get_json()
    assert len(p1["runs"]) == 5 and p2["runs"] == []


def test_history_run_detail(authed, with_history):
    r = authed.get("/api/history/run/r1").get_json()
    assert r["duration"] == 60 and r["jobs"][0]["transferred_bytes"] == 300
    assert authed.get("/api/history/run/nope").status_code == 404


def test_history_summary_and_daily(authed, with_history):
    d = authed.get("/api/history/summary?days=3650").get_json()
    assert d["summary"] == {"runs": 5, "success": 2, "failed": 1, "skipped": 1, "bytes": 305, "files": 2}
    days = {x["day"]: x for x in d["daily"]}
    assert days["2026-11-01"]["n"] == 2 and days["2026-11-01"]["bytes"] == 300
    assert days["2026-11-01"]["max_duration"] == 60


def test_history_growth(authed, with_history):
    s = authed.get("/api/history/growth?days=3650").get_json()["series"]
    assert s[TV] == [["2026-11-01", 1000], ["2026-11-02", 1500]]


def test_history_ingest_is_incremental_and_skips_partial_lines(app, with_history):
    h = app.extensions["history"]
    path = STATE / "history.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps({"type": "run", "run_id": "r6", "start": "2026-11-05T02:00:00+00:00",
                            "end": "2026-11-05T02:00:01+00:00", "status": "success"}) + "\n")
        f.write('{"type": "run", "run_id": "r7", "sta')  # being written right now
    h.ingest()
    total, _ = h.runs(days=3650)
    assert total == 6
    with open(path, "a") as f:
        f.write('rt": "2026-11-06T02:00:00+00:00", "status": "success"}\n')
    h.ingest()
    assert h.runs(days=3650)[0] == 7


def test_history_ignores_corrupt_lines(app):
    (STATE / "history.jsonl").write_text("not json\n" + fakehost.history_lines(RUNS[:2]) + "{bad\n")
    app.extensions["history"].ingest()
    assert app.extensions["history"].runs(days=3650)[0] == 1


def test_history_reingests_after_file_replaced(app, with_history):
    h = app.extensions["history"]
    path = STATE / "history.jsonl"
    path.unlink()
    path.write_text(fakehost.history_lines([RUNS[1]]))
    h.ingest()
    assert h.runs(days=3650)[0] == 5  # records are upserted, never duplicated


# --------------------------------------------------------------------------- filesystems
def test_filesystems_lists_real_mounts_with_roles(authed):
    fs = {f["mount"]: f for f in authed.get("/api/filesystems").get_json()["filesystems"]}
    assert set(fs) == {"/", "/mnt/src", "/mnt/disk1", "/mnt/disk2"}  # proc and overlay skipped
    assert fs["/mnt/disk1"]["roles"] == ["destination"] and fs["/mnt/disk1"]["required"]
    assert fs["/mnt/src"]["roles"] == ["source"]
    assert fs["/mnt/disk1"]["backups"] == [TV]
    st = os.statvfs(HOST / "mnt/disk1")  # free space can shift slightly between the two reads
    assert abs(fs["/mnt/disk1"]["avail"] - st.f_bavail * st.f_frsize) < 256 * 2**20
    assert fs["/mnt/disk1"]["size"] == st.f_blocks * st.f_frsize


@pytest.mark.parametrize("pct,level", [(None, "unknown"), (0, "good"), (84.9, "good"), (85, "warning"),
                                       (94.9, "warning"), (95, "critical"), (100, "critical")])
def test_fullness_levels(pct, level):
    assert filesystems.level(pct) == level


def test_mount_for_picks_longest_prefix():
    fs = [{"mount": "/"}, {"mount": "/mnt/disk1"}, {"mount": "/mnt/disk10"}]
    assert filesystems.mount_for("/mnt/disk10/TV", fs)["mount"] == "/mnt/disk10"
    assert filesystems.mount_for("/mnt/disk1", fs)["mount"] == "/mnt/disk1"
    assert filesystems.mount_for("/mnt/disk1x", fs)["mount"] == "/"


def test_snapshot_records_every_mounted_filesystem(app):
    from app.history import connect
    filesystems.snapshot()
    with connect() as c:
        mounts = {r[0] for r in c.execute("SELECT mount FROM fs_snapshots")}
    assert mounts == {"/", "/mnt/src", "/mnt/disk1", "/mnt/disk2"}


def test_snapshots_and_growth_projection(authed, app):
    from app.history import connect
    day = 86400
    with connect() as c:  # 10 GiB/day growth over 4 days on disk1
        c.executemany("INSERT INTO fs_snapshots VALUES(?,?,?,?,?)",
                      [(time.time() - (4 - i) * day, "/mnt/disk1", 10**12, 10**11 + i * 10 * 2**30, 0)
                       for i in range(5)])
    rate = filesystems.snapshot_growth_rate("/mnt/disk1")
    assert abs(rate - 10 * 2**30) < 2**20
    fs = {f["mount"]: f for f in authed.get("/api/filesystems").get_json()["filesystems"]}
    assert fs["/mnt/disk1"]["growth_basis"] == "usage snapshots"
    assert abs(fs["/mnt/disk1"]["days_to_full"] - fs["/mnt/disk1"]["avail"] / rate) < 1e-6
    assert authed.get("/api/filesystems/trend?days=7").get_json()["series"]["/mnt/disk1"]


# --------------------------------------------------------------------------- folders
def test_folder_sizes(authed):
    d = authed.get(f"/api/folders?source={TV}&destination={DISK}").get_json()
    f = {x["name"]: x for x in d["folders"]}
    assert set(f) == {"Big Show", "Small Show", "Show [x]", "New Show", "Gone Show"}  # symlink not listed
    assert f["Big Show"] == {"name": "Big Show", "in_source": True, "src_size": 8000, "src_files": 2,
                             "on_dest": True, "dest_size": 8000}
    assert f["New Show"]["on_dest"] is False and f["New Show"]["dest_size"] is None
    assert f["Gone Show"]["in_source"] is False and f["Gone Show"]["dest_size"] == 250
    assert d["loose"] == {"count": 1, "size": 42}
    assert d["dest_folder"] == DISK + "/TV" and d["partial"] is False
    assert d["saved_selection"] == {"mode": "all", "folders": [], "include_root_files": True}
    assert d["dest_fs"]["mount"] == "/mnt/disk1"


def test_folder_sizes_cached_until_refresh(authed):
    url = f"/api/folders?source={TV}&destination={DISK}"
    authed.get(url)
    (HOST / "mnt/src/TV/Big Show/extra.mkv").write_bytes(b"e" * 1000)
    size = lambda d: next(x for x in d["folders"] if x["name"] == "Big Show")["src_size"]  # noqa: E731
    assert size(authed.get(url).get_json()) == 8000
    assert size(authed.get(url + "&refresh=1").get_json()) == 9000


def test_folders_unsaved_entry_has_no_saved_selection(authed):
    d = authed.get(f"/api/folders?source=/mnt/src/Movies&destination={DISK}").get_json()
    assert d["saved_selection"] is None


@pytest.mark.parametrize("q,code", [("source=/etc&destination=/mnt/disk1", 400),
                                    ("source=/mnt/src/TV&destination=/tmp", 400),
                                    ("source=mnt/src/TV&destination=/mnt/disk1", 400),
                                    ("source=/mnt/src/Nope&destination=/mnt/disk1", 404)])
def test_folders_bad_paths(authed, q, code):
    assert authed.get(f"/api/folders?{q}").status_code == code


# --------------------------------------------------------------------------- removal requests
def remove(client, folder, source=TV, destination=DISK):
    return client.post("/api/remove-dest-folder", headers=client.H,
                       json={"source": source, "destination": destination, "folder": folder})


def test_remove_refused_while_selected(authed):
    r = remove(authed, "Big Show")
    assert r.status_code == 409 and "still selected" in r.get_json()["error"]
    assert requests_written() == {}


def test_remove_deselected_folder_writes_request(authed):
    set_selection(authed, 0, mode="exclude", folders=["Big Show"])
    r = remove(authed, "Big Show")
    assert r.status_code == 200
    rid = r.get_json()["id"]
    req = requests_written()
    assert list(req) == [f"remove-{rid}"]
    assert json.loads(req[f"remove-{rid}"]) == {"source": TV, "destination": DISK, "folder": "Big Show",
                                                "user": "gozz"}
    (queued,) = authed.get("/api/removals").get_json()["removals"]
    assert queued["status"] == "queued" and queued["folder"] == "Big Show"
    assert (HOST / "mnt/disk1/TV/Big Show").exists(), "the portal itself never deletes anything"


def test_remove_orphan_allowed_without_deselecting(authed):
    assert remove(authed, "Gone Show").status_code == 200


def test_remove_in_include_mode(authed):
    set_selection(authed, 0, mode="include", folders=["Small Show"])
    assert remove(authed, "Small Show").status_code == 409
    assert remove(authed, "Big Show").status_code == 200


@pytest.mark.parametrize("folder", ["", ".", "..", "../TV", "a/b", "x\ny"])
def test_remove_invalid_names(authed, folder):
    assert remove(authed, folder).status_code == 400
    assert requests_written() == {}


def test_remove_unsaved_entry(authed):
    r = remove(authed, "Film A", source="/mnt/src/Movies", destination=DISK)
    assert r.status_code == 409 and "save first" in r.get_json()["error"]


def test_remove_while_running(authed):
    set_selection(authed, 0, mode="exclude", folders=["Big Show"])
    host_path("/mnt/.backup_running").write_text("")
    assert remove(authed, "Big Show").status_code == 409


def test_remove_symlink_or_missing(authed):
    os.symlink("/etc", HOST / "mnt/disk1/TV/Evil")
    set_selection(authed, 0, mode="exclude", folders=["Evil", "Ghost"])
    assert remove(authed, "Evil").status_code == 404
    assert remove(authed, "Ghost").status_code == 404
    assert requests_written() == {}


def test_removal_status_from_host(authed):
    d = STATE / "removals"
    d.mkdir()
    (d / ("a" * 32 + ".json")).write_text(json.dumps({"id": "a" * 32, "status": "done", "folder": "Big Show",
                                                      "source": TV, "destination": DISK, "updated": 2}))
    (d / ("b" * 32 + ".json")).write_text(json.dumps({"id": "b" * 32, "status": "failed", "folder": "X",
                                                      "source": TV, "destination": DISK, "updated": 1}))
    (d / "garbage.json").write_text("{not json")
    r = authed.get("/api/removals").get_json()["removals"]
    assert [x["status"] for x in r] == ["done", "failed"]
    f = authed.get(f"/api/folders?source={TV}&destination={DISK}").get_json()
    assert len(f["removals"]) == 2


# --------------------------------------------------------------------------- stale running flag
def test_stale_running_flag_is_not_reported_as_running(authed):
    host_path("/mnt/.backup_running").write_text("99999999\n")  # no such process on the fake host
    s = authed.get("/api/status").get_json()
    assert s["flags"]["running"] is None
    assert s["flags"]["stale_running"]["pid"] == "99999999"
    assert authed.post("/api/action/run", headers=authed.H).status_code == 200


def test_running_flag_of_live_backup_is_running(authed):
    (HOST / "proc/4242").mkdir(parents=True)
    (HOST / "proc/4242/cmdline").write_bytes(b"bash\0/opt/backup_sets/backup.sh\0")
    host_path("/mnt/.backup_running").write_text("4242\n")
    s = authed.get("/api/status").get_json()
    assert s["flags"]["running"] and s["flags"]["stale_running"] is None
    assert authed.post("/api/action/run", headers=authed.H).status_code == 409


def test_history_status_filter_accepts_stopped(authed):
    assert authed.get("/api/history/runs?status=stopped").status_code == 200


# --------------------------------------------------------------------------- browser tab icon
@pytest.mark.parametrize("url", ["/login"] + PAGES)
def test_every_page_links_the_tab_icon(authed, url):
    html = (authed.get(url) if url != "/login" else authed.application.test_client().get(url)).get_data(as_text=True)
    assert 'rel="icon" href="/static/favicon.svg' in html and 'type="image/svg+xml"' in html
    assert 'rel="icon" href="/static/favicon-32.png' in html
    assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png' in html
    assert 'class="brand-mark" src="/static/favicon.svg' in html


@pytest.mark.parametrize("path,mime", [("/static/favicon.svg", "image/svg+xml"), ("/static/favicon-32.png", "image/png"),
                                       ("/static/apple-touch-icon.png", "image/png"), ("/favicon.ico", "image/png")])
def test_icon_files_are_served_without_login(client, path, mime):
    r = client.get(path)
    assert r.status_code == 200 and r.mimetype == mime and len(r.data) > 500


def test_png_icons_have_the_right_sizes(client):
    import struct
    for path, size in [("/static/favicon-32.png", 32), ("/static/apple-touch-icon.png", 180)]:
        data = client.get(path).data
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (size, size)
