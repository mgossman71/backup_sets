"""Render every portal page and the API responses its scripts request, from a fake host,
into a directory used by the jsdom browser tests (tests/js/run.js).

Usage: python tests/js/make_fixtures.py OUT_DIR
"""
import json
import os
import pathlib
import sys
import tempfile
import time

OUT = pathlib.Path(sys.argv[1])
ROOT = pathlib.Path(tempfile.mkdtemp(prefix="js-fixtures-"))
os.environ.update({
    "HOST_ROOT": str(ROOT / "host"), "STATE_DIR": str(ROOT / "state"),
    "CONFIG_FILE": str(ROOT / "opt/config.yaml"), "PORTAL_BACKGROUND": "0", "BROWSE_ROOTS": "/mnt",
})
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "portal"))
sys.path.insert(0, str(HERE.parent / "portal"))

import fakehost  # noqa: E402

fakehost.build(ROOT / "host")
(ROOT / "opt").mkdir(parents=True)
(ROOT / "opt/config.yaml").write_text(fakehost.CONFIG_TEMPLATE)
(ROOT / "state").mkdir()
now = time.time()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))


records = []
for i in range(10):  # ten nightly runs, one failure
    t = now - (10 - i) * 86400
    rid = f"run{i}"
    ok = i != 6
    records.append({"type": "job", "run_id": rid, "source": "/mnt/src/TV", "dest": "/mnt/disk1/TV",
                    "start": iso(t), "end": iso(t + 60 + i), "status": "success" if ok else "failed",
                    "rc": 0 if ok else 23, "reason": None if ok else "Rsync failed",
                    "stats": {"files": 5, "total_size": 8000 + i * 100, "transferred_bytes": i * 1000,
                              "transferred_files": i, "bytes_per_sec": 1e6}})
    records.append({"type": "run", "run_id": rid, "start": iso(t), "end": iso(t + 61 + i),
                    "status": "success" if ok else "failed", "reason": None if ok else "Rsync failed for: x",
                    "trigger": "cron", "jobs_completed": 1 if ok else 0})
(ROOT / "state/history.jsonl").write_text(fakehost.history_lines(records))

from app import create_app, filesystems  # noqa: E402

app = create_app()
app.extensions["history"].ingest()
filesystems.snapshot()
c = app.test_client()
with c.session_transaction() as s:
    s["user"], s["checked"], s["csrf"] = "gozz", time.time(), "T"

OUT.mkdir(parents=True, exist_ok=True)
for name, url in [("dashboard", "/"), ("selections", "/selections"), ("history", "/history"),
                  ("filesystems", "/filesystems")]:
    r = c.get(url)
    assert r.status_code == 200, url
    (OUT / f"{name}.html").write_text(r.get_data(as_text=True))

api = {}
for url in ["/api/status", "/api/log", "/api/filesystems", "/api/filesystems/trend", "/api/config",
            "/api/history/summary?days=30", "/api/history/growth?days=30", "/api/history/runs?days=30",
            "/api/browse?path=/mnt", "/api/removals",
            "/api/folders?source=/mnt/src/TV&destination=/mnt/disk1"]:
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    api[url.split("?")[0]] = r.get_json()
api["/api/history/run"] = c.get("/api/history/run/run9").get_json()

# a hostile folder name: must be shown as text, never interpreted as HTML
api["/api/folders"]["folders"].append({"name": '<img src=x onerror="window.__xss=1">', "in_source": True,
                                       "src_size": 1, "src_files": 1, "on_dest": False, "dest_size": None})
(OUT / "api.json").write_text(json.dumps(api))
print(f"fixtures written to {OUT}")
