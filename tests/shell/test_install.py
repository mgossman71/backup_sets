"""host/install.sh: the dependency check runs before anything on the system is changed."""
import os
import subprocess

import pytest

from helpers import REPO

NEEDED = ["rsync", "jq", "yq", "python3", "openssl", "systemctl", "docker", "crontab"]
SYSTEM = ["id", "grep", "bash", "env", "cat", "dirname", "basename", "head", "tr"]  # real tools


def run_install(tmp_path, drop=(), fake_go_yq=False, no_yaml=False):
    """Run install.sh with a PATH of stubs. Every tool exists (as a harmless stub) unless dropped."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in NEEDED + SYSTEM:
        if name in drop:
            continue
        stub = bin_dir / name
        if name == "yq":
            stub.write_text("#!/bin/bash\nif [ \"$1\" = --version ]; then echo " +
                            ("'yq (https://github.com/mikefarah/yq/) version v4.44.1'" if fake_go_yq else "'yq 3.1.0'") +
                            "; exit 0; fi\necho 1\n")
        elif name == "python3":
            stub.write_text("#!/bin/bash\n" + ("exit 1\n" if no_yaml else "exit 0\n"))
        elif name == "docker":
            stub.write_text("#!/bin/bash\nexit 0\n")
        elif name in SYSTEM:
            os.symlink(subprocess.run(["which", name], capture_output=True, text=True).stdout.strip(), stub)
            continue
        else:
            stub.write_text("#!/bin/bash\necho \"STUB $0 $*\" >> \"$STUB_CALLS\"\nexit 0\n")
        stub.chmod(0o755)
    calls = tmp_path / "calls"
    r = subprocess.run(["/bin/bash", str(REPO / "host/install.sh")], capture_output=True, text=True,
                       env={"PATH": str(bin_dir), "STUB_CALLS": str(calls)})
    return r, calls


@pytest.mark.parametrize("missing", ["yq", "rsync", "jq", "docker", "crontab"])
def test_missing_dependency_stops_before_any_change(tmp_path, missing):
    before = os.path.exists("/var/lib/backup_sets"), os.path.exists("/etc/systemd/system/backup-portal-actions.path")
    r, calls = run_install(tmp_path, drop=(missing,))
    assert r.returncode == 1
    assert "nothing was changed" in r.stdout and missing in r.stdout
    after = os.path.exists("/var/lib/backup_sets"), os.path.exists("/etc/systemd/system/backup-portal-actions.path")
    assert after == before and not calls.exists(), "no installer step ran"


def test_go_yq_is_rejected(tmp_path):
    r, _ = run_install(tmp_path, fake_go_yq=True)
    assert r.returncode == 1 and "Python yq is required" in r.stdout


def test_missing_python_yaml_is_reported(tmp_path):
    r, _ = run_install(tmp_path, no_yaml=True)
    assert r.returncode == 1 and "python3-yaml" in r.stdout
