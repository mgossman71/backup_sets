"""Pages and JSON API."""
import functools
import hmac
import secrets
import time
from urllib.parse import urlsplit

from werkzeug.exceptions import HTTPException
from flask import (Blueprint, abort, current_app, jsonify, redirect, render_template, request,
                   session, url_for)

import json
import os
import uuid

from . import auth, cleanup, config_store, filesystems, folders, schedule, status
from .settings import BROWSE_ROOTS, STATE_DIR, host_now, host_timezone, hostpath

bp = Blueprint("portal", __name__)
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
REAUTH_SECONDS = 300
ASSET_V = int(time.time())  # cache-buster for static files


def history():
    return current_app.extensions["history"]


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@bp.app_context_processor
def inject():
    return {"csrf_token": csrf_token, "user": session.get("user"), "asset_v": ASSET_V}


@bp.before_app_request
def check_csrf():
    if request.method in UNSAFE:
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
        expected = session.get("csrf") or ""
        # both must be present: compare_digest("", "") is True, so an empty session token
        # (no session at all) would otherwise let a token-less request through
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            abort(400, "CSRF token missing or invalid - reload the page")


@bp.app_errorhandler(HTTPException)
def http_error(e):
    """API callers get JSON errors (the page scripts show error.message); pages keep HTML."""
    if request.path.startswith("/api/"):
        return jsonify(error=e.description), e.code
    return e


def safe_next(nxt):
    """Only redirect to a path on this site. Browsers treat '\\' like '/' and drop tabs and
    newlines from URLs, so '/\\evil.com' or '/<TAB>/evil.com' would leave the site."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
        return None
    if "\\" in nxt or any(ord(c) < 32 or ord(c) == 127 for c in nxt):
        return None
    parts = urlsplit(nxt)
    if parts.scheme or parts.netloc:
        return None
    return nxt


def login_required(api=False):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            user = session.get("user")
            if user and time.time() - session.get("checked", 0) > REAUTH_SECONDS:
                # account removed from the allowed group, locked, etc.
                if not auth.is_authorized(user) or auth.shadow_entry(user) is None:
                    session.clear()
                    user = None
                else:
                    session["checked"] = time.time()
            if not user:
                if api:
                    return jsonify(error="not logged in"), 401
                return redirect(url_for("portal.login", next=request.path))
            return fn(*a, **kw)
        return wrapper
    return deco


def _cfg():
    return config_store.load()


# ------------------------------------------------------------------ auth
@bp.get("/healthz")
def healthz():
    return "ok"


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        user = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if auth.rate_limited(ip):
            error = "Too many failed attempts. Try again in a few minutes."
        elif auth.check_password(user, password) and auth.is_authorized(user):
            auth.clear_failures(ip)
            session.clear()
            session.permanent = True
            session["user"] = user
            session["checked"] = time.time()
            print(f"login ok: {user} from {ip}", flush=True)
            nxt = request.args.get("next", "")
            return redirect(safe_next(nxt) or url_for("portal.dashboard"))
        else:
            auth.record_failure(ip)
            print(f"login failed: {user!r} from {ip}", flush=True)
            error = "Invalid username or password, or the account is not allowed to use the portal."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("portal.login"))


# ------------------------------------------------------------------ pages
@bp.get("/")
@login_required()
def dashboard():
    return render_template("dashboard.html", page="dashboard")


@bp.get("/selections")
@login_required()
def selections():
    return render_template("selections.html", page="selections")


@bp.get("/history")
@login_required()
def history_page():
    return render_template("history.html", page="history")


@bp.get("/filesystems")
@login_required()
def filesystems_page():
    return render_template("filesystems.html", page="filesystems")


# ------------------------------------------------------------------ API: status
def _fix_running(run, running_flag):
    if run and run.get("status") == "running" and not running_flag:
        run["status"] = "incomplete"
    return run


@bp.get("/api/status")
@login_required(api=True)
def api_status():
    cfg = _cfg()
    h = history()
    h.ingest(min_interval=5)
    fl = status.flags(cfg)
    tail = status.log_tail(cfg.get("log_file"), lines=400)
    last = h.latest_runs(1)
    last = h.run_detail(last[0]["run_id"]) if last else None
    return jsonify(
        flags=fl,
        schedules=status.schedules(),
        pending_actions=[a for a in status.pending_actions() if a in status.ACTIONS],
        activity=status.current_activity(tail) if fl["running"] else None,
        last_run=_fix_running(last, fl["running"]),
        last_success=h.last_with_status("success"),
        last_failure=h.last_with_status("failed"),
        summary=h.summary(30),
        indexing=h.indexing,
        server_time=time.time(),
    )


@bp.get("/api/log")
@login_required(api=True)
def api_log():
    n = min(max(request.args.get("lines", 200, type=int), 10), 2000)
    return jsonify(lines=status.log_tail(_cfg().get("log_file"), lines=n))


@bp.post("/api/action/<name>")
@login_required(api=True)
def api_action(name):
    if name not in status.ACTIONS:
        abort(404)
    cfg = _cfg()
    if name == "run" and status.flags(cfg)["running"]:
        return jsonify(error="A backup is already running"), 409
    status.request_action(name, session["user"])
    print(f"action {name} requested by {session['user']}", flush=True)
    return jsonify(ok=True)


# ------------------------------------------------------------------ API: config
@bp.get("/api/config")
@login_required(api=True)
def api_config():
    cfg = _cfg()
    fs = filesystems.usage()
    latest = history().latest_by_source()
    for b in cfg["backups"]:
        dest_fs = filesystems.mount_for(b["destination"], fs)
        src_fs = filesystems.mount_for(b["source"], fs)
        b["dest_fs"] = _fs_brief(dest_fs)
        b["source_fs"] = _fs_brief(src_fs)
        j = latest.get(b["source"])
        b["last"] = ({"total_size": j["total_size"], "files": j["files"], "end": j["end"],
                      "transferred_bytes": j["transferred_bytes"]} if j else None)
        b["growth_per_day"] = history().source_growth_rate(b["source"]) if j else None
    cfg["mounts"] = [m[1] for m in filesystems.mounts()]
    cfg["backup_copies"] = config_store.list_backup_copies()[:10]
    return jsonify(cfg)


def _fs_brief(e):
    if not e or not e.get("ok"):
        return None
    return {"mount": e["mount"], "avail": e["avail"], "size": e["size"], "pct": e["pct"],
            "level": filesystems.level(e["pct"])}


@bp.put("/api/config")
@login_required(api=True)
def api_config_save():
    body = request.get_json(silent=True) or {}
    try:
        version, warnings = config_store.save(body, body.get("version", ""), user=session["user"],
                                              force=bool(body.get("force")))
    except config_store.ValidationError as e:
        return jsonify(errors=e.errors, warnings=e.warnings), 422
    except config_store.ConflictError as e:
        return jsonify(errors=[str(e)]), 409
    print(f"config.yaml saved by {session['user']}", flush=True)
    return jsonify(ok=True, version=version, warnings=warnings)


@bp.post("/api/config/validate")
@login_required(api=True)
def api_config_validate():
    body = request.get_json(silent=True) or {}
    backups = config_store.normalize_backups(body.get("backups", []))
    mps = [config_store.norm_path(m) for m in body.get("required_mount_points", []) if str(m).strip()]
    errors, warnings = config_store.validate(backups, mps, " ".join(str(body.get("rsync_opts", "")).split()))
    return jsonify(errors=errors, warnings=warnings)


def _in_browse_roots(path):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in BROWSE_ROOTS)


def _saved_entry(cfg, source, destination):
    return next((b for b in cfg["backups"] if b["source"] == source and b["destination"] == destination), None)


@bp.get("/api/folders")
@login_required(api=True)
def api_folders():
    source = config_store.norm_path(request.args.get("source", ""))
    destination = config_store.norm_path(request.args.get("destination", ""))
    for p in (source, destination):
        if not p.startswith("/") or not _in_browse_roots(p):
            return jsonify(error=f"paths must be inside {', '.join(BROWSE_ROOTS)}"), 400
    recent = [r for r in status.removals(50) if r.get("source") == source and r.get("destination") == destination]
    for r in recent:  # sizes of folders being / just deleted must not come from the cache
        if r.get("target") and r.get("status") in ("running", "done") and time.time() - (r.get("updated") or 0) < 86400:
            folders.invalidate(r["target"])
    try:
        data = folders.scan(source, destination, refresh=request.args.get("refresh") == "1")
    except FileNotFoundError:
        return jsonify(error=f"Source folder not found: {source}"), 404
    cfg = _cfg()
    saved = _saved_entry(cfg, source, destination)
    data["saved_selection"] = ({"mode": saved["mode"], "folders": saved["folders"],
                                "include_root_files": saved["include_root_files"]} if saved else None)
    data["dest_fs"] = _fs_brief(filesystems.mount_for(destination, filesystems.usage()))
    data["removals"] = recent
    return jsonify(data)


def _removable(saved, source, folder):
    """Same rule the host bridge enforces: deselected in the saved config, or gone from the source."""
    if saved["mode"] == "exclude" and folder in saved["folders"]:
        return True
    if saved["mode"] == "include" and folder not in saved["folders"]:
        return True
    return not os.path.lexists(hostpath(os.path.join(source, folder)))


@bp.post("/api/remove-dest-folder")
@login_required(api=True)
def api_remove_dest_folder():
    body = request.get_json(silent=True) or {}
    source = config_store.norm_path(body.get("source", ""))
    destination = config_store.norm_path(body.get("destination", ""))
    folder = str(body.get("folder", ""))
    if not config_store.valid_folder_name(folder):
        return jsonify(error="Invalid folder name"), 400
    cfg = _cfg()
    saved = _saved_entry(cfg, source, destination)
    if not saved:
        return jsonify(error="That backup entry isn't saved in config.yaml yet - save first"), 409
    if not _removable(saved, source, folder):
        return jsonify(error="This folder is still selected for backup. Deselect it and save first."), 409
    if status.flags(cfg)["running"]:
        return jsonify(error="A backup is running - try again when it finishes"), 409
    target = os.path.join(destination, os.path.basename(source), folder)
    tp = hostpath(target)
    if os.path.islink(tp) or not os.path.isdir(tp):
        return jsonify(error=f"Not a folder on the backup disk: {target}"), 404
    rid = status.request_removal(source, destination, folder, session["user"])
    folders.invalidate(target)
    print(f"removal of {target} requested by {session['user']} ({rid})", flush=True)
    return jsonify(ok=True, id=rid)


@bp.post("/api/cleanup/plan")
@login_required(api=True)
def api_cleanup_plan():
    """Backup Folder Cleanup step 1: compare the backup folder with the source (read-only)."""
    body = request.get_json(silent=True) or {}
    source = config_store.norm_path(body.get("source", ""))
    destination = config_store.norm_path(body.get("destination", ""))
    saved = _saved_entry(_cfg(), source, destination)
    if not saved:
        return jsonify(error="That backup set isn't saved in config.yaml yet - save first"), 409
    try:
        p = cleanup.plan(saved)
    except cleanup.CleanupError as e:
        return jsonify(error=str(e)), 409
    print(f"cleanup plan {p['id']} for {p['dest_folder']} by {session['user']}: "
          f"{p['total_items']} items, {p['total_size']} bytes", flush=True)
    return jsonify(cleanup.summary(p))


@bp.post("/api/cleanup/run")
@login_required(api=True)
def api_cleanup_run():
    """Step 2: after the user typed the backup set's folder name, hand the plan to the host."""
    body = request.get_json(silent=True) or {}
    try:
        p = cleanup.load_plan(str(body.get("plan_id", "")))
    except cleanup.CleanupError as e:
        return jsonify(error=str(e)), 404
    if body.get("confirm") != p["confirm_name"]:
        return jsonify(error=f"Type {p['confirm_name']} exactly to confirm"), 400
    if time.time() - p["created"] > cleanup.PLAN_MAX_AGE:
        return jsonify(error="This comparison is more than an hour old - run it again"), 409
    if not p["items"]:
        return jsonify(error="Nothing to clean up"), 409
    cfg = _cfg()
    if not _saved_entry(cfg, p["source"], p["destination"]):
        return jsonify(error="That backup set is no longer in config.yaml"), 409
    if status.flags(cfg)["running"]:
        return jsonify(error="A backup is running - try again when it finishes"), 409
    if cleanup.status(p["id"]) or f"cleanup-{p['id']}" in status.pending_actions():
        return jsonify(error="This cleanup was already started"), 409
    status.request_action(f"cleanup-{p['id']}", session["user"], {})
    print(f"cleanup {p['id']} of {p['dest_folder']} confirmed by {session['user']}", flush=True)
    return jsonify(ok=True, id=p["id"])


@bp.get("/api/cleanup/<plan_id>")
@login_required(api=True)
def api_cleanup_status(plan_id):
    try:
        p = cleanup.load_plan(plan_id)
    except cleanup.CleanupError as e:
        return jsonify(error=str(e)), 404
    st = cleanup.status(plan_id)
    if st is None:
        st = ({"status": "queued", "message": "Waiting for the server"}
              if f"cleanup-{plan_id}" in status.pending_actions() else {"status": "planned"})
    return jsonify(dict(st, id=plan_id, total_items=p["total_items"], total_size=p["total_size"]))


def _schedule_status():
    try:
        with open(os.path.join(STATE_DIR, "schedule_status.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _schedule_view():
    cron = schedule.read_crontab()
    m = cron["managed"]
    view = {"enabled": bool(m and m["enabled"]), "expr": m["expr"] if m else None,
            "managed": m is not None, "unmanaged": cron["unmanaged"], "broken": cron["broken"],
            "pending": "schedule" in status.pending_actions(), "last_status": _schedule_status(),
            "timezone": host_now().strftime("%Z"), "timezone_name": host_timezone(),
            "server_time": time.time()}
    if m and not schedule.validate(m["expr"]):
        view["form"] = schedule.describe(m["expr"])
        view["next_runs"] = schedule.next_runs(m["expr"]) if m["enabled"] else []
    else:
        view["form"] = {"kind": "daily", "time": "02:00", "text": "Daily at 02:00"}
        view["next_runs"] = []
    return view


def _expr_from(body):
    try:
        expr = schedule.from_preset(body)
    except ValueError as e:
        return None, [str(e)]
    errors = schedule.validate(expr)
    return (None, errors) if errors else (expr, [])


@bp.get("/api/schedule")
@login_required(api=True)
def api_schedule():
    return jsonify(_schedule_view())


@bp.post("/api/schedule/preview")
@login_required(api=True)
def api_schedule_preview():
    expr, errors = _expr_from(request.get_json(silent=True) or {})
    if errors:
        return jsonify(errors=errors), 422
    return jsonify(expr=expr, form=schedule.describe(expr), next_runs=schedule.next_runs(expr))


@bp.put("/api/schedule")
@login_required(api=True)
def api_schedule_save():
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify(errors=["Choose whether automatic backups are on or off"]), 422
    expr, errors = _expr_from(body)
    if errors:
        return jsonify(errors=errors), 422
    rid = uuid.uuid4().hex
    status.request_action("schedule", session["user"], {"id": rid, "enabled": body["enabled"], "expr": expr})
    print(f"schedule change requested by {session['user']}: enabled={body['enabled']} expr={expr!r}", flush=True)
    return jsonify(ok=True, id=rid, expr=expr)


@bp.get("/api/removals")
@login_required(api=True)
def api_removals():
    return jsonify(removals=status.removals())


@bp.get("/api/browse")
@login_required(api=True)
def api_browse():
    try:
        return jsonify(status.browse(request.args.get("path", "")))
    except PermissionError as e:
        return jsonify(error=str(e)), 403
    except (FileNotFoundError, NotADirectoryError):
        return jsonify(error="No such directory"), 404
    except OSError as e:
        return jsonify(error=str(e)), 500


# ------------------------------------------------------------------ API: history
def _days():
    return min(max(request.args.get("days", 30, type=int), 1), 3650)


@bp.get("/api/history/summary")
@login_required(api=True)
def api_hist_summary():
    history().ingest(min_interval=5)
    return jsonify(summary=history().summary(_days()), daily=history().daily(_days()),
                   indexing=history().indexing)


@bp.get("/api/history/runs")
@login_required(api=True)
def api_hist_runs():
    per = min(max(request.args.get("per_page", 50, type=int), 10), 500)
    page = max(request.args.get("page", 1, type=int), 1)
    st = request.args.get("status") or None
    if st not in (None, "success", "failed", "skipped", "stopped", "incomplete", "running"):
        abort(400)
    total, runs = history().runs(days=_days(), status=st,
                                 only_changes=request.args.get("only_changes") == "1",
                                 limit=per, offset=(page - 1) * per)
    running = status.flags(_cfg())["running"]
    return jsonify(total=total, page=page, per_page=per, runs=[_fix_running(r, running) for r in runs])


@bp.get("/api/history/run/<run_id>")
@login_required(api=True)
def api_hist_run(run_id):
    run = history().run_detail(run_id)
    if not run:
        abort(404)
    return jsonify(_fix_running(run, status.flags(_cfg())["running"]))


@bp.get("/api/history/growth")
@login_required(api=True)
def api_hist_growth():
    sources = {b["source"] for b in _cfg()["backups"]}
    return jsonify(series=history().source_growth(days=_days(), sources=sources))


# ------------------------------------------------------------------ API: filesystems
@bp.get("/api/filesystems")
@login_required(api=True)
def api_filesystems():
    cfg = _cfg()
    fs = filesystems.usage()
    for e in fs:
        e["level"] = filesystems.level(e.get("pct"))
        e["roles"] = []
        e["backups"] = []
        e["required"] = e["mount"] in cfg["required_mount_points"]
    for b in cfg["backups"]:
        d = filesystems.mount_for(b["destination"], fs)
        s = filesystems.mount_for(b["source"], fs)
        if d:
            d["backups"].append(b["source"])
            if "destination" not in d["roles"]:
                d["roles"].append("destination")
        if s and "source" not in s["roles"]:
            s["roles"].append("source")
    for e in fs:
        if not e.get("ok"):
            continue
        rate, basis = filesystems.snapshot_growth_rate(e["mount"]), "usage snapshots"
        if rate is None and e["backups"]:
            rates = [r for r in (history().source_growth_rate(src) for src in e["backups"]) if r is not None]
            if rates:
                rate, basis = sum(rates), "growth of the sources backed up here"
        e["growth_per_day"] = rate
        e["growth_basis"] = basis if rate is not None else None
        e["days_to_full"] = (e["avail"] / rate) if rate and rate > 0 else None
    return jsonify(filesystems=fs, server_time=time.time())


@bp.get("/api/filesystems/trend")
@login_required(api=True)
def api_fs_trend():
    return jsonify(series=filesystems.trend(_days()))
