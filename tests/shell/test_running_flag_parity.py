"""The 'is a backup running?' rule is implemented four times - in backup.sh, host/actions.sh,
host/cleanup.py and portal/app/status.py. This test runs all four on the same cases so they
can never drift apart."""
import json
import os
import subprocess
import sys

import pytest

from helpers import REPO

CASES = ["none", "empty", "dead", "backup_sh", "cleanup_py", "other"]
EXPECTED = {"none": False, "empty": True, "dead": False, "backup_sh": True, "cleanup_py": True, "other": False}


@pytest.fixture(scope="module")
def procs():
    p = {
        # "; true" stops bash from exec'ing sleep, so the command line keeps the marker
        "backup_sh": subprocess.Popen(["bash", "-c", "sleep 60; true", "/opt/backup_sets/backup.sh"]),
        "cleanup_py": subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                        "/opt/backup_sets/host/cleanup.py"]),
        "other": subprocess.Popen(["sleep", "60"]),
    }
    dead = subprocess.Popen(["true"])
    dead.wait()
    p["dead_pid"] = dead.pid
    yield p
    for k, v in p.items():
        if k != "dead_pid":
            v.kill()
            v.wait()


def make_flag(tmp_path, case, procs):
    flag = tmp_path / "flag"
    if case == "none":
        return flag
    content = {"empty": "", "dead": f"{procs['dead_pid']}\n"}.get(case)
    if content is None:
        content = f"{procs[case].pid}\n"
    flag.write_text(content)
    return flag


def bash_fn(script, fn, extra_fns=()):
    text = open(REPO / script).read()
    out = []
    for name in (*extra_fns, fn):
        start = text.index(f"{name}() {{")
        out.append(text[start:text.index("\n}\n", start) + 3])
    return "\n".join(out)


def via_backup_sh(flag):
    code = bash_fn("backup.sh", "running_flag_active") + '\nRUNNING_FLAG="$1"; running_flag_active && echo yes || echo no'
    return subprocess.run(["bash", "-c", code, "x", str(flag)], capture_output=True, text=True).stdout.strip() == "yes"


def via_actions_sh(flag):
    code = bash_fn("host/actions.sh", "backup_running", ("valid_path",)) + \
        '\nRUNNING_FLAG="$1"; backup_running && echo yes || echo no'
    return subprocess.run(["bash", "-c", code, "x", str(flag)], capture_output=True, text=True).stdout.strip() == "yes"


def via_cleanup_py(flag):
    code = ("import importlib.util, sys; s = importlib.util.spec_from_file_location('c', sys.argv[1]); "
            "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.backup_running(sys.argv[2]))")
    return subprocess.run([sys.executable, "-c", code, str(REPO / "host/cleanup.py"), str(flag)],
                          capture_output=True, text=True).stdout.strip() == "True"


def via_portal(flag, tmp_path):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from app import status; "
            "print(status._running_pid_alive(sys.argv[2])[0] if __import__('os').path.exists(sys.argv[2]) else False)")
    env = dict(os.environ, HOST_ROOT="", STATE_DIR=str(tmp_path / "state"), CONFIG_FILE=str(tmp_path / "c.yaml"))
    r = subprocess.run([sys.executable, "-c", code, str(REPO / "portal"), str(flag)], capture_output=True,
                       text=True, env=env)
    return r.stdout.strip() == "True"


@pytest.mark.parametrize("case", CASES)
def test_all_four_implementations_agree(tmp_path, procs, case):
    flag = make_flag(tmp_path, case, procs)
    got = {"backup.sh": via_backup_sh(flag), "actions.sh": via_actions_sh(flag),
           "cleanup.py": via_cleanup_py(flag), "portal": via_portal(flag, tmp_path)}
    assert got == {k: EXPECTED[case] for k in got}, f"{case}: {json.dumps(got)}"
