"""Portal test fixtures.

The portal reads its settings from the environment at import time, so the environment
is pointed at a session temp dir *before* the app is imported. Each test then gets a
freshly rebuilt fake host, config file and state directory.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(tempfile.mkdtemp(prefix="portal-tests-"))
HOST = ROOT / "host"
STATE = ROOT / "state"
CONFIG_DIR = ROOT / "opt"
CONFIG = CONFIG_DIR / "config.yaml"

os.environ.update({
    "HOST_ROOT": str(HOST),
    "STATE_DIR": str(STATE),
    "CONFIG_FILE": str(CONFIG),
    "PORTAL_BACKGROUND": "0",
    "ALLOWED_GROUP": "sudo",
    "ALLOWED_USERS": "root",
    "BROWSE_ROOTS": "/mnt",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "portal"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fakehost  # noqa: E402
from app import auth, create_app, folders  # noqa: E402


@pytest.fixture
def host():
    fakehost.build(HOST)
    for d in (STATE, CONFIG_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    CONFIG.write_text(fakehost.CONFIG_TEMPLATE)
    auth._failures.clear()
    folders._schema_done = False
    return HOST


@pytest.fixture
def app(host):
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_of(client):
    client.get("/login")
    with client.session_transaction() as s:
        return s["csrf"]


@pytest.fixture
def authed(client):
    """A client with a logged-in session for gozz (sudo member)."""
    with client.session_transaction() as s:
        s["user"] = "gozz"
        s["checked"] = time.time()
        s["csrf"] = "test-token"
    client.H = {"X-CSRF-Token": "test-token"}
    return client


def host_path(p):
    return HOST / p.lstrip("/")
