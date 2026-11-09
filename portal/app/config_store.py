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
# Options that could run commands, read/write arbitrary files, or delete source data.
FORBIDDEN_LONG = ("--rsh", "--rsync-path", "--remote-option", "--daemon", "--config", "--files-from",
                  "--write-batch", "--only-write-batch", "--read-batch", "--log-file", "--remove-source-files",
                  "--exclude-from", "--include-from", "--password-file", "--write-devices")
FORBIDDEN_SHORT = {"e": "-e (--rsh)", "M": "-M (--remote-option)"}


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
        "backups": [_backup_view(b) for b in (data.get("backups") or [])],
    }


def _backup_view(b):
    inc = [str(x) for x in (b.get("include") or [])]
    exc = [str(x) for x in (b.get("exclude") or [])]
    if inc and exc:
        mode = "both"  # invalid: surfaced by validate()
    elif inc:
        mode = "include"
    elif exc:
        mode = "exclude"
    else:
        mode = "all"
    return {
        "source": str(b.get("source", "")),
        "destination": str(b.get("destination", "")),
        "mode": mode,
        "folders": inc or exc,
        "include_root_files": b.get("include_root_files") is not False,
    }


def normalize_backups(raw):
    """Clean up backup entries coming from the portal UI."""
    out = []
    for b in raw or []:
        mode = b.get("mode") or "all"
        folders = []
        for f in b.get("folders") or []:
            f = str(f)
            if f not in folders:
                folders.append(f)
        out.append({
            "source": _norm(b.get("source")),
            "destination": _norm(b.get("destination")),
            "mode": mode if folders or mode == "include" else "all",
            "folders": folders if mode in ("include", "exclude") else [],
            "include_root_files": b.get("include_root_files") is not False,
        })
    return out


def valid_folder_name(name):
    return bool(name) and name not in (".", "..") and "/" not in name and "\n" not in name and "\0" not in name


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

        mode, folders = b.get("mode", "all"), b.get("folders", [])
        if mode == "both":
            errors.append(f"{n}: has both include and exclude lists in config.yaml - use only one")
        elif mode not in ("all", "include", "exclude"):
            errors.append(f"{n}: unknown folder selection mode {mode!r}")
        elif mode == "include" and not folders:
            errors.append(f"{n}: no folders are selected - choose at least one, or back up everything")
        bad = [f for f in folders if not valid_folder_name(f)]
        if bad:
            errors.append(f"{n}: invalid folder name(s): {', '.join(repr(f) for f in bad)}")
        if mode == "include" and folders and _isdir(src):
            missing = [f for f in folders if valid_folder_name(f) and not _isdir(os.path.join(src, f))]
            if missing:
                warnings.append(f"{n}: {len(missing)} selected folder(s) no longer exist in {src}: "
                                f"{', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}")

    try:
        tokens = shlex.split(rsync_opts)
    except ValueError as e:
        errors.append(f"rsync options: {e}")
        tokens = []
    for t in tokens:
        if not t.startswith("-"):
            errors.append(f"rsync options: unexpected argument {t!r} (only options are allowed)")
        elif t.startswith("--"):
            if t.split("=", 1)[0] in FORBIDDEN_LONG:
                errors.append(f"rsync options: {t.split('=', 1)[0]} is not allowed from the portal")
        else:
            # short options can be clustered ("-avessh" is -a -v -e ssh): check every letter
            # up to the first one that takes a value
            bad = next((FORBIDDEN_SHORT[c] for c in t[1:] if c in FORBIDDEN_SHORT), None)
            if bad:
                errors.append(f"rsync options: {bad} is not allowed from the portal (in {t!r})")
    if "--delete-excluded" in tokens:
        warnings.append("rsync options include --delete-excluded: copies of deselected folders will be "
                        "deleted from the backup disk on the next run")
    if any(t.startswith("--delete") for t in tokens):
        warnings.append("rsync options include --delete: files removed from a source will also be "
                        "deleted from its backup")
    if tokens and not any(t in ("-a", "--archive") or (t.startswith("-") and not t.startswith("--")
                                                       and "a" in t[1:]) for t in tokens):
        warnings.append("rsync options do not include -a (archive mode)")
    return errors, warnings


def save(new, expected_version, user="portal", force=False):
    """Apply backups / required_mount_points / rsync_opts. Returns (version, warnings)."""
    backups = normalize_backups(new.get("backups", []))
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
    _replace_seq(data, "backups", [_entry_map(b) for b in backups])
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


def _entry_map(b):
    m = CommentedMap()
    m["source"] = b["source"]
    m["destination"] = b["destination"]
    if b["mode"] in ("include", "exclude") and b["folders"]:
        m[b["mode"]] = CommentedSeq(b["folders"])
        if b["mode"] == "include" and not b["include_root_files"]:
            m["include_root_files"] = False
    return m


def _blank():
    return CommentToken("\n\n", CommentMark(0), None)


def _get_after(node):
    """The comment token that follows a backup entry / list item (blank lines, trailing comments)."""
    if isinstance(node, CommentedMap) and len(node):
        last_key = list(node.keys())[-1]
        val = node[last_key]
        if isinstance(val, CommentedSeq) and len(val):
            tok = val.ca.items.get(len(val) - 1)
            return tok[0] if tok else None
        tok = node.ca.items.get(last_key)
        return tok[2] if tok and len(tok) > 2 else None
    return None


def _set_after(node, token):
    last_key = list(node.keys())[-1]
    val = node[last_key]
    if isinstance(val, CommentedSeq) and len(val):
        val.ca.items[len(val) - 1] = [token, None]
    else:
        node.ca.items[last_key] = [None, None, token, None]


def _replace_seq(data, key, items):
    """Replace a sequence's items in place, keeping blank lines between entries and the
    comment that trails the sequence (e.g. the example block after the last backup entry)."""
    old = data.get(key)
    seq = old if isinstance(old, CommentedSeq) else CommentedSeq()
    trailing = None
    if len(seq):
        last = seq[-1]
        if isinstance(last, CommentedMap):
            trailing = _get_after(last)
        elif seq.ca.items.get(len(seq) - 1):
            trailing = seq.ca.items.get(len(seq) - 1)
    seq.clear()
    seq.ca.items.clear()
    for i, item in enumerate(items):
        seq.append(item)
        if isinstance(item, CommentedMap) and i < len(items) - 1:
            _set_after(item, _blank())
    if items and trailing is not None:
        last = seq[-1]
        if isinstance(last, CommentedMap):
            _set_after(last, trailing)
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
