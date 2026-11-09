"""Backup schedule: presets <-> cron expressions, strict validation, and reading the
portal-managed block in root's crontab (through /host, read-only).

The portal never writes the crontab. It sends a request to the host bridge, and
host/schedule.py (which has its own copy of validate()) applies it.
"""
import re
from datetime import datetime

from croniter import croniter

from .settings import CRONTAB_FILE, hostpath

BEGIN = "# BEGIN backup_sets schedule (managed by the backup portal - change it there)"
END = "# END backup_sets schedule"
EVERY_HOURS = (1, 2, 3, 4, 6, 8, 12)
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# one cron field: "*" or "n" or "n-m", optionally "/step", comma-separated
_FIELD = re.compile(r"^(\*|\d{1,2}(-\d{1,2})?)(/\d{1,2})?(,(\*|\d{1,2}(-\d{1,2})?)(/\d{1,2})?)*$")
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["minute", "hour", "day of month", "month", "day of week"]


def validate(expr):
    """Return a list of problems with a 5-field cron expression (empty list = valid)."""
    if not isinstance(expr, str):
        return ["The schedule must be text"]
    if "\n" in expr or "\r" in expr or "\0" in expr:
        return ["The schedule must be a single line"]
    fields = expr.split(" ")
    if len(fields) != 5 or any(f == "" for f in fields):
        return ["A schedule needs exactly 5 fields separated by single spaces: minute hour day month weekday"]
    errors = []
    for f, (lo, hi), name in zip(fields, _RANGES, _NAMES):
        if not _FIELD.fullmatch(f):  # fullmatch: "$" alone would allow a trailing newline
            errors.append(f"The {name} field {f!r} may only contain digits and * , - /")
            continue
        for part in f.split(","):
            base, _, step = part.partition("/")
            if step and int(step) == 0:
                errors.append(f"The {name} field has a step of 0")
            if base != "*":
                a, _, b = base.partition("-")
                for v in (a, b or a):
                    if not lo <= int(v) <= hi:
                        errors.append(f"The {name} field value {v} is outside {lo}-{hi}")
                if b and int(b) < int(a):
                    errors.append(f"The {name} field range {base} is backwards")
    if not errors:
        try:
            croniter(expr)
        except (ValueError, KeyError) as e:
            errors.append(f"Not a valid schedule: {e}")
    return errors


def _hm(time_str):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(time_str or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Enter a time as HH:MM (24-hour, server time)")
    return int(m.group(1)), int(m.group(2))


def from_preset(body):
    """Build a cron expression from the form fields. Raises ValueError with a readable message."""
    kind = body.get("kind")
    if kind == "daily":
        h, m = _hm(body.get("time"))
        return f"{m} {h} * * *"
    if kind == "weekdays":
        h, m = _hm(body.get("time"))
        try:
            days = sorted({int(d) for d in body.get("days") or []})
        except (TypeError, ValueError):
            raise ValueError("Pick the days of the week")
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Pick at least one day of the week")
        if len(days) == 7:
            return f"{m} {h} * * *"
        return f"{m} {h} * * {','.join(map(str, days))}"
    if kind == "hours":
        try:
            every, minute = int(body.get("every")), int(body.get("minute", 0))
        except (TypeError, ValueError):
            raise ValueError("Pick how often to run")
        if every not in EVERY_HOURS:
            raise ValueError(f"Every N hours must be one of {', '.join(map(str, EVERY_HOURS))}")
        if not 0 <= minute <= 59:
            raise ValueError("Minute must be 0-59")
        return f"{minute} * * * *" if every == 1 else f"{minute} */{every} * * *"
    if kind == "advanced":
        expr = str(body.get("expr") or "").strip()
        # tidy repeated spaces, but leave line breaks in place so validate() rejects them
        return expr if ("\n" in expr or "\r" in expr) else " ".join(expr.split())
    raise ValueError("Unknown schedule type")


def describe(expr):
    """Turn an expression back into form fields + a readable sentence."""
    f = expr.split(" ")
    plain = lambda s: s.isdigit()  # noqa: E731
    if len(f) == 5 and plain(f[0]) and f[2] == "*" and f[3] == "*":
        m = int(f[0])
        if plain(f[1]):
            t = f"{int(f[1]):02d}:{m:02d}"
            if f[4] == "*":
                return {"kind": "daily", "time": t, "text": f"Daily at {t}"}
            if re.match(r"^\d(,\d)*$", f[4]):
                days = sorted({int(d) % 7 for d in f[4].split(",")})
                return {"kind": "weekdays", "time": t, "days": days,
                        "text": f"{', '.join(DAY_NAMES[d] for d in days)} at {t}"}
        if f[4] == "*" and (f[1] == "*" or re.match(r"^\*/\d+$", f[1])):
            every = 1 if f[1] == "*" else int(f[1][2:])
            if every in EVERY_HOURS:
                return {"kind": "hours", "every": every, "minute": m,
                        "text": "Every hour" + (f" at :{m:02d}" if m else "") if every == 1
                        else f"Every {every} hours at :{m:02d}"}
    return {"kind": "advanced", "expr": expr, "text": f"Custom schedule ({expr})"}


def next_runs(expr, n=5, now=None):
    it = croniter(expr, now or datetime.now().astimezone())
    return [it.get_next(datetime).timestamp() for _ in range(n)]


_LINE = re.compile(r"^(#?)\s*(\S+ \S+ \S+ \S+ \S+)\s+(.*)$")


def parse_crontab(text, backup_dir="/opt/backup_sets"):
    """{managed: {enabled, expr} | None, unmanaged: [active backup.sh lines outside the block],
    broken: bool (BEGIN without END)}"""
    managed, unmanaged, inside, broken = None, [], False, False
    script = f"{backup_dir}/backup.sh"
    for raw in text.splitlines():
        line = raw.strip()
        if line == BEGIN:
            inside = True
            continue
        if line == END:
            inside = False
            continue
        if inside:
            m = _LINE.match(line)
            if m and script in m.group(3) and managed is None:
                managed = {"enabled": m.group(1) == "", "expr": m.group(2)}
            continue
        if line and not line.startswith("#") and script in line:
            unmanaged.append(line)
    if inside:
        broken = True
    return {"managed": managed, "unmanaged": unmanaged, "broken": broken}


def read_crontab():
    try:
        with open(hostpath(CRONTAB_FILE)) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    return parse_crontab(text)
