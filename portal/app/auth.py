"""Authentication against the HOST's local accounts (/etc/shadow via the /host mount).

Passwords are verified with libcrypt (supports yescrypt, sha512-crypt, ...).
Reading the files through /host (rather than bind-mounting /etc/shadow itself)
means password changes on the host are picked up immediately.
"""
import hmac
import re
import threading
import time
import warnings
from collections import defaultdict, deque

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import crypt  # noqa: E402  (deprecated, still present in 3.12)

from .settings import ALLOWED_GROUP, ALLOWED_USERS, hostpath

USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$")
# Used for unknown users so response time doesn't reveal which accounts exist
_DUMMY_HASH = crypt.crypt("dummy-password", crypt.mksalt(crypt.METHOD_SHA512))

MAX_FAILURES = 5
WINDOW_SECONDS = 300
_failures = defaultdict(deque)
_fail_lock = threading.Lock()


def _read_db(name):
    with open(hostpath(f"/etc/{name}"), "r", errors="replace") as f:
        return [line.rstrip("\n").split(":") for line in f if line.strip() and not line.startswith("#")]


def _shadow_entry(user):
    for fields in _read_db("shadow"):
        if fields[0] == user:
            return fields
    return None


def user_groups(user):
    groups = set()
    gid = None
    for fields in _read_db("passwd"):
        if fields[0] == user and len(fields) > 3:
            gid = fields[3]
    for fields in _read_db("group"):
        if len(fields) < 4:
            continue
        members = [m for m in fields[3].split(",") if m]
        if user in members or fields[2] == gid:
            groups.add(fields[0])
    return groups


def is_authorized(user):
    return user in ALLOWED_USERS or ALLOWED_GROUP in user_groups(user)


def check_password(user, password):
    """True if user/password match an enabled, unexpired host account."""
    if not USER_RE.match(user or "") or not password:
        crypt.crypt(password or "", _DUMMY_HASH)
        return False
    entry = _shadow_entry(user)
    pw_hash = entry[1] if entry and len(entry) > 1 else ""
    if not pw_hash or pw_hash[0] in "!*" or not pw_hash.startswith("$"):
        crypt.crypt(password, _DUMMY_HASH)
        return False
    # Account expiry (field 8, days since epoch)
    if len(entry) > 7 and entry[7].strip().isdigit():
        if int(entry[7]) * 86400 < time.time():
            return False
    computed = crypt.crypt(password, pw_hash)
    return computed is not None and hmac.compare_digest(computed, pw_hash)


def rate_limited(ip):
    now = time.time()
    with _fail_lock:
        q = _failures[ip]
        while q and q[0] < now - WINDOW_SECONDS:
            q.popleft()
        return len(q) >= MAX_FAILURES


def record_failure(ip):
    with _fail_lock:
        _failures[ip].append(time.time())


def clear_failures(ip):
    with _fail_lock:
        _failures.pop(ip, None)
