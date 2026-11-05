"""Pause and resume jobs (Developer dashboard, deploys).

A pause is kept in the database (state 'jobs_pause'), so it survives a restart. It covers every job or a list of jobs,
and always ends by itself (`until`), so a forgotten pause cannot stop the daily collection. While it lasts, the
worker, the scheduler and the health watcher start nothing it covers, and a manual run is refused.

"Pause now" also asks the running jobs it covers to stop at their next safe point (between songs, pages or videos;
everything read so far is kept); those runs are recorded with status 'paused'. Every job works from what is still
due, so after a resume it carries on where it stopped. Paused runs do not count as "ran recently" for the worker's
spacing, so a job paused part-way is due again as soon as the pause ends.
"""
import json
from datetime import datetime, timedelta, timezone

from . import db

KEY = "jobs_pause"
DEFAULT_MINUTES = 30
MAX_MINUTES = 24 * 60
ALERT_AFTER_MINUTES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS pause_log (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    action  TEXT NOT NULL,     -- pause | pause_now | resume | expired
    by      TEXT,
    reason  TEXT,
    jobs    TEXT,              -- JSON list, or NULL for every job
    until   TEXT
);
"""


def _now():
    return datetime.now(timezone.utc)


def ensure(conn):
    conn.executescript(SCHEMA)


def _log(conn, action, by, reason=None, jobs=None, until=None):
    ensure(conn)
    conn.execute("INSERT INTO pause_log (at, action, by, reason, jobs, until) VALUES (?,?,?,?,?,?)",
                 (db.now_iso(), action, by, reason, json.dumps(jobs) if jobs else None, until))


def _parts(p):
    """A pause's separate requests (each with its own jobs and end); an older single pause is one part."""
    return p.get("parts") or [{k: p.get(k) for k in ("all", "jobs", "mode", "by", "reason", "since", "until")}]


def _combine(parts):
    """The pause that several requests make together: every job any of them covers, until the last one ends."""
    if not parts:
        return None
    every = any(x.get("all") for x in parts)
    jobs = None if every else sorted({j for x in parts for j in x.get("jobs") or []})
    return {"all": every, "jobs": jobs, "mode": parts[-1].get("mode"), "by": parts[-1].get("by"),
            "reason": "; ".join(dict.fromkeys(x.get("reason") or "" for x in parts if x.get("reason"))),
            "since": min(x["since"] for x in parts), "until": max(x["until"] for x in parts), "parts": parts}


def _save(conn, p):
    if p:
        conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (KEY, json.dumps(p)))
    else:
        conn.execute("DELETE FROM state WHERE key=?", (KEY,))


def get(conn):
    """The active pause as a dict (all, jobs, mode, by, reason, since, until, parts), or None. Requests that have run
    out are dropped here (and logged), so a pause ends even if nobody presses Resume."""
    row = conn.execute("SELECT value FROM state WHERE key=?", (KEY,)).fetchone()
    if not row:
        return None
    try:
        p = json.loads(row[0])
    except (TypeError, ValueError):
        p = None
    now = _now().isoformat(timespec="seconds")
    parts = _parts(p) if p else []
    live = [x for x in parts if (x.get("until") or "") > now]
    if len(live) != len(parts) or not p:
        for x in parts:
            if x not in live:
                _log(conn, "expired", "auto", x.get("reason"), x.get("jobs"), x.get("until"))
        p = _combine(live)
        _save(conn, p)
        conn.commit()
    return p


def covers(p, job):
    """True if pause p covers this job."""
    return bool(p) and (p.get("all") or job in (p.get("jobs") or []))


def is_paused(conn, job):
    return covers(get(conn), job)


def pause(conn, by, reason="", minutes=DEFAULT_MINUTES, jobs=None, now=False):
    """Start a pause. jobs=None pauses every job. A pause already running is kept: the new request is added to it
    (each request covers its own jobs until its own end). Returns the combined pause."""
    minutes = max(1, min(int(minutes or DEFAULT_MINUTES), MAX_MINUTES))
    jobs = sorted(set(jobs)) if jobs else None
    part = {"all": jobs is None, "jobs": jobs, "mode": "now" if now else "finish", "by": by, "reason": reason or "",
            "since": db.now_iso(), "until": (_now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")}
    old = get(conn)
    p = _combine((_parts(old) if old else []) + [part])
    _save(conn, p)
    _log(conn, "pause_now" if now else "pause", by, reason, jobs, part["until"])
    conn.commit()
    return p


def resume(conn, by, reason="", jobs=None):
    """End the pause (jobs=None), or lift only the listed jobs: requests for those jobs alone end, and the jobs are
    taken out of the others (a pause of every job stays). Returns the pause as it was, or None if there was none."""
    p = get(conn)
    if not p:
        conn.commit()
        return None
    if jobs:
        lift = set(jobs)
        rest = []
        for x in _parts(p):
            if x.get("all"):
                rest.append(x)
            else:
                left = sorted(set(x.get("jobs") or []) - lift)
                if left:
                    rest.append(dict(x, jobs=left))
        _save(conn, _combine(rest))
    else:
        _save(conn, None)
    _log(conn, "resume", by, reason or p.get("reason"), sorted(jobs) if jobs else p.get("jobs"))
    conn.commit()
    return p


def history(conn, limit=20):
    ensure(conn)
    rows = conn.execute("SELECT at, action, by, reason, jobs, until FROM pause_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(zip(("at", "action", "by", "reason", "jobs", "until"), r), jobs=json.loads(r[4]) if r[4] else None)
            for r in rows]


def minutes_paused(p):
    return (_now() - datetime.fromisoformat(p["since"]).astimezone(timezone.utc)).total_seconds() / 60 if p else 0
