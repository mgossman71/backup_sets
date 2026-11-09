"""Backup Sets web portal."""
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import Flask

from . import filesystems
from .history import History
from .settings import SESSION_HOURS, STATE_DIR


def _secret_key():
    path = os.path.join(STATE_DIR, "secret_key")
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def _ingest_loop(history):
    while True:
        try:
            history.ingest()
        except Exception as e:  # keep the thread alive
            print(f"history ingest error: {e}", flush=True)
        time.sleep(30)


def create_app():
    app = Flask(__name__)
    tls = os.path.exists(os.path.join(STATE_DIR, "tls", "cert.pem"))
    app.config.update(
        SECRET_KEY=_secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=tls,
        SESSION_COOKIE_NAME="backup_portal",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )

    history = History()
    app.extensions["history"] = history
    threading.Thread(target=_ingest_loop, args=(history,), name="history-ingest", daemon=True).start()
    filesystems.start_snapshot_thread()

    from .routes import bp
    app.register_blueprint(bp)

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'")
        if tls:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    return app
