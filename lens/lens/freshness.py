"""Readings within 24 hours (user rule, 2026-11-06): every song of the period (from 2026-07-01, or freshness.since), not
released before the period, is read on every platform it is linked to at least once in any 24 hours: YouTube views,
likes and comments, Spotify plays, JioSaavn plays, YouTube Music plays, Deezer rank, Shorts and Reels counts.

  * missed: an item last read more than freshness.hours (24) ago, or linked that long ago and never read
  * the worker's watchdog: a platform with missed items gets its job started straight away (at most every
    freshness.retry_minutes, 30), ahead of the usual order, until they are read; failed runs are retried the same way
  * the Console page "Missed readings" (/api/freshness) lists every missed item with its last read and the last error

A read that gives no number still counts (table read_mark): Spotify hides play counts under 1,000, and a track page can be
gone. Apple and MusicBrainz are metadata only (no daily reading).
"""
import json
from datetime import datetime, timedelta, timezone

from . import config, pause

SCHEMA = """
CREATE TABLE IF NOT EXISTS read_mark (
    platform_item_id INTEGER NOT NULL,
    metric           TEXT NOT NULL,
    read_at          TEXT NOT NULL,
    outcome          TEXT,              -- hidden (under 1,000 plays) | gone | ...
    PRIMARY KEY (platform_item_id, metric)
);
"""
# platform -> (metric, the job that reads it)
PLATFORMS = {
    "youtube":  ("views", "youtube_sync"),
    "spotify":  ("plays", "spotify_plays"),
    "jiosaavn": ("plays", "jiosaavn"),
    "ytmusic":  ("plays", "ytmusic"),
    "deezer":   ("rank", "songs"),
    "shorts":   ("shorts", "shorts"),
    "instagram": ("reels", "instagram"),
}
_HIST = {}  # platform -> (missed items when the watchdog last started its job, runs in a row that did not reduce them)
STUCK_AFTER, STUCK_MINUTES = 3, 180  # after 3 runs that read none of them, try every 3 hours instead of every 30 minutes
HOLD_DAYS = 7      # "Mark unavailable" on the page: not shown as missed for 7 days (then checked again)
NOSOUND_DAYS = 7   # shorts_use.NO_SOUND_RECHECK_DAYS
NAMES = {"youtube": "YouTube (views, likes, comments)", "spotify": "Spotify plays", "jiosaavn": "JioSaavn plays",
         "ytmusic": "YouTube Music plays", "deezer": "Deezer rank", "shorts": "YouTube Shorts", "instagram": "Instagram Reels"}


def ensure(conn):
    conn.executescript(SCHEMA)


def settings(cfg=None):
    f = (cfg or config.load()).get("freshness") or {}  # since: the period start (2026-07-01) unless set here
    cfg = cfg or config.load()
    return {"since": f.get("since") or config.window(cfg)[0], "hours": float(f.get("hours", 24)),
            "retry_minutes": float(f.get("retry_minutes", 30)), "watchdog": f.get("watchdog", True)}


def mark(conn, item_id, metric, outcome, at=None):
    """A read that gave no number (hidden count, page gone): it still counts as read."""
    ensure(conn)
    conn.execute("""INSERT INTO read_mark (platform_item_id, metric, read_at, outcome) VALUES (?,?,?,?)
                    ON CONFLICT(platform_item_id, metric) DO UPDATE SET read_at=excluded.read_at, outcome=excluded.outcome""",
                 (item_id, metric, at or datetime.now(timezone.utc).isoformat(timespec="seconds"), outcome))


def _items_sql():
    """Every item to read of the songs in scope, with its last read: a reading, or a read that gave no number (read_mark,
    JioSaavn's hidden counts, Deezer's ISRC not found, an upload without a sound page)."""
    mk = lambda metric: f"""coalesce((SELECT CASE WHEN m.outcome='unavailable' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00', m.read_at, '+{HOLD_DAYS} days')
                                                   ELSE m.read_at END FROM read_mark m WHERE m.platform_item_id=p.id AND m.metric='{metric}'), '')"""
    snap = lambda metric: f"coalesce((SELECT max(s.captured_at) FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='{metric}'), '')"
    day = lambda key: f"coalesce((SELECT value || 'T00:00:00+00:00' FROM state WHERE key={key}), '')"
    parts = [
        f"""SELECT 'youtube' platform, p.id item_id, p.track_id song_id, p.title item_title, p.first_seen_at, max({snap('views')}, {mk('views')}) last_read
            FROM platform_item p WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song')""",
        f"""SELECT 'spotify', p.id, p.track_id, p.title, p.first_seen_at, max({snap('plays')}, {mk('plays')})
            FROM platform_item p WHERE p.platform='spotify' AND p.review_state IN ('auto','approved')""",
        f"""SELECT 'jiosaavn', p.id, p.track_id, p.title, p.first_seen_at, max({snap('plays')}, {mk('plays')}, {day("'js_hidden:' || p.id")})
            FROM platform_item p WHERE p.platform='jiosaavn' AND p.review_state IN ('auto','approved')""",
        f"""SELECT 'ytmusic', p.id, p.track_id, p.title, p.first_seen_at, max({snap('plays')}, {mk('plays')})
            FROM platform_item p WHERE p.platform='ytmusic' AND p.review_state IN ('auto','approved')""",
        # Deezer: per song (its ISRC may move to another Deezer item); "ISRC not found" counts as read
        f"""SELECT 'deezer', min(p.id), p.track_id, max(p.title), min(p.first_seen_at),
                   max(max({snap('rank')}, {mk('rank')}, {day("'deezer_missing:' || (SELECT isrc FROM track x WHERE x.id=p.track_id)")}))
            FROM platform_item p WHERE p.platform='deezer' AND p.review_state IN ('auto','approved') AND p.track_id IS NOT NULL
            GROUP BY p.track_id""",
        # Shorts: label uploads whose official sound has been counted before (an upload without a sound page counts as read)
        f"""SELECT 'shorts', p.id, p.track_id, p.title, p.first_seen_at,
                   max(coalesce((SELECT max(u.captured_at) FROM sound_use u WHERE u.platform_item_id=p.id AND u.platform='youtube'), ''), {mk('shorts')})
            FROM platform_item p WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song')
              AND EXISTS (SELECT 1 FROM sound_use u WHERE u.platform_item_id=p.id AND u.platform='youtube' AND u.official=1)
              -- no sound page found: nothing to count until the Shorts job's weekly recheck
              AND coalesce((SELECT value FROM state WHERE key='shorts_nosound:' || p.id), '') <= date('now', '-{NOSOUND_DAYS} day')""",
        f"""SELECT 'instagram', p.id, p.track_id, p.title, p.first_seen_at,
                  max(coalesce((SELECT max(u.captured_at) FROM sound_use u WHERE u.platform_item_id=p.id AND u.platform='instagram'), ''), {mk('reels')})
           FROM platform_item p WHERE p.platform='instagram' AND p.track_id IS NOT NULL"""]
    return " UNION ALL ".join(parts)


def missed(conn, cfg=None, platform=None, limit=None):
    """The items of the songs in scope not read within the hours: newest songs first."""
    ensure(conn)
    s = settings(cfg)
    cut = (datetime.now(timezone.utc) - timedelta(hours=s["hours"])).isoformat(timespec="seconds")
    labels = config.channel_labels(cfg or config.load())
    rows = conn.execute(f"""
        WITH i AS ({_items_sql()})
        SELECT i.*, coalesce(t.yt_title, t.title) song, substr(t.first_published,1,10) first_upload,
               (SELECT channel FROM platform_item y WHERE y.track_id=t.id AND y.platform='youtube' ORDER BY published_at LIMIT 1) song_channel
        FROM i JOIN track t ON t.id=i.song_id
        WHERE t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL
          AND ((i.last_read = '' AND coalesce(i.first_seen_at, '') < ?) OR (i.last_read <> '' AND i.last_read < ?))
          {"AND i.platform = ?" if platform else ""}
        ORDER BY t.first_published DESC, i.platform""",
                        (s["since"], cut, cut) + ((platform,) if platform else ())).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for r in rows[:limit] if limit else rows:
        last = r["last_read"] or None
        hours = (now - datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds() / 3600 if last else None
        out.append({"platform": r["platform"], "item_id": r["item_id"], "song_id": r["song_id"], "song": r["song"],
                    "first_upload": r["first_upload"], "label": labels.get(r["song_channel"], ""), "item": r["item_title"],
                    "last_read": last, "hours_since": round(hours, 1) if hours is not None else None,
                    "linked_at": r["first_seen_at"]})
    return out


def counts(conn, cfg=None):
    """{platform: missed items} and the number of items in scope per platform."""
    ensure(conn)
    s = settings(cfg)
    cut = (datetime.now(timezone.utc) - timedelta(hours=s["hours"])).isoformat(timespec="seconds")
    rows = conn.execute(f"""
        WITH i AS ({_items_sql()})
        SELECT i.platform, count(*) total,
               sum((i.last_read = '' AND coalesce(i.first_seen_at, '') < ?) OR (i.last_read <> '' AND i.last_read < ?)) missed
        FROM i JOIN track t ON t.id=i.song_id
        WHERE t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL GROUP BY 1""", (cut, cut, s["since"])).fetchall()
    return {r["platform"]: {"total": r["total"], "missed": r["missed"] or 0} for r in rows}


def last_runs(conn):
    """The latest run of each reading job: status, when, and its error (for the page)."""
    out = {}
    for plat, (_m, job) in PLATFORMS.items():
        r = conn.execute("SELECT status, started_at, finished_at, stats FROM run_log WHERE job=? ORDER BY id DESC LIMIT 1",
                         (job,)).fetchone()
        if not r:
            out[plat] = None
            continue
        try:
            st = json.loads(r["stats"] or "{}")
        except (TypeError, ValueError):
            st = {}
        out[plat] = {"job": job, "status": r["status"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                     "error": str(st.get("error") or st.get("stop_reason") or "")[:300]}
    return out


def watchdog_job(conn, cfg, tried, paused, browser=True, api=True):
    """(job, reason) for a platform with missed items whose job may run again now, else None. Browser jobs (Spotify,
    Instagram) only for the browser lane; Instagram keeps its own login-wall stop."""
    s = settings(cfg)
    if not s["watchdog"]:
        return None
    from .jobs import BROWSER_JOBS
    import time
    c = counts(conn, cfg)
    for plat, (_m, job) in PLATFORMS.items():
        n = (c.get(plat) or {}).get("missed", 0)
        if not n or pause.covers(paused, job):
            continue
        is_browser = job in BROWSER_JOBS
        if (is_browser and not browser) or (not is_browser and not api):
            continue
        last, strikes = _HIST.get(plat, (None, 0))
        if last is not None and n >= last:
            strikes += 1  # the last watchdog run did not read them: something else is wrong (shown on the page)
        elif last is not None:
            strikes = 0
        wait = 60 * (STUCK_MINUTES if strikes >= STUCK_AFTER else s["retry_minutes"])
        if time.time() - tried.get(job, 0) < wait:
            continue
        _HIST[plat] = (n, strikes)
        return job, f"{n} {NAMES[plat]} items not read in {s['hours']:g} h (watchdog)"
    return None


def stuck():
    """Platforms whose watchdog runs have not reduced the missed items (STUCK_AFTER runs in a row)."""
    return {p: n for p, (n, k) in _HIST.items() if k >= STUCK_AFTER}


def marked(conn, cfg=None):
    """Items read without a number in the last week because they could not be read: removed or private videos, Spotify
    pages gone, Deezer ISRCs gone, items marked unavailable on the page."""
    ensure(conn)
    s = settings(cfg)
    labels = config.channel_labels(cfg or config.load())
    rows = conn.execute("""
        SELECT m.platform_item_id, m.metric, m.read_at, m.outcome, p.platform, p.title, p.track_id, coalesce(t.yt_title, t.title) song,
               substr(t.first_published,1,10) first_upload,
               (SELECT channel FROM platform_item y WHERE y.track_id=t.id AND y.platform='youtube' ORDER BY published_at LIMIT 1) ch
        FROM read_mark m JOIN platform_item p ON p.id=m.platform_item_id JOIN track t ON t.id=p.track_id
        WHERE m.outcome IN ('gone', 'unavailable') AND m.read_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-7 days')
          AND t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL
          AND NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric=m.metric AND s.captured_at > m.read_at)
        ORDER BY m.read_at DESC LIMIT 1000""", (s["since"],)).fetchall()
    plat = {"views": "youtube", "plays": None, "rank": "deezer", "shorts": "shorts", "reels": "instagram"}
    return [{"item_id": r["platform_item_id"], "platform": plat.get(r["metric"]) or r["platform"], "metric": r["metric"],
             "outcome": r["outcome"], "at": r["read_at"], "item": r["title"], "song_id": r["track_id"], "song": r["song"],
             "first_upload": r["first_upload"], "label": labels.get(r["ch"], "")} for r in rows]


def mark_unavailable(conn, item_id, platform):
    """From the page: this item cannot be read (e.g. a removed video); not shown as missed for HOLD_DAYS."""
    if platform not in PLATFORMS:
        raise ValueError("unknown platform")
    mark(conn, int(item_id), PLATFORMS[platform][0], "unavailable")
    conn.commit()


def clear_mark(conn, item_id, metric):
    ensure(conn)
    conn.execute("DELETE FROM read_mark WHERE platform_item_id=? AND metric=?", (int(item_id), metric))
    conn.commit()
