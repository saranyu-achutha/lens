"""Collection schedule (Developer page): how often each source is read, and what that costs per day in YouTube
quota and in machine time, so a change can be judged before it is saved.

Two settings can be changed here (both in config.json):
  youtube.fresh_every_hours     new videos (first `fresh_days` days): every 6, 12 or 24 hours; older videos once a day
  songs.jiosaavn_refresh_hours  JioSaavn play counts: every 6, 12 or 24 hours
Every other source is read once a day per item (daily numbers are kept for good; nothing is ever read less often).

Cost model (per item and read, from the readers' own pacing):
  YouTube        videos.list: 1 quota unit per 50 videos; uploads scan: about 1 unit per channel per hourly sync
  YouTube Music  plays read through YouTube's videos.list too: 1 unit per 50 tracks
  JioSaavn       50 tracks per request, ~1.5 s each
  Spotify plays  public track page in the headless browser, ~5 s per track with 3 tabs (browser lane)
  Shorts         one public sound page per official upload, ~3 s
  Instagram      one rendered audio page per linked audio, ~15 s (browser lane)
  Deezer         one call per ISRC, ~0.4 s
"""
import json
from datetime import datetime, timedelta, timezone

from . import config

CHOICES = {"youtube_fresh_every_hours": (6, 12, 24), "jiosaavn_refresh_hours": (6, 12, 24)}


def _n(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()[0] or 0
    except Exception:
        return 0


def estimate(conn, cfg=None, overrides=None):
    """Items, reads a day and daily cost per source for the current (or proposed) settings."""
    from .youtube import DAILY_UNITS, parse_keys
    cfg = cfg or config.load()
    o = overrides or {}
    yh = int(o.get("youtube_fresh_every_hours") or cfg["youtube"].get("fresh_every_hours", 24))
    jh = int(o.get("jiosaavn_refresh_hours") or cfg["songs"].get("jiosaavn_refresh_hours", 24))
    since, until = config.window(cfg)
    channels = [c for c in cfg["youtube"]["channels"]]
    fresh_from = (datetime.now(timezone.utc) - timedelta(days=cfg["youtube"].get("fresh_days", 7))).isoformat()
    titles = json.dumps([c["title"] for c in channels])
    yt_new = _n(conn, "SELECT count(*) FROM platform_item WHERE platform='youtube' AND published_at >= ? AND channel IN (SELECT value FROM json_each(?))", (fresh_from, titles))
    yt_all = _n(conn, "SELECT count(*) FROM platform_item WHERE platform='youtube' AND published_at >= ? AND channel IN (SELECT value FROM json_each(?))", (since, titles))
    ytm = _n(conn, "SELECT count(*) FROM platform_item p JOIN track t ON t.id=p.track_id WHERE p.platform='ytmusic' AND t.first_published >= ? AND t.old_release IS NULL", (since,))
    js = _n(conn, "SELECT count(*) FROM platform_item p JOIN track t ON t.id=p.track_id WHERE p.platform='jiosaavn' AND t.first_published >= ? AND t.old_release IS NULL", (since,))
    sp = _n(conn, "SELECT count(DISTINCT p.platform_id) FROM platform_item p JOIN track t ON t.id=p.track_id WHERE p.platform='spotify' AND p.review_state IN ('auto','approved') AND t.first_published >= ? AND t.old_release IS NULL", (since,))
    dz = _n(conn, "SELECT count(*) FROM track WHERE isrc IS NOT NULL AND first_published >= ? AND old_release IS NULL", (since,))
    from . import shorts_use
    shorts_use.ensure(conn)
    sh = len(shorts_use.due(conn, cfg, "0000-00-00"))  # every official sound (none counts as read on day 0)
    ig = _n(conn, "SELECT count(*) FROM platform_item WHERE platform='instagram' AND review_state IN ('auto','approved') AND track_id IS NOT NULL")

    yt_reads = yt_new * (24 // yh) + (yt_all - yt_new)
    scans = len(channels) * 24  # the hourly sync looks at each channel's newest uploads
    yt_units = -(-yt_reads // 50) + scans
    ytm_units = -(-ytm // 50)
    keys = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
    units_limit = DAILY_UNITS * max(len(keys), 1)
    search_units = cfg["songs"].get("ytmusic_searches_per_run", 200) * 100  # YouTube Music searches: 100 units each, while songs are left
    js_req = -(-js // 50) * (24 // jh)
    api_lane_s = js_req * 1.5 + sh * 3 + dz * 0.4
    browser_lane_s = sp * 5 + ig * 15
    sources = [
        {"key": "youtube", "name": "YouTube views, likes, comments", "items": yt_all,
         "rule": f"new videos (first {cfg['youtube'].get('fresh_days', 7)} days) every {yh} h, older once a day",
         "reads": yt_reads, "cost": f"{yt_units:,} quota units", "units": yt_units, "setting": "youtube_fresh_every_hours", "value": yh,
         "detail": f"{yt_new:,} new videos x {24 // yh} reads + {yt_all - yt_new:,} older x 1, at 50 videos a unit, + {scans:,} units scanning {len(channels)} channels hourly"},
        {"key": "ytmusic", "name": "YouTube Music plays", "items": ytm, "rule": "once a day", "reads": ytm, "cost": f"{ytm_units:,} quota units", "units": ytm_units},
        {"key": "jiosaavn", "name": "JioSaavn plays", "items": js, "rule": "once a day" if jh >= 24 else f"every {jh} h",
         "reads": js * (24 // jh), "cost": f"{js_req * 1.5 / 60:.0f} min", "setting": "jiosaavn_refresh_hours", "value": jh,
         "detail": f"{js:,} tracks, 50 per request, {24 // jh} round(s) a day; no quota"},
        {"key": "spotify", "name": "Spotify plays (public pages)", "items": sp, "rule": "once a day", "reads": sp, "cost": f"{sp * 5 / 3600:.1f} h browser"},
        {"key": "deezer", "name": "Deezer rank", "items": dz, "rule": "once a day", "reads": dz, "cost": f"{dz * 0.4 / 60:.0f} min"},
        {"key": "shorts", "name": "YouTube Shorts (official sounds)", "items": sh, "rule": "once a day", "reads": sh, "cost": f"{sh * 3 / 3600:.1f} h"},
        {"key": "instagram", "name": "Instagram Reels (linked audios)", "items": ig, "rule": "once a day", "reads": ig, "cost": f"{ig * 15 / 60:.0f} min browser"},
    ]
    # What actually ran in the last 24 hours (quota units recorded by the runs)
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    measured = 0
    for (st,) in conn.execute("SELECT stats FROM run_log WHERE started_at >= ?", (day_ago,)):
        s = json.loads(st or "{}")
        measured += int(s.get("quota_units") or 0) + int(s.get("ytmusic_quota_units") or 0)
    reads_units = yt_units + ytm_units
    verdict = lambda share: "comfortable" if share < 0.5 else "fine" if share < 0.8 else "tight" if share <= 1 else "over the limit"
    return {
        "settings": {"youtube_fresh_every_hours": yh, "jiosaavn_refresh_hours": jh}, "choices": CHOICES,
        "sources": sources,
        "youtube": {"keys": len(keys), "units_limit": units_limit, "reads_units": reads_units, "search_units": search_units,
                    "share": round(reads_units / units_limit, 3) if units_limit else None,
                    "share_with_search": round((reads_units + search_units) / units_limit, 3) if units_limit else None,
                    "measured_24h": measured, "verdict": verdict(reads_units / units_limit if units_limit else 9)},
        "time": {"api_lane_hours": round(api_lane_s / 3600, 1), "browser_lane_hours": round(browser_lane_s / 3600, 1),
                 "api_verdict": verdict(api_lane_s / 3600 / 20), "browser_verdict": verdict(browser_lane_s / 3600 / 20)},
    }


def save(body):
    """Store the chosen settings (only the allowed values)."""
    cfg = config.load()
    for k, allowed in CHOICES.items():
        if k in body:
            v = int(body[k])
            if v not in allowed:
                raise ValueError(f"{k} must be one of {allowed}")
            if k == "youtube_fresh_every_hours":
                cfg["youtube"]["fresh_every_hours"] = v
            else:
                cfg["songs"]["jiosaavn_refresh_hours"] = v
    return config.save(cfg)
