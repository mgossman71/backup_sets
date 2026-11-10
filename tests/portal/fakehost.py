"""Builds a fake host filesystem for the portal tests.

The portal reads everything about the host through HOST_ROOT (normally the read-only
bind of / at /host). Tests point HOST_ROOT at a temp dir laid out like a server:
account files, a mount table, root's crontab, a backup log, source and backup disks.
"""
import json
import os
import pathlib
import shutil
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import crypt  # deprecated in 3.12 (the portal image pins Python 3.12)

PASSWORDS = {"gozz": "correct horse", "bob": "bobs password", "expired": "expired pw", "locked": "locked pw"}

CONFIG_TEMPLATE = """# Backup Configuration File
# Edit this file to configure your backup settings

# Log file location
log_file: /var/log/backup_sets.log

# Rsync options (space-separated)
rsync_opts: "-av --stats"

# Flag files
running_flag: /mnt/.backup_running
fail_flag: /mnt/.backup_failed
stop_flag: /mnt/.backup_stop

# Required mount points to verify before starting backups
required_mount_points:
  - /mnt/disk1
  - /mnt/disk2

# Backup mappings
backups:
  - source: /mnt/src/TV
    destination: /mnt/disk1

  - source: /mnt/src/Movies
    destination: /mnt/disk2

# To add new backups from different sources, add more entries:
#  - source: /mnt/OtherVol/Photos
#    destination: /mnt/usb_chassis/disk5
"""

TV = {
    "Big Show/s1/e1.mkv": b"x" * 5000,
    "Big Show/s1/e2.mkv": b"x" * 3000,
    "Small Show/e1.mkv": b"y" * 100,
    "Show [x]/e1.mkv": b"z" * 10,
    "New Show/e1.mkv": b"n" * 700,
    "loose.nfo": b"l" * 42,
}
TV_ON_DISK = {  # already backed up (New Show not yet), plus an orphan
    "Big Show/s1/e1.mkv": b"x" * 5000,
    "Big Show/s1/e2.mkv": b"x" * 3000,
    "Small Show/e1.mkv": b"y" * 100,
    "Show [x]/e1.mkv": b"z" * 10,
    "Gone Show/e1.mkv": b"g" * 250,
    "loose.nfo": b"l" * 42,
}


def write_tree(root, spec):
    root = pathlib.Path(root)
    for rel, content in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def _hash(pw):
    return crypt.crypt(pw, crypt.mksalt(crypt.METHOD_SHA512))


def shadow_text(overrides=None):
    rows = {
        "root": "!",                                   # locked, like the real server
        "gozz": _hash(PASSWORDS["gozz"]),
        "bob": _hash(PASSWORDS["bob"]),                # valid password, not in sudo
        "locked": "!" + _hash(PASSWORDS["locked"]),
        "expired": _hash(PASSWORDS["expired"]),
        "nopw": "",
    }
    rows.update(overrides or {})
    lines = []
    for user, h in rows.items():
        expire = "1" if user == "expired" else ""      # expired on 1970-01-02
        lines.append(f"{user}:{h}:20000:0:99999:7::{expire}:")
    return "\n".join(lines) + "\n"


def build(host, config=CONFIG_TEMPLATE):
    host = pathlib.Path(host)
    if host.exists():
        shutil.rmtree(host)
    (host / "etc").mkdir(parents=True)
    (host / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "gozz:x:1000:1000::/home/gozz:/bin/bash\n"
        "bob:x:1001:1001::/home/bob:/bin/bash\n"
        "locked:x:1002:1002::/home/locked:/bin/bash\n"
        "expired:x:1003:27::/home/expired:/bin/bash\n"   # primary group sudo
        "nopw:x:1004:1004::/home/nopw:/bin/bash\n")
    (host / "etc/group").write_text(
        "root:x:0:\nsudo:x:27:gozz,locked,nopw\nbob:x:1001:\ngozz:x:1000:\n")
    (host / "etc/shadow").write_text(shadow_text())

    mnt = host / "mnt"
    for d in ["src", "disk1", "disk2", "unmounted"]:
        (mnt / d).mkdir(parents=True)
    write_tree(mnt / "src/TV", TV)
    write_tree(mnt / "disk1/TV", TV_ON_DISK)
    write_tree(mnt / "src/Movies", {"Film A/a.mkv": b"a" * 400, "Film B/b.mkv": b"b" * 600})
    os.symlink("/etc", mnt / "src/TV/LinkToEtc")  # must not be followed or listed

    (host / "proc/1").mkdir(parents=True)
    (host / "proc/1/mounts").write_text(
        "/dev/root / ext4 rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "/dev/sdb1 /mnt/src xfs rw 0 0\n"
        "/dev/sdc1 /mnt/disk1 ext4 rw 0 0\n"
        "/dev/sdd1 /mnt/disk2 ext4 rw 0 0\n"
        "overlay /var/lib/docker/overlay2/x overlay rw 0 0\n")

    (host / "var/spool/cron/crontabs").mkdir(parents=True)
    (host / "var/spool/cron/crontabs/root").write_text(
        "# m h dom mon dow command\n"
        "#*/5 * * * * bash /opt/backup_sets/backup.sh\n"
        "0 2 * * * bash /opt/backup_sets/backup.sh >> /var/log/backup_sets_cron.log 2>&1\n")

    (host / "var/log").mkdir(parents=True)
    (host / "var/log/backup_sets.log").write_text(
        "[2026-11-09 02:00:01] Backup script started\n"
        "[2026-11-09 02:00:05] Starting backup: TV\n"
        "sending incremental file list\n"
        "Big Show/s1/e1.mkv\n"
        "         32,768   0%    0.00kB/s    0:00:00      434,536,448  16%  414.38MB/s\n"
        "[2026-11-09 02:00:09] ERROR: something went wrong\n")
    return host


def history_lines(records):
    return "".join(json.dumps(r) + "\n" for r in records)
