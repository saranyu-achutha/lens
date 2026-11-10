"""Alerts for the Developer console: conditions checked every 10 minutes by the worker (and when the bell is opened),
remembered in `alert` so each has a "since", can be snoozed, and closes itself when the condition clears.

  charts_token        the Spotify India chart is behind: paste a fresh charts.spotify.com session token (form in the Console)
  stale:<source>      a source's newest reading is older than it should be (expired token, blocked browser, changed page)
  zero:<job>          a page reader read nothing in its runs of the last 24 hours
  failing:<job>       a job's last 3 runs failed
  disk                disk nearly full, or filling up within two weeks at the current rate
  backup              no nightly database backup in the last 30 hours
  spotify_api         the Spotify API health check is failing
  review              matches waiting for review
  paused              jobs paused for more than 30 minutes
  heal:<reader>       a page reader is on a fallback (the page changed) or held by its guard (lens/heal.py)
  suspect:<reader>    20+ values held as suspicious today
Severity: critical (data is being lost now), warning (needs attention soon), info.
"""
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from . import config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert (
    key            TEXT PRIMARY KEY,
    severity       TEXT,
    title          TEXT,
    detail         TEXT,
    action         TEXT,      -- what the Console offers to fix it: charts_token, open:<page>
    first_seen     TEXT,
    last_seen      TEXT,
    resolved_at    TEXT,      -- set when the condition clears; a new occurrence starts a new 'since'
    snoozed_until  TEXT
);
"""
# Newest reading per source, and the age in days after which it counts as stale.
FRESHNESS = [
    ("YouTube views", "SELECT max(s.capture_date) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id WHERE p.platform='youtube' AND s.metric='views'", 1),
    ("YouTube Music plays", "SELECT max(s.capture_date) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id WHERE p.platform='ytmusic' AND s.metric='plays'", 1),
    ("Spotify plays (pages)", "SELECT max(s.capture_date) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id WHERE p.platform='spotify' AND s.metric='plays'", 1),
    ("JioSaavn plays", "SELECT max(s.capture_date) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id WHERE p.platform='jiosaavn' AND s.metric='plays'", 1),
    ("Deezer rank", "SELECT max(s.capture_date) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id WHERE p.platform='deezer' AND s.metric='rank'", 1),
    ("YouTube Shorts (sound pages)", "SELECT max(day) FROM sound_use WHERE platform='youtube'", 1),
    ("Instagram Reels (audio pages)", "SELECT max(day) FROM sound_use WHERE platform='instagram'", 1),
    ("Spotify India chart", "SELECT max(chart_date) FROM chart_entry WHERE chart='regional-in-daily'", 2),
]
READERS = {"shorts": ("shorts_sounds_read", "shorts_pages_failed"), "instagram": ("instagram_read", "instagram_failed"),
           "jiosaavn": ("jiosaavn_read", None), "spotify_plays": ("spotify_plays_read", None)}
CHARTS = {"running": False}  # the background chart import started from the token form


def ensure(conn):
    conn.executescript(SCHEMA)


def freshness(conn, today=None):
    """Newest reading per source, and page readers that read nothing in their runs of the last 24 hours."""
    from . import shorts_use
    shorts_use.ensure(conn)
    today = today or date.today().isoformat()
    out = []
    for name, sql, max_age in FRESHNESS:
        try:
            last = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            last = None
        age = (date.fromisoformat(today) - date.fromisoformat(last[:10])).days if last else None
        out.append({"source": name, "last": last, "age_days": age, "ok": age is not None and age <= max_age, "max_age": max_age})
    zero = []
    since = (datetime.now().astimezone() - timedelta(hours=24)).isoformat(timespec="seconds")
    for job, (read_key, fail_key) in READERS.items():
        runs = [json.loads(r[0] or "{}") for r in conn.execute("SELECT stats FROM run_log WHERE started_at >= ? AND stats LIKE ?", (since, f'%"{read_key}"%'))]
        read = sum(int(r.get(read_key) or 0) for r in runs)
        failed = sum(int(r.get(fail_key) or 0) for r in runs) if fail_key else 0
        if runs and read == 0 and (failed or not fail_key):
            zero.append({"job": job, "runs": len(runs), "failed": failed})
    return {"sources": out, "zero_reads": zero}


def _state(conn, key):
    r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and r[0] else {}


def evaluate(conn):
    """The conditions that hold right now: [{key, severity, title, detail, action}]."""
    from . import health, sysstats
    today = date.today().isoformat()
    out = []
    fr = freshness(conn, today)
    for s in fr["sources"]:
        if s["ok"]:
            continue
        if s["source"] == "Spotify India chart":
            last = _state(conn, "charts_fetch")
            out.append({"key": "charts_token", "severity": "warning", "action": "charts_token",
                        "title": "Spotify India chart needs a fresh session token",
                        "detail": f"Newest chart: {s['last'] or 'none'}. Spotify publishes it daily; importing needs a token from a "
                                  f"logged-in charts.spotify.com session (it expires after about an hour)."
                                  + (f" Last import: {last.get('message')}" if last.get("message") else "")})
            continue
        days = s["age_days"]
        if days is None:  # never read yet (e.g. no Instagram audio linked): nothing has stopped
            continue
        out.append({"key": f"stale:{s['source']}", "severity": "critical" if days >= 3 else "warning", "action": "open:dev",
                    "title": f"{s['source']}: no new reading for {days} day{'s' if days != 1 else ''}",
                    "detail": "The job that reads it may be failing (expired access, blocked browser, or the page changed). See Runs and Crawl stats."})
    for z in fr["zero_reads"]:
        out.append({"key": f"zero:{z['job']}", "severity": "warning", "action": "open:crawl",
                    "title": f"{z['job']} read nothing in the last 24 hours",
                    "detail": f"{z['runs']} run(s){', ' + str(z['failed']) + ' pages not readable' if z['failed'] else ''}. A page layout may have changed, or access is blocked."})
    # A job whose last 3 runs all failed
    for (job,) in conn.execute("SELECT DISTINCT job FROM run_log WHERE started_at >= date('now', '-2 day')").fetchall():
        last3 = conn.execute("SELECT status, stats FROM run_log WHERE job=? AND status NOT IN ('running','cancelled') ORDER BY id DESC LIMIT 3", (job,)).fetchall()
        if len(last3) == 3 and all(r[0] == "failed" for r in last3):
            err = json.loads(last3[0][1] or "{}").get("error") or ""
            out.append({"key": f"failing:{job}", "severity": "critical", "action": "open:runs",
                        "title": f"{job}: the last 3 runs failed", "detail": err[:300]})
    # Disk
    c = sysstats.counters()
    free_pct = 100 * c["disk_free"] / c["disk_total"] if c["disk_total"] else 100
    days_left = sysstats.history(conn)["disk_days_left"]
    if free_pct < 15 or (days_left is not None and days_left < 14):
        out.append({"key": "disk", "severity": "critical" if free_pct < 8 else "warning", "action": "open:dev",
                    "title": f"Disk {100 - free_pct:.0f}% full ({c['disk_free'] / 1e9:.1f} GB free)",
                    "detail": (f"At the current rate it fills in about {days_left} days. " if days_left is not None else "")
                              + "See Disk use on the Developer dashboard."})
    # Nightly backup
    folder = config.DB_FILE.parent / "backups"
    backups = [f for f in folder.glob("lens_*.db.gz")] if folder.exists() else []
    newest = max((f.stat().st_mtime for f in backups), default=None)
    if newest is None or datetime.now().timestamp() - newest > 30 * 3600:
        out.append({"key": "backup", "severity": "warning", "action": "open:dev",
                    "title": "No database backup in the last 30 hours",
                    "detail": "The nightly backup (02:30 UTC, lens_backup.sh) did not write a new file. Check ~/lens_backup.log on the server."})
    # Spotify API
    last = health.last_check(conn)
    if last and not last["ok"]:
        out.append({"key": "spotify_api", "severity": "warning", "action": "open:sources",
                    "title": "Spotify API is not answering normally",
                    "detail": f"{last.get('message') or ''} Retry after {last.get('retry_at') or 'the next check'}."})
    # Jobs paused for a long time (a pause always ends by itself, but daily data waits meanwhile)
    from . import pause
    p = pause.get(conn)
    if p and pause.minutes_paused(p) > pause.ALERT_AFTER_MINUTES:
        what = "All jobs" if p.get("all") else ", ".join(p.get("jobs") or [])
        out.append({"key": "paused", "severity": "warning", "action": "open:dev",
                    "title": f"{what} paused for {pause.minutes_paused(p):.0f} minutes",
                    "detail": f"Paused by {p.get('by') or '?'}{' (' + p['reason'] + ')' if p.get('reason') else ''}; "
                              f"resumes by itself at {p['until'][:16].replace('T', ' ')} UTC. Resume on the Developer dashboard."})
    # Page readers (lens/heal.py): a page changed and a fallback reads it, a reader held by its guard, suspicious values
    from . import heal
    out += heal.alert_conditions(conn)
    pending = conn.execute("SELECT count(*) FROM platform_item WHERE review_state='pending'").fetchone()[0]
    if pending:
        out.append({"key": "review", "severity": "info", "action": "open:review",
                    "title": f"{pending} match{'es' if pending != 1 else ''} waiting for review", "detail": "Open Review to approve or reject them."})
    return out


def sync(conn):
    """Store the current conditions: new ones open, existing ones refresh, absent ones close."""
    ensure(conn)
    now = db.now_iso()
    current = {a["key"]: a for a in evaluate(conn)}
    for k, a in current.items():
        row = conn.execute("SELECT resolved_at FROM alert WHERE key=?", (k,)).fetchone()
        if row is None or row[0]:  # new, or back after it had cleared: a fresh 'since', no old snooze
            conn.execute("""INSERT OR REPLACE INTO alert (key, severity, title, detail, action, first_seen, last_seen, resolved_at, snoozed_until)
                            VALUES (?,?,?,?,?,?,?,NULL,NULL)""", (k, a["severity"], a["title"], a["detail"], a["action"], now, now))
        else:
            conn.execute("UPDATE alert SET severity=?, title=?, detail=?, action=?, last_seen=? WHERE key=?",
                         (a["severity"], a["title"], a["detail"], a["action"], now, k))
    for (k,) in conn.execute("SELECT key FROM alert WHERE resolved_at IS NULL").fetchall():
        if k not in current:
            conn.execute("UPDATE alert SET resolved_at=? WHERE key=?", (now, k))
    conn.commit()


def listing(conn):
    ensure(conn)
    now = db.now_iso()
    rank = {"critical": 0, "warning": 1, "info": 2}
    active = [dict(r) for r in conn.execute("SELECT * FROM alert WHERE resolved_at IS NULL")]
    for a in active:
        a["snoozed"] = bool(a["snoozed_until"] and a["snoozed_until"] > now)
    active.sort(key=lambda a: (a["snoozed"], rank.get(a["severity"], 3), a["first_seen"]))
    resolved = [dict(r) for r in conn.execute("SELECT key, severity, title, first_seen, resolved_at FROM alert WHERE resolved_at >= ? ORDER BY resolved_at DESC LIMIT 10",
                                              ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds"),))]
    return {"active": active, "resolved": resolved, "charts": dict(_state(conn, "charts_fetch"), running=CHARTS["running"]),
            "count": sum(1 for a in active if not a["snoozed"] and a["severity"] in ("critical", "warning"))}


def snooze(conn, key, hours=24):
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds") if hours else None
    conn.execute("UPDATE alert SET snoozed_until=? WHERE key=?", (until, key))
    conn.commit()


# ---- Spotify chart token: saved from the Console form, then the missing chart days are imported in the background
def save_charts_token(token):
    tok = (token or "").strip().removeprefix("Bearer ").removeprefix("bearer ").strip()
    if len(tok) < 100 or any(ch.isspace() for ch in tok):
        raise ValueError("that doesn't look like a charts.spotify.com token (it starts with BQ… and is a few hundred characters)")
    config.set_env("SPOTIFY_CHARTS_TOKEN", tok)
    conn = db.connect(str(config.DB_FILE))
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('charts_token_at', ?)", (json.dumps(db.now_iso()),))
    conn.commit()
    conn.close()


def charts_status(conn):
    """For the Spotify chart page: what is stored, the last import, and when a token was last saved (never the token)."""
    from . import spotify_charts
    spotify_charts.ensure(conn)
    one = lambda sql: conn.execute(sql).fetchone()
    daily = one("SELECT max(chart_date), count(DISTINCT chart_date) FROM chart_entry WHERE chart='regional-in-daily'")
    weekly = one("SELECT max(chart_date), count(DISTINCT chart_date) FROM chart_entry WHERE chart='regional-in-weekly'")
    ours = one("SELECT count(*) FROM song_daily WHERE chart_rank IS NOT NULL AND day=(SELECT max(chart_date) FROM chart_entry WHERE chart='regional-in-daily')")[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='song_daily'").fetchone() else 0
    age = (date.today() - date.fromisoformat(daily[0])).days if daily[0] else None
    return {"daily_latest": daily[0], "daily_count": daily[1], "weekly_latest": weekly[0], "weekly_count": weekly[1],
            "our_songs_on_latest": ours, "age_days": age, "behind": age is None or age > 2,
            "token_saved_at": _state(conn, "charts_token_at") or None, "last_import": _state(conn, "charts_fetch"), "running": CHARTS["running"]}


def import_charts():
    """Fetch the chart days missing since the newest one stored, import them, rebuild those days' song totals.
    Runs in its own thread (a handful of small requests); the outcome is kept in state 'charts_fetch'."""
    if CHARTS["running"]:
        return False

    def work():
        from . import song_daily, spotify_charts
        conn = db.connect(str(config.DB_FILE))
        msgs = []
        try:
            spotify_charts.ensure(conn)
            last = conn.execute("SELECT max(chart_date) FROM chart_entry WHERE chart='regional-in-daily'").fetchone()[0]
            since = date.fromisoformat(last) - timedelta(days=7) if last else date.today() - timedelta(days=30)
            ok = True
            try:
                spotify_charts.fetch(since, log=msgs.append)
            except SystemExit as e:  # token rejected or repeated errors: what was fetched is kept
                ok, msgs = False, msgs + [str(e)]
            spotify_charts.import_files(conn, log=msgs.append)
            new_last = conn.execute("SELECT max(chart_date) FROM chart_entry WHERE chart='regional-in-daily'").fetchone()[0]
            if new_last and (not last or new_last > last):
                song_daily.build(conn, [d[0] for d in conn.execute("SELECT DISTINCT capture_date FROM snapshot WHERE capture_date > ?", (last or "0",))], log=msgs.append)
            msg = (f"charts up to {new_last}" if ok else f"stopped: {msgs[-1]}") + f" (newest before: {last})"
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('charts_fetch', ?)",
                         (json.dumps({"at": db.now_iso(), "ok": ok, "message": msg, "latest": new_last}),))
            conn.commit()
            sync(conn)
        except Exception as e:
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('charts_fetch', ?)",
                         (json.dumps({"at": db.now_iso(), "ok": False, "message": f"error: {e!r}"[:300]}),))
            conn.commit()
        finally:
            conn.close()
            CHARTS["running"] = False

    CHARTS["running"] = True
    threading.Thread(target=work, daemon=True).start()
    return True
