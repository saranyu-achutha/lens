"""Crawl statistics for the Developer side: what ran, how much it read, what failed, per platform and period.

Two tables, filled from run_log (so past runs are included) whenever the Crawl stats page asks:
  crawl_stat      one row per finished run per source. A step row (is_step=1: youtube, songs, ytmusic, jiosaavn,
                  spotify_plays, daily, spotify) has the step's status, seconds and any failure; the songs step also
                  has metric rows for what it does inside (apple, musicbrainz, deezer, spotify_search, spotify_links).
  crawl_readings  per day and platform: items tracked and readings captured, from snapshot.
"""
import json
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_stat (
    run_id      INTEGER NOT NULL,
    source      TEXT NOT NULL,
    job         TEXT,
    step        TEXT,
    is_step     INTEGER DEFAULT 0,
    day         TEXT NOT NULL,
    started_at  TEXT,
    status      TEXT,
    seconds     REAL,
    items_read  INTEGER DEFAULT 0,
    found       INTEGER DEFAULT 0,
    missing     INTEGER DEFAULT 0,
    quota_units INTEGER DEFAULT 0,
    api_calls   INTEGER DEFAULT 0,
    extra       TEXT,     -- every number the run reported (JSON, on the first step row): nothing is lost for later use
    kind        TEXT,     -- failure type: quota, browser, timeout, disk, network, database, cancelled, interrupted, other
    message     TEXT,
    PRIMARY KEY (run_id, source)
);
CREATE INDEX IF NOT EXISTS crawl_stat_day ON crawl_stat(day, source);
CREATE TABLE IF NOT EXISTS crawl_readings (
    day      TEXT NOT NULL,
    platform TEXT NOT NULL,
    readings INTEGER,
    items    INTEGER,
    PRIMARY KEY (day, platform)
);
"""
# One line per source; a new platform or route only needs a line here (any new job step is recorded by itself).
#   step: the job step it runs in; access: how we reach it; read / found / missing / quota / calls: run_log stats keys,
#   summed. `calls` may hold a number instead: stats keys are divided by it (e.g. 100 quota units = one search).
METRICS = {
    "youtube": dict(step="youtube", access="API (key)", read=["videos_read"], found=["new_videos"], quota=["quota_units"], calls=["videos_read"], per=50),
    "ytmusic": dict(step="ytmusic", access="API (key)", read=["ytmusic_read"], found=["ytmusic_found"], missing=["ytmusic_missing"], quota=["ytmusic_quota_units"], calls=["ytmusic_found", "ytmusic_missing"]),
    "jiosaavn": dict(step="jiosaavn", access="Public API", read=["jiosaavn_read"], found=["jiosaavn_found"], missing=["jiosaavn_missing"], calls=["jiosaavn_found", "jiosaavn_missing"]),
    "spotify_plays": dict(step="spotify_plays", access="Public page (browser)", read=["spotify_plays_read"], calls=["spotify_plays_read"]),
    "apple": dict(step="songs", access="Public API", read=["itunes_matched", "itunes_not_found"], found=["itunes_matched"], missing=["itunes_not_found"], calls=["itunes_matched", "itunes_not_found"]),
    "musicbrainz": dict(step="songs", access="Open API", read=["musicbrainz_matched", "musicbrainz_not_found"], found=["musicbrainz_matched"], missing=["musicbrainz_not_found"], calls=["musicbrainz_matched", "musicbrainz_not_found"]),
    "deezer": dict(step="songs", access="Public API", read=["deezer_read"], found=["deezer_read"], missing=["deezer_missing"], calls=["deezer_read", "deezer_missing"]),
    "spotify_search": dict(step="songs", access="API (key)", read=["spotify_searches", "spotify_artist_calls"], found=["spotify_matched", "spotify_artist_matched"], missing=["spotify_not_found"], calls=["spotify_searches", "spotify_artist_calls"]),
    "spotify_playlists": dict(step="songs", access="Public embed page", read=["spotify_playlist_tracks"], found=["spotify_playlist_links"], calls=["spotify_playlist_pages"]),
    "spotify_web": dict(step="songs", access="Web search", read=["spotify_web_searches"], found=["spotify_links_web"], calls=["spotify_web_searches"]),
    "spotify_mb_links": dict(step="songs", access="Open API", found=["spotify_links_musicbrainz"]),
    "odesli": dict(step="songs", access="API (key)", found=["spotify_links_odesli"]),
    "spotify": dict(step="spotify", access="API (key)", read=["tracks"], found=["releases"], calls=["api_calls"]),
}
STEP_NAMES = ("youtube", "songs", "ytmusic", "jiosaavn", "spotify_plays", "daily", "spotify")
API_ACCESS = ("API (key)", "Public API", "Open API")
PROBLEM = ("failed", "partial", "quota_exceeded", "interrupted")


def ensure(conn):
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(crawl_stat)")}
    for col, typ in (("api_calls", "INTEGER DEFAULT 0"), ("extra", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE crawl_stat ADD COLUMN {col} {typ}")


def _when(text):
    """Run times are ISO strings, some without a time zone."""
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def classify(status, message):
    """Failure type from the status and message."""
    if status == "cancelled":
        return "cancelled"
    if status == "interrupted":
        return "interrupted"
    m = (message or "").lower()
    if not m and status == "ok":
        return None
    if any(w in m for w in ("quota", "429", "ratelimit", "search limit", "too many")):
        return "quota"
    if any(w in m for w in ("403", "401", "forbidden", "unauthorized")):
        return "access denied"
    if any(w in m for w in ("connection refused", "no browser", "chrome", "sandbox")):
        return "browser"
    if "timed out" in m or "timeout" in m:
        return "timeout"
    if "disk" in m:
        return "disk"
    if "database" in m or "locked" in m:
        return "database"
    if any(w in m for w in ("urlerror", "name or service", "network", "unreachable", "reset by peer")):
        return "network"
    return "other"


def record(conn, row):
    """Rows of one finished run_log row."""
    run_id, job, started, finished, status, stats = row
    st = json.loads(stats or "{}")
    day = started[:10]
    seconds = None
    if started and finished:
        seconds = (_when(finished) - _when(started)).total_seconds()
    steps = st.get("steps") or {}
    if not steps:  # older runs and runs stopped by a restart have no step list
        steps = {job if job in STEP_NAMES else {"youtube_sync": "youtube", "spotify_discovery": "spotify"}.get(job, "songs" if job == "pipeline" else job):
                 {"status": status, "error": st.get("error"), "stop_reason": st.get("stop_reason")}}
    numbers = json.dumps({k: v for k, v in st.items() if isinstance(v, (int, float))})
    for i, (name, s) in enumerate(steps.items()):
        msg = s.get("error") or s.get("stop_reason") or ""
        stat = s.get("status") or status
        conn.execute("""INSERT OR REPLACE INTO crawl_stat (run_id, source, job, step, is_step, day, started_at, status, seconds, kind, message, extra)
                        VALUES (?,?,?,?,1,?,?,?,?,?,?,?)""",
                     (run_id, name, job, name, day, started, stat, s.get("seconds") or (seconds if len(steps) == 1 else None),
                      classify(stat, msg) if stat != "ok" else None, msg[:500], numbers if i == 0 else None))
    for source, m in METRICS.items():
        step = m["step"]
        if step not in steps:
            continue
        total = lambda name: sum(int(st.get(k) or 0) for k in m.get(name, []))
        read, found, missing, quota = total("read"), total("found"), total("missing"), total("quota")
        calls = total("calls")
        if m.get("per"):
            calls = (calls + m["per"] - 1) // m["per"]
        if source == "spotify_plays" and read:  # this count is the day's running total
            prev = conn.execute("SELECT coalesce(sum(items_read), 0) FROM crawl_stat WHERE source='spotify_plays' AND day=? AND run_id<?", (day, run_id)).fetchone()[0]
            read = calls = max(0, read - prev)
        if source in steps:
            conn.execute("UPDATE crawl_stat SET items_read=?, found=?, missing=?, quota_units=?, api_calls=? WHERE run_id=? AND source=?",
                         (read, found, missing, quota, calls, run_id, source))
        elif read or found or missing or quota or calls:
            conn.execute("""INSERT OR REPLACE INTO crawl_stat (run_id, source, job, step, is_step, day, started_at, status, items_read, found, missing, quota_units, api_calls)
                            VALUES (?,?,?,?,0,?,?,?,?,?,?,?,?)""",
                         (run_id, source, job, step, day, started, steps[step].get("status") or status, read, found, missing, quota, calls))


def sync(conn):
    """Add every finished run not recorded yet, and refresh the recent readings counts."""
    ensure(conn)
    rows = conn.execute("""SELECT id, job, started_at, finished_at, status, stats FROM run_log WHERE status != 'running'
                           AND id NOT IN (SELECT DISTINCT run_id FROM crawl_stat) ORDER BY started_at""").fetchall()
    for r in rows:
        record(conn, tuple(r))
    # Readings against the items of the song period (only those are read daily). Version 2 of this count replaces older rows.
    if not conn.execute("SELECT 1 FROM state WHERE key='crawl_readings_v2'").fetchone():
        conn.execute("DELETE FROM crawl_readings")
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('crawl_readings_v2', '1')")
    window_start = config.window(config.load())[0]
    have = conn.execute("SELECT max(day) FROM crawl_readings").fetchone()[0]
    since = "0" if not have else conn.execute("SELECT date(?, '-2 day')", (have,)).fetchone()[0]
    conn.execute("DELETE FROM crawl_readings WHERE day >= ?", (since,))
    tracked = dict(conn.execute("""SELECT p.platform, count(*) FROM platform_item p JOIN track t ON t.id=p.track_id
                                   WHERE t.first_published >= ? AND p.review_state IN ('auto','approved','song') GROUP BY 1""", (window_start,)).fetchall())
    for day, plat, n in conn.execute("""SELECT s.capture_date, p.platform, count(*) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                                        JOIN track t ON t.id=p.track_id
                                        WHERE s.capture_date >= ? AND t.first_published >= ? AND p.review_state IN ('auto','approved','song')
                                          AND s.metric IN ('views', 'plays', 'rank') GROUP BY 1, 2""", (since, window_start)).fetchall():
        conn.execute("INSERT OR REPLACE INTO crawl_readings (day, platform, readings, items) VALUES (?,?,?,?)", (day, plat, n, tracked.get(plat)))
    conn.commit()
    return len(rows)
