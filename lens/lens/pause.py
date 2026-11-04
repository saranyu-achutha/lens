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


def get(conn):
    """The active pause as a dict (all, jobs, mode, by, reason, since, until), or None. An expired pause is
    cleared here (and logged), so it ends even if nobody presses Resume."""
    row = conn.execute("SELECT value FROM state WHERE key=?", (KEY,)).fetchone()
    if not row:
        return None
    try:
        p = json.loads(row[0])
    except (TypeError, ValueError):
        p = None
    if not p or (p.get("until") or "") <= _now().isoformat(timespec="seconds"):
        conn.execute("DELETE FROM state WHERE key=?", (KEY,))
        if p:
            _log(conn, "expired", "auto", p.get("reason"), p.get("jobs"), p.get("until"))
        conn.commit()
        return None
    return p


def covers(p, job):
    """True if pause p covers this job."""
    return bool(p) and (p.get("all") or job in (p.get("jobs") or []))


def is_paused(conn, job):
    return covers(get(conn), job)


def pause(conn, by, reason="", minutes=DEFAULT_MINUTES, jobs=None, now=False):
    """Start (or replace) a pause. jobs=None pauses every job. Returns the pause."""
    minutes = max(1, min(int(minutes or DEFAULT_MINUTES), MAX_MINUTES))
    jobs = sorted(set(jobs)) if jobs else None
    p = {"all": jobs is None, "jobs": jobs, "mode": "now" if now else "finish", "by": by, "reason": reason or "",
         "since": db.now_iso(), "until": (_now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")}
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (KEY, json.dumps(p)))
    _log(conn, "pause_now" if now else "pause", by, reason, jobs, p["until"])
    conn.commit()
    return p


def resume(conn, by, reason=""):
    """End the pause. Returns the pause that ended, or None if there was none."""
    p = get(conn)
    conn.execute("DELETE FROM state WHERE key=?", (KEY,))
    if p:
        _log(conn, "resume", by, reason or p.get("reason"), p.get("jobs"))
    conn.commit()
    return p


def history(conn, limit=20):
    ensure(conn)
    rows = conn.execute("SELECT at, action, by, reason, jobs, until FROM pause_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(zip(("at", "action", "by", "reason", "jobs", "until"), r), jobs=json.loads(r[4]) if r[4] else None)
            for r in rows]


def minutes_paused(p):
    return (_now() - datetime.fromisoformat(p["since"]).astimezone(timezone.utc)).total_seconds() / 60 if p else 0
