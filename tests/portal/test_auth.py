"""Login against host accounts, who may sign in, rate limiting, sessions."""
import time

import pytest

import fakehost
from conftest import HOST, csrf_of


def login(client, user, password, ip="10.0.0.5", next_=None):
    token = csrf_of(client)
    url = "/login" + (f"?next={next_}" if next_ else "")
    return client.post(url, data={"username": user, "password": password, "csrf": token},
                       environ_base={"REMOTE_ADDR": ip})


def logged_in_as(client):
    with client.session_transaction() as s:
        return s.get("user")


def test_sudo_member_with_correct_password_signs_in(client):
    r = login(client, "gozz", fakehost.PASSWORDS["gozz"])
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert logged_in_as(client) == "gozz"
    assert client.get("/api/status").status_code == 200


@pytest.mark.parametrize("user,password,why", [
    ("gozz", "wrong password", "wrong password"),
    ("gozz", "", "empty password"),
    ("bob", fakehost.PASSWORDS["bob"], "valid account, not in the allowed group"),
    ("locked", fakehost.PASSWORDS["locked"], "locked account"),
    ("expired", fakehost.PASSWORDS["expired"], "expired account"),
    ("nopw", "", "account without a password"),
    ("nopw", "anything", "account without a password"),
    ("root", "", "root is locked on this server"),
    ("root", "anything", "root is locked on this server"),
    ("nosuchuser", "x", "unknown user"),
    ("GOZZ", fakehost.PASSWORDS["gozz"], "usernames are case-sensitive"),
    ("gozz:x", "x", "invalid characters"),
    ("../../etc/shadow", "x", "invalid characters"),
])
def test_rejected_logins(client, user, password, why):
    r = login(client, user, password)
    assert r.status_code == 200, why
    assert b"Invalid username or password" in r.data
    assert logged_in_as(client) is None


def test_root_can_sign_in_once_it_has_a_password(client):
    (HOST / "etc/shadow").write_text(fakehost.shadow_text({"root": fakehost._hash("root pw")}))
    r = login(client, "root", "root pw")
    assert r.status_code == 302
    assert logged_in_as(client) == "root"


def test_password_change_on_host_applies_immediately(client):
    (HOST / "etc/shadow").write_text(fakehost.shadow_text({"gozz": fakehost._hash("new password")}))
    assert login(client, "gozz", fakehost.PASSWORDS["gozz"]).status_code == 200
    assert login(client, "gozz", "new password").status_code == 302


def test_login_requires_csrf_token(client):
    client.get("/login")
    r = client.post("/login", data={"username": "gozz", "password": fakehost.PASSWORDS["gozz"]})
    assert r.status_code == 400
    assert logged_in_as(client) is None


def test_rate_limit_blocks_even_correct_password(client):
    for _ in range(5):
        login(client, "gozz", "nope", ip="10.9.9.9")
    r = login(client, "gozz", fakehost.PASSWORDS["gozz"], ip="10.9.9.9")
    assert b"Too many failed attempts" in r.data
    assert logged_in_as(client) is None


def test_rate_limit_is_per_ip(client):
    for _ in range(5):
        login(client, "gozz", "nope", ip="10.9.9.9")
    assert login(client, "gozz", fakehost.PASSWORDS["gozz"], ip="10.1.1.1").status_code == 302


def test_rate_limit_window_expires(client, monkeypatch):
    for _ in range(5):
        login(client, "gozz", "nope", ip="10.9.9.9")
    real = time.time
    monkeypatch.setattr("app.auth.time.time", lambda: real() + 301)
    assert login(client, "gozz", fakehost.PASSWORDS["gozz"], ip="10.9.9.9").status_code == 302


def test_successful_login_resets_failure_count(client):
    for _ in range(4):
        login(client, "gozz", "nope", ip="10.2.2.2")
    assert login(client, "gozz", fakehost.PASSWORDS["gozz"], ip="10.2.2.2").status_code == 302
    client.post("/logout", data={"csrf": csrf_of(client)})
    for _ in range(4):
        login(client, "gozz", "nope", ip="10.2.2.2")
    assert login(client, "gozz", fakehost.PASSWORDS["gozz"], ip="10.2.2.2").status_code == 302


@pytest.mark.parametrize("nxt,expected", [
    ("/history", "/history"),
    ("//evil.example.com/", "/"),
    ("https://evil.example.com/", "/"),
])
def test_next_redirect_stays_on_site(client, nxt, expected):
    r = login(client, "gozz", fakehost.PASSWORDS["gozz"], next_=nxt)
    assert r.headers["Location"] == expected


def test_logout(client):
    login(client, "gozz", fakehost.PASSWORDS["gozz"])
    client.post("/logout", data={"csrf": csrf_of(client)})
    assert logged_in_as(client) is None
    assert client.get("/api/status").status_code == 401


def test_session_dropped_when_user_leaves_allowed_group(authed, monkeypatch):
    assert authed.get("/api/status").status_code == 200
    (HOST / "etc/group").write_text("root:x:0:\nsudo:x:27:\n")
    with authed.session_transaction() as s:
        s["checked"] = time.time() - 301  # due for re-validation
    assert authed.get("/api/status").status_code == 401


def test_session_dropped_when_account_deleted(authed):
    (HOST / "etc/shadow").write_text(fakehost.shadow_text().replace("gozz:", "zzzz:"))
    with authed.session_transaction() as s:
        s["checked"] = time.time() - 301
    assert authed.get("/").status_code == 302


def test_forged_session_cookie_rejected(client, app):
    other = app.test_client()
    client.set_cookie("backup_portal", "eyJ1c2VyIjoiZ296eiJ9.forged.signature")
    assert client.get("/api/status").status_code == 401
    assert other.get("/api/status").status_code == 401
