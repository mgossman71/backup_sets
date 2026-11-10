"""host/schedule.py (via host/actions.sh): writing the backup schedule into root's crontab.

A file-backed stand-in replaces the `crontab` command, so the real crontab is never used."""
import json
import os

import pytest

from test_actions import Bridge

FAKE_CRONTAB = """#!/bin/bash
# test stand-in for crontab: -l prints the file, - installs stdin (unless told to fail)
f="$FAKE_CRONTAB_FILE"
case "$1" in
  -l) if [ -f "$f" ]; then cat "$f"; else echo "no crontab for root" >&2; exit 1; fi ;;
  -)  if [ -n "$FAKE_CRONTAB_FAIL" ]; then cat >/dev/null; echo "errors in crontab file, can't install" >&2; exit 1; fi
      cat > "$f.new" && mv "$f.new" "$f" ;;
  *)  exit 2 ;;
esac
"""

USER_CRONTAB = """# Edit this file to introduce tasks to be run by cron.
# m h  dom mon dow   command
MAILTO=""
#*/5 * * * * bash /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1
#0 2 * * * bash /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1
30 4 * * 0 /usr/local/bin/other-job.sh --keep  this line exactly
"""

BEGIN = "# BEGIN backup_sets schedule (managed by the backup portal - change it there)"
END = "# END backup_sets schedule"


class Sched(Bridge):
    def __init__(self, root):
        super().__init__(root)
        self.crontab = self.root / "crontab.txt"
        stub = self.root / "crontab"
        stub.write_text(FAKE_CRONTAB)
        stub.chmod(0o755)
        self.crontab_stub = stub
        self.fail_install = False
        self.status_file = self.root / "schedule_status.json"

    def set(self, enabled, expr, user="gozz", rid="r1", raw=None):
        body = raw if raw is not None else json.dumps({"user": user, "id": rid, "enabled": enabled, "expr": expr})
        self.request("schedule", body)
        os.environ["CRONTAB_CMD"] = str(self.crontab_stub)
        os.environ["FAKE_CRONTAB_FILE"] = str(self.crontab)
        if self.fail_install:
            os.environ["FAKE_CRONTAB_FAIL"] = "1"
        try:
            self.process()
        finally:
            for k in ("CRONTAB_CMD", "FAKE_CRONTAB_FILE", "FAKE_CRONTAB_FAIL"):
                os.environ.pop(k, None)
        return json.loads(self.status_file.read_text())

    def text(self):
        return self.crontab.read_text() if self.crontab.exists() else None

    def block(self):
        lines = self.text().splitlines()
        i = lines.index(BEGIN)
        assert lines[i + 2] == END
        return lines[i + 1]


@pytest.fixture
def sc(tmp_path):
    s = Sched(tmp_path)
    s.crontab.write_text(USER_CRONTAB)
    return s


def backup_line(script_dir, expr, enabled=True):
    return ("" if enabled else "#") + f"{expr} bash {script_dir}/backup.sh >> /var/log/backup_sets_cron.log 2>&1"


@pytest.fixture
def script_dir():
    from helpers import REPO
    return str(REPO)


def test_enable_adds_block_and_keeps_every_other_line(sc, script_dir):
    st = sc.set(True, "0 2 * * *")
    assert st["status"] == "applied", st
    assert sc.text() == USER_CRONTAB + "\n" + BEGIN + "\n" + backup_line(script_dir, "0 2 * * *") + "\n" + END + "\n"
    assert 'Portal action: schedule set to "0 2 * * *" by gozz' in sc.log_text()


def test_update_replaces_block_in_place(sc, script_dir):
    sc.set(True, "0 2 * * *")
    first = sc.text()
    st = sc.set(True, "30 */6 * * *", rid="r2")
    assert st["status"] == "applied" and st["id"] == "r2"
    assert sc.text() == first.replace(backup_line(script_dir, "0 2 * * *"), backup_line(script_dir, "30 */6 * * *"))
    assert sc.text().count(BEGIN) == 1


def test_disable_keeps_schedule_commented(sc, script_dir):
    sc.set(True, "15 3 * * 1,3,5")
    st = sc.set(False, "15 3 * * 1,3,5")
    assert st["status"] == "applied"
    assert sc.block() == backup_line(script_dir, "15 3 * * 1,3,5", enabled=False)
    assert "turned off" in sc.log_text()


def test_block_moved_by_user_is_updated_where_it_is(sc, script_dir):
    sc.crontab.write_text(f"A=1\n{BEGIN}\n{backup_line(script_dir, '0 1 * * *')}\n{END}\n30 4 * * 0 other\n")
    sc.set(True, "0 5 * * *")
    assert sc.text() == f"A=1\n{BEGIN}\n{backup_line(script_dir, '0 5 * * *')}\n{END}\n30 4 * * 0 other\n"


def test_unchanged_schedule_is_not_reinstalled(sc):
    sc.set(True, "0 2 * * *")
    mtime = os.stat(sc.crontab).st_mtime_ns
    st = sc.set(True, "0 2 * * *")
    assert st["status"] == "applied" and "unchanged" in st["message"]
    assert os.stat(sc.crontab).st_mtime_ns == mtime


def test_active_backup_lines_elsewhere_are_commented_out(sc, script_dir):
    stray = f"0 3 * * * bash {script_dir}/backup.sh >> /tmp/x.log"
    sc.crontab.write_text(USER_CRONTAB + stray + "\n")
    st = sc.set(True, "0 2 * * *")
    assert "commented out 1 old backup line" in st["message"]
    assert "#[moved to portal schedule] " + stray in sc.text().splitlines()
    active = [l for l in sc.text().splitlines() if l and not l.startswith("#") and "backup.sh" in l]
    assert active == [backup_line(script_dir, "0 2 * * *")], "exactly one active backup line"


def test_other_scripts_named_backup_sh_are_not_touched(sc):
    other = "0 3 * * * /home/someone/backup.sh"
    sc.crontab.write_text(USER_CRONTAB + other + "\n")
    sc.set(True, "0 2 * * *")
    assert other in sc.text().splitlines()


def test_empty_crontab(sc, script_dir):
    sc.crontab.unlink()
    st = sc.set(True, "0 2 * * *")
    assert st["status"] == "applied"
    assert sc.text() == f"{BEGIN}\n{backup_line(script_dir, '0 2 * * *')}\n{END}\n"


def test_failed_install_leaves_crontab_unchanged(sc):
    sc.fail_install = True
    st = sc.set(True, "0 2 * * *")
    assert st["status"] == "failed" and "crontab rejected" in st["message"]
    assert sc.text() == USER_CRONTAB


@pytest.mark.parametrize("expr", [
    "0 2 * * * ; rm -rf /", "0 2 * * *\n* * * * * rm -rf /", "0 2 * * * rm", "@reboot", "@daily",
    "0 2 * *", "0 2 * * * *", "0  2 * * *", "60 2 * * *", "0 24 * * *", "0 2 0 * *", "0 2 * 13 *",
    "0 2 * * 8", "*/0 * * * *", "0\n 2 * * *", "0 2\n * * *", "5-1 * * * *", "0 2 * * MON", "$(id) 2 * * *", "0 2 * * *\r", "", None, 5,
])
def test_invalid_schedules_refused(sc, expr):
    st = sc.set(True, expr)
    assert st["status"] == "failed" and "invalid schedule" in st["message"]
    assert sc.text() == USER_CRONTAB


@pytest.mark.parametrize("raw,fragment", [
    ("not json", "unreadable request"),
    ("[1,2]", "unreadable request"),
    (json.dumps({"user": "gozz", "expr": "0 2 * * *", "enabled": "yes"}), "'enabled' must be true or false"),
    (json.dumps({"user": "gozz", "expr": "0 2 * * *"}), "'enabled' must be true or false"),
])
def test_malformed_requests_refused(sc, raw, fragment):
    st = sc.set(None, None, raw=raw)
    assert st["status"] == "failed" and fragment in st["message"]
    assert sc.text() == USER_CRONTAB


@pytest.mark.parametrize("damage", [f"{BEGIN}\nx\n", f"x\n{END}\n", f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n"])
def test_damaged_block_is_refused(sc, damage):
    sc.crontab.write_text(USER_CRONTAB + damage)
    st = sc.set(True, "0 2 * * *")
    assert st["status"] == "failed" and "crontab -e" in st["message"]
    assert sc.text() == USER_CRONTAB + damage


def test_user_is_sanitised(sc):
    st = sc.set(True, "0 2 * * *", user="$(touch /tmp/pwned)")
    assert st["user"] == "unknown"
    assert not os.path.exists("/tmp/pwned")


def test_command_comes_from_the_host_not_the_request(sc, script_dir):
    raw = json.dumps({"user": "gozz", "id": "x", "enabled": True, "expr": "0 2 * * *",
                      "command": "rm -rf /", "line": "* * * * * rm -rf /"})
    sc.set(None, None, raw=raw)
    assert sc.block() == backup_line(script_dir, "0 2 * * *")
    assert "rm -rf" not in sc.text()
