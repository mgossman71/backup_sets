"""Read / validate / write config.yaml, preserving comments and layout (ruamel)."""
import hashlib
import io
import os
import shlex
import time

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from . import filesystems
from .settings import CONFIG_FILE, hostpath

BACKUP_COPY_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "config_backups")
KEEP_COPIES = 30
FORBIDDEN_RSYNC = ("-e", "--rsh", "--rsync-path", "--remote-option", "-M", "--daemon",
                   "--config", "--files-from", "--write-batch", "--read-batch", "--log-file")


class ConflictError(Exception):
    pass


class ValidationError(Exception):
    def __init__(self, errors, warnings):
        super().__init__("; ".join(errors))
        self.errors, self.warnings = errors, warnings


def _yaml():
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _read_raw():
    with open(CONFIG_FILE, "r") as f:
        return f.read()


def version_of(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load():
    """Plain-dict view of the config plus a version token for optimistic locking."""
    text = _read_raw()
    data = _yaml().load(text) or {}
    return {
        "version": version_of(text),
        "log_file": data.get("log_file"),
        "rsync_opts": data.get("rsync_opts") or "",
        "running_flag": data.get("running_flag"),
        "fail_flag": data.get("fail_flag"),
        "stop_flag": data.get("stop_flag"),
        "history_file": data.get("history_file"),
        "required_mount_points": [str(m) for m in (data.get("required_mount_points") or [])],
        "backups": [{"source": str(b.get("source", "")), "destination": str(b.get("destination", ""))}
                    for b in (data.get("backups") or [])],
    }


def _norm(p):
    p = (p or "").strip()
    if not p.startswith("/"):
        return p
    return os.path.normpath(p)


def _isdir(p):
    try:
        return os.path.isdir(hostpath(p))
    except ValueError:
        return False


def _inside(child, parent):
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def validate(backups, mount_points, rsync_opts):
    errors, warnings = [], []
    mounted = {m[1] for m in filesystems.mounts()}

    for i, mp in enumerate(mount_points):
        if not mp.startswith("/"):
            errors.append(f"Mount point #{i + 1} must be an absolute path: {mp!r}")
        elif mp not in mounted:
            warnings.append(f"{mp} is not currently mounted - backups will refuse to run until it is")

    seen_targets = {}
    for i, b in enumerate(backups):
        n = f"Backup #{i + 1}"
        src, dst = b["source"], b["destination"]
        if not src.startswith("/") or not dst.startswith("/"):
            errors.append(f"{n}: source and destination must be absolute paths")
            continue
        if src == "/":
            errors.append(f"{n}: source cannot be /")
            continue
        if not _isdir(src):
            errors.append(f"{n}: source does not exist or is not a directory: {src}")
        if not _isdir(dst):
            errors.append(f"{n}: destination does not exist or is not a directory: {dst}")
        target = os.path.join(dst, os.path.basename(src))
        if _inside(target, src) or _inside(src, target):
            errors.append(f"{n}: destination {target} overlaps the source {src}")
        if target in seen_targets:
            errors.append(f"{n}: writes to {target}, the same folder as backup #{seen_targets[target] + 1} "
                          f"(sources with the same folder name need different destinations)")
        seen_targets.setdefault(target, i)
        if mount_points and not any(_inside(dst, mp) for mp in mount_points):
            warnings.append(f"{n}: destination {dst} is not under a required mount point - if its disk "
                            f"is unmounted, rsync would write into the parent filesystem")

    try:
        tokens = shlex.split(rsync_opts)
    except ValueError as e:
        errors.append(f"rsync options: {e}")
        tokens = []
    for t in tokens:
        if not t.startswith("-"):
            errors.append(f"rsync options: unexpected argument {t!r} (only options are allowed)")
        elif t.split("=", 1)[0] in FORBIDDEN_RSYNC:
            errors.append(f"rsync options: {t.split('=', 1)[0]} is not allowed from the portal")
    if any(t.startswith("--delete") for t in tokens):
        warnings.append("rsync options include --delete: files removed from a source will also be "
                        "deleted from its backup")
    if tokens and not any(t in ("-a", "--archive") or (t.startswith("-") and not t.startswith("--")
                                                       and "a" in t[1:]) for t in tokens):
        warnings.append("rsync options do not include -a (archive mode)")
    return errors, warnings


def save(new, expected_version, user="portal", force=False):
    """Apply backups / required_mount_points / rsync_opts. Returns (version, warnings)."""
    backups = [{"source": _norm(b.get("source")), "destination": _norm(b.get("destination"))}
               for b in new.get("backups", [])]
    mount_points = [_norm(m) for m in new.get("required_mount_points", []) if str(m).strip()]
    rsync_opts = " ".join(str(new.get("rsync_opts", "")).split())

    errors, warnings = validate(backups, mount_points, rsync_opts)
    if errors:
        raise ValidationError(errors, warnings)
    if warnings and not force:
        raise ValidationError([], warnings)

    text = _read_raw()
    if version_of(text) != expected_version:
        raise ConflictError("config.yaml was changed by someone else - reload and try again")

    y = _yaml()
    data = y.load(text)
    data["rsync_opts"] = rsync_opts
    _replace_seq(data, "required_mount_points", mount_points)
    _replace_seq(data, "backups", [{"source": b["source"], "destination": b["destination"]}
                                   for b in backups])
    buf = io.StringIO()
    y.dump(data, buf)
    out = buf.getvalue()

    _write_backup_copy(text, user)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(out)
    st = os.stat(CONFIG_FILE)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, CONFIG_FILE)
    return version_of(out), warnings


def _replace_seq(data, key, items):
    """Replace a sequence's items in place, keeping the comment that trails the
    sequence (e.g. the example block after the last backup entry)."""
    old = data.get(key)
    seq = old if isinstance(old, CommentedSeq) else CommentedSeq()
    trailing = None
    if len(seq):
        last = seq[-1]
        if isinstance(last, CommentedMap) and last.ca.items:
            trailing = last.ca.items.get(list(last.keys())[-1])
        elif seq.ca.items.get(len(seq) - 1):
            trailing = seq.ca.items.get(len(seq) - 1)
    seq.clear()
    seq.ca.items.clear()
    for i, item in enumerate(items):
        if isinstance(item, dict):
            m = CommentedMap(item)
            seq.append(m)
            if i < len(items) - 1:
                # blank line between backup entries, as in the hand-written file
                m.ca.items[list(m.keys())[-1]] = [None, None, CommentToken("\n\n", CommentMark(0), None), None]
        else:
            seq.append(item)
    if items and trailing:
        last = seq[-1]
        if isinstance(last, CommentedMap):
            last.ca.items[list(last.keys())[-1]] = trailing
        else:
            seq.ca.items[len(seq) - 1] = trailing
    data[key] = seq


def _write_backup_copy(text, user):
    os.makedirs(BACKUP_COPY_DIR, exist_ok=True)
    name = f"config.yaml.{time.strftime('%Y%m%d-%H%M%S')}.{user}"
    with open(os.path.join(BACKUP_COPY_DIR, name), "w") as f:
        f.write(text)
    copies = sorted(os.listdir(BACKUP_COPY_DIR))
    for old in copies[:-KEEP_COPIES]:
        try:
            os.remove(os.path.join(BACKUP_COPY_DIR, old))
        except OSError:
            pass


def list_backup_copies():
    try:
        return sorted(os.listdir(BACKUP_COPY_DIR), reverse=True)
    except FileNotFoundError:
        return []
