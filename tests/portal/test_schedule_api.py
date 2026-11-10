"""Backup schedule in the portal: presets <-> cron, validation, reading the crontab block,
and the request sent to the host."""
import importlib.util
import json
import pathlib
import time
from datetime import datetime, timezone

import pytest

from app import schedule
from conftest import STATE, host_path
from test_api import requests_written

HOST_SCHEDULE = pathlib.Path(__file__).resolve().parents[2] / "host" / "schedule.py"
spec = importlib.util.spec_from_file_location("host_schedule", HOST_SCHEDULE)
host_schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_schedule)

CMD = "bash /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1"


def set_crontab(text):
    host_path("/var/spool/cron/crontabs/root").write_text(text)


def block(expr, enabled=True):
    return f"{schedule.BEGIN}\n{'' if enabled else '#'}{expr} {CMD}\n{schedule.END}\n"


# --------------------------------------------------------------------------- presets
@pytest.mark.parametrize("body,expr,text", [
    ({"kind": "daily", "time": "02:00"}, "0 2 * * *", "Daily at 02:00"),
    ({"kind": "daily", "time": "23:59"}, "59 23 * * *", "Daily at 23:59"),
    ({"kind": "weekdays", "time": "03:15", "days": [5, 1, 3, 1]}, "15 3 * * 1,3,5", "Mon, Wed, Fri at 03:15"),
    ({"kind": "weekdays", "time": "01:00", "days": [0]}, "0 1 * * 0", "Sun at 01:00"),
    ({"kind": "weekdays", "time": "01:00", "days": [0, 1, 2, 3, 4, 5, 6]}, "0 1 * * *", "Daily at 01:00"),
    ({"kind": "hours", "every": 6, "minute": 30}, "30 */6 * * *", "Every 6 hours at :30"),
    ({"kind": "hours", "every": 1, "minute": 0}, "0 * * * *", "Every hour"),
    ({"kind": "hours", "every": 1, "minute": 5}, "5 * * * *", "Every hour at :05"),
    ({"kind": "advanced", "expr": "  0  4  1  *  * "}, "0 4 1 * *", "Custom schedule (0 4 1 * *)"),
])
def test_presets_round_trip(body, expr, text):
    assert schedule.from_preset(body) == expr
    d = schedule.describe(expr)
    assert d["text"] == text
    if d["kind"] != "advanced":
        assert schedule.from_preset(dict(d)) == expr, "describe() fills the form so it saves the same schedule"


@pytest.mark.parametrize("body", [
    {"kind": "daily", "time": "24:00"}, {"kind": "daily", "time": "2am"}, {"kind": "daily"},
    {"kind": "weekdays", "time": "02:00", "days": []}, {"kind": "weekdays", "time": "02:00", "days": [7]},
    {"kind": "weekdays", "time": "02:00", "days": ["x"]},
    {"kind": "hours", "every": 5, "minute": 0}, {"kind": "hours", "every": 6, "minute": 60},
    {"kind": "nope"}, {},
])
def test_bad_presets(body):
    with pytest.raises(ValueError):
        schedule.from_preset(body)


VALID = ["0 2 * * *", "*/15 * * * *", "0 0-6/2 * * 1-5", "5,35 1 1,15 * *", "0 2 * * 7", "59 23 31 12 6"]
INVALID = ["0 2 * * * ; rm -rf /", "0 2 * * *\n* * * * * x", "@reboot", "@daily", "0 2 * *", "0 2 * * * *",
           "0  2 * * *", "60 2 * * *", "0 24 * * *", "0 2 0 * *", "0 2 * 13 *", "0 2 * * 8", "*/0 * * * *",
           "5-1 * * * *", "0 2 * * MON", "0\n 2 * * *", "0 2\n * * *", "$(id) * * * *", "0 2 * * *\r", "", " 0 2 * * *", "0 2 * * * "]


@pytest.mark.parametrize("expr", VALID)
def test_valid_expressions(expr):
    assert schedule.validate(expr) == []
    assert host_schedule.validate(expr) is True


@pytest.mark.parametrize("expr", INVALID)
def test_invalid_expressions_rejected_by_portal_and_host(expr):
    assert schedule.validate(expr), expr
    assert host_schedule.validate(expr) is False, "the host must reject everything the portal rejects"


def test_next_runs():
    start = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)
    runs = schedule.next_runs("0 2 * * *", 3, now=start)
    assert [datetime.fromtimestamp(t, timezone.utc).strftime("%d %H:%M") for t in runs] == \
        ["10 02:00", "11 02:00", "12 02:00"]


# --------------------------------------------------------------------------- reading the crontab
def test_parse_crontab():
    other = "#0 2 * * * bash /opt/backup_sets/backup.sh\n30 4 * * 0 /usr/bin/other\n"
    assert schedule.parse_crontab(other) == {"managed": None, "unmanaged": [], "broken": False}
    stray = "0 3 * * * bash /opt/backup_sets/backup.sh >> x\n"
    assert schedule.parse_crontab(stray)["unmanaged"] == [stray.strip()]
    assert schedule.parse_crontab(other + block("15 3 * * 1"))["managed"] == {"enabled": True, "expr": "15 3 * * 1"}
    assert schedule.parse_crontab(block("15 3 * * 1", False))["managed"] == {"enabled": False, "expr": "15 3 * * 1"}
    assert schedule.parse_crontab(f"{schedule.BEGIN}\n0 2 * * * {CMD}\n")["broken"] is True


def test_get_schedule_with_old_style_cron_line(authed):
    s = authed.get("/api/schedule").get_json()  # fake host has an active, unmanaged backup.sh line
    assert s["managed"] is False and s["enabled"] is False
    assert s["unmanaged"] == ["0 2 * * * bash /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1"]
    assert s["form"]["kind"] == "daily" and s["next_runs"] == []


def test_get_schedule_with_no_schedule(authed):
    set_crontab("#0 2 * * * bash /opt/backup_sets/backup.sh\n")
    s = authed.get("/api/schedule").get_json()
    assert s["managed"] is False and s["enabled"] is False and s["unmanaged"] == []


def test_get_schedule_managed_enabled(authed):
    set_crontab("MAILTO=\"\"\n" + block("30 */6 * * *"))
    s = authed.get("/api/schedule").get_json()
    assert s["managed"] and s["enabled"] and s["expr"] == "30 */6 * * *"
    assert s["form"] == {"kind": "hours", "every": 6, "minute": 30, "text": "Every 6 hours at :30"}
    assert len(s["next_runs"]) == 5 and s["next_runs"][0] > time.time()
    st = authed.get("/api/status").get_json()["schedules"]
    assert [x["expr"] for x in st] == ["30 */6 * * *"], "the dashboard sees the managed line"


def test_get_schedule_managed_disabled(authed):
    set_crontab(block("0 2 * * *", enabled=False))
    s = authed.get("/api/schedule").get_json()
    assert s["managed"] and not s["enabled"] and s["next_runs"] == []
    assert authed.get("/api/status").get_json()["schedules"] == []


def test_get_schedule_broken_block(authed):
    set_crontab(f"{schedule.BEGIN}\n0 2 * * * {CMD}\n")
    assert authed.get("/api/schedule").get_json()["broken"] is True


# --------------------------------------------------------------------------- preview & save
def test_preview(authed):
    r = authed.post("/api/schedule/preview", headers=authed.H, json={"kind": "weekdays", "time": "04:30", "days": [6, 0]})
    d = r.get_json()
    assert r.status_code == 200 and d["expr"] == "30 4 * * 0,6" and d["form"]["text"] == "Sun, Sat at 04:30"
    assert len(d["next_runs"]) == 5


@pytest.mark.parametrize("body", [{"kind": "daily", "time": "25:00"}, {"kind": "advanced", "expr": "0 2 * * * ; reboot"},
                                  {"kind": "advanced", "expr": "0 2 * * *\n* * * * * x"}])
def test_preview_errors(authed, body):
    r = authed.post("/api/schedule/preview", headers=authed.H, json=body)
    assert r.status_code == 422 and r.get_json()["errors"]


def test_save_sends_request_to_host(authed):
    r = authed.put("/api/schedule", headers=authed.H, json={"enabled": True, "kind": "daily", "time": "02:00"})
    assert r.status_code == 200
    rid = r.get_json()["id"]
    req = json.loads(requests_written()["schedule"])
    assert req == {"id": rid, "enabled": True, "expr": "0 2 * * *", "user": "gozz"}
    assert authed.get("/api/schedule").get_json()["pending"] is True


def test_save_turn_off(authed):
    authed.put("/api/schedule", headers=authed.H, json={"enabled": False, "kind": "daily", "time": "02:00"})
    assert json.loads(requests_written()["schedule"])["enabled"] is False


@pytest.mark.parametrize("body", [
    {"kind": "daily", "time": "02:00"},                         # enabled missing
    {"enabled": "true", "kind": "daily", "time": "02:00"},      # not a boolean
    {"enabled": True, "kind": "advanced", "expr": "@reboot"},
    {"enabled": True, "kind": "advanced", "expr": "* * * * * /bin/sh -c id"},
    {"enabled": True, "kind": "hours", "every": 7},
])
def test_save_rejects_bad_input(authed, body):
    assert authed.put("/api/schedule", headers=authed.H, json=body).status_code == 422
    assert requests_written() == {}


def test_save_requires_login_and_csrf(client, authed):
    body = {"enabled": True, "kind": "daily", "time": "02:00"}
    assert client.application.test_client().put("/api/schedule", json=body).status_code in (400, 401)
    assert authed.put("/api/schedule", json=body).status_code == 400
    assert requests_written() == {}


def test_last_status_from_host(authed):
    (STATE / "schedule_status.json").write_text(json.dumps({"status": "failed", "message": "crontab rejected", "id": "x"}))
    assert authed.get("/api/schedule").get_json()["last_status"]["message"] == "crontab rejected"


# --------------------------------------------------------------------------- time zone
from zoneinfo import ZoneInfo  # noqa: E402

from app import settings  # noqa: E402
from conftest import HOST  # noqa: E402

CHICAGO = ZoneInfo("America/Chicago")


def set_host_zone(link=None, etc_timezone=None):
    lt, tz = HOST / "etc/localtime", HOST / "etc/timezone"
    for p in (lt, tz):
        if p.is_symlink() or p.exists():
            p.unlink()
    if link:
        lt.symlink_to(f"/usr/share/zoneinfo/{link}")
    if etc_timezone:
        tz.write_text(etc_timezone + "\n")


def test_host_timezone_from_localtime_link_wins_over_stale_etc_timezone(host):
    """Exactly this server: timedatectl updated /etc/localtime but /etc/timezone still said UTC."""
    set_host_zone(link="America/Chicago", etc_timezone="Etc/UTC")
    assert settings.host_timezone() == "America/Chicago"


def test_host_timezone_fallbacks(host, monkeypatch):
    set_host_zone(etc_timezone="America/Denver")
    assert settings.host_timezone() == "America/Denver"
    set_host_zone()
    monkeypatch.delenv("TZ", raising=False)
    assert settings.host_timezone() == "UTC"


@pytest.mark.parametrize("start,utc_hour", [
    (datetime(2026, 11, 9, 18, 0, tzinfo=CHICAGO), 8),   # CST, UTC-6
    (datetime(2027, 7, 1, 18, 0, tzinfo=CHICAGO), 7),    # CDT, UTC-5: still 2:00 AM local
])
def test_next_runs_follow_local_time_across_daylight_saving(start, utc_hour):
    (t,) = schedule.next_runs("0 2 * * *", 1, now=start)
    assert datetime.fromtimestamp(t, CHICAGO).strftime("%H:%M") == "02:00"
    assert datetime.fromtimestamp(t, timezone.utc).hour == utc_hour


def test_api_reports_server_zone_and_local_next_runs(authed):
    set_host_zone(link="America/Chicago", etc_timezone="Etc/UTC")
    set_crontab(block("0 2 * * *"))
    s = authed.get("/api/schedule").get_json()
    assert s["timezone_name"] == "America/Chicago" and s["timezone"] in ("CST", "CDT")
    assert all(datetime.fromtimestamp(t, CHICAGO).strftime("%H:%M") == "02:00" for t in s["next_runs"])
    (d,) = authed.get("/api/status").get_json()["schedules"]
    assert datetime.fromtimestamp(d["next"], CHICAGO).strftime("%H:%M") == "02:00"
