"""Pages and JSON API."""
import functools
import hmac
import secrets
import time

from flask import (Blueprint, abort, current_app, jsonify, redirect, render_template, request,
                   session, url_for)

from . import auth, config_store, filesystems, status

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
        if not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "CSRF token missing or invalid - reload the page")


def login_required(api=False):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            user = session.get("user")
            if user and time.time() - session.get("checked", 0) > REAUTH_SECONDS:
                # account removed from the allowed group, locked, etc.
                if not auth.is_authorized(user) or auth._shadow_entry(user) is None:
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
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("portal.dashboard"))
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
        pending_actions=status.pending_actions(),
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
    backups = [{"source": config_store._norm(b.get("source")),
                "destination": config_store._norm(b.get("destination"))} for b in body.get("backups", [])]
    mps = [config_store._norm(m) for m in body.get("required_mount_points", []) if str(m).strip()]
    errors, warnings = config_store.validate(backups, mps, " ".join(str(body.get("rsync_opts", "")).split()))
    return jsonify(errors=errors, warnings=warnings)


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
    if st not in (None, "success", "failed", "skipped", "incomplete", "running"):
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
