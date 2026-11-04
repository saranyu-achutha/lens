"""Status page (Developer): for each source, what was read today and what is still waiting, quota used and left,
the backlog still to find, and what the two job lanes are doing now and will do next. All counts are for the
server's current day (UTC), the day the daily readings are stored under.
"""
import json
from datetime import date, datetime, timezone

from . import config


def _n(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()[0] or 0
    except Exception:
        return 0


def _runs_today(conn, jobs_, today):
    """Summed numbers of today's runs of these jobs, and the newest run."""
    tot, last = {}, None
    for rid, job, started, status, stats in conn.execute(
            f"SELECT id, job, started_at, status, stats FROM run_log WHERE job IN ({','.join('?' * len(jobs_))}) AND started_at >= ? ORDER BY id",
            (*jobs_, today)):
        s = json.loads(stats or "{}")
        for k, v in s.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tot[k] = tot.get(k, 0) + v
        last = {"id": rid, "job": job, "started_at": started, "status": status}
    if not last:
        r = conn.execute(f"SELECT id, job, started_at, status FROM run_log WHERE job IN ({','.join('?' * len(jobs_))}) ORDER BY id DESC LIMIT 1", jobs_).fetchone()
        last = dict(zip(("id", "job", "started_at", "status"), r)) if r else None
    return tot, last


def compute(conn, manager, browser, quota):
    """quota: the Console's YouTube quota status (units used today, limit, keys)."""
    from . import alerts, health, instagram_audio, jobs, shorts_use, songs, spotify_links, spotify_plays, youtube_music
    cfg = config.load()
    today = date.today().isoformat()
    since, until = config.window(cfg)
    titles = json.dumps([c["title"] for c in cfg["youtube"]["channels"]])
    read_today = lambda platform, metric: _n(conn, """SELECT count(DISTINCT s.platform_item_id) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                                                      WHERE s.capture_date=? AND s.metric=? AND p.platform=?""", (today, metric, platform))
    cards = []

    # YouTube
    yt_total = _n(conn, "SELECT count(*) FROM platform_item WHERE platform='youtube' AND published_at >= ? AND channel IN (SELECT value FROM json_each(?))", (since, titles))
    yt_read = _n(conn, """SELECT count(DISTINCT s.platform_item_id) FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                          WHERE s.capture_date=? AND s.metric='views' AND p.platform='youtube' AND p.published_at >= ? AND p.channel IN (SELECT value FROM json_each(?))""",
                 (today, since, titles))
    yt_runs, yt_last = _runs_today(conn, ("youtube_sync", "pipeline"), today)
    cards.append({"key": "youtube", "icon": "youtube", "name": "YouTube", "what": "views, likes, comments of the labels' uploads",
                  "read": yt_read, "total": yt_total, "unit": "videos",
                  "quota": {"used": quota["units_today"], "limit": quota["units_limit"], "unit": "units", "note": f"{quota['keys']} keys, shared with YouTube Music; resets {quota['resets']}"},
                  "extra": [("New uploads found today", yt_runs.get("new_videos", 0))], "last": yt_last})
    # YouTube Music
    ytm_total = _n(conn, "SELECT count(*) FROM platform_item p JOIN track t ON t.id=p.track_id WHERE p.platform='ytmusic' AND t.first_published >= ?", (since,))
    ytm_runs, ytm_last = _runs_today(conn, ("ytmusic", "pipeline"), today)
    ytm_todo = len(youtube_music._todo(conn, since, until, today, 100000))
    ytm_missing = _n(conn, """SELECT count(*) FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                              AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='ytmusic')""", (since, until))
    cards.append({"key": "ytmusic", "icon": "ytmusic", "name": "YouTube Music", "what": "plays of the songs' audio tracks",
                  "read": read_today("ytmusic", "plays"), "total": ytm_total, "unit": "tracks",
                  "backlog": {"label": "songs without a YouTube Music track", "count": ytm_missing, "due": ytm_todo, "due_label": "due a search now"},
                  "extra": [("Searches today", int(ytm_runs.get("ytmusic_found", 0) - ytm_runs.get("ytmusic_topic_found", 0) + ytm_runs.get("ytmusic_missing", 0))),
                            ("Found today (search / Topic channels)", f"{int(ytm_runs.get('ytmusic_found', 0) - ytm_runs.get('ytmusic_topic_found', 0))} / {int(ytm_runs.get('ytmusic_topic_found', 0))}"),
                            ("Quota used by YouTube Music today", f"{int(ytm_runs.get('ytmusic_quota_units', 0)):,} units")], "last": ytm_last})
    # Spotify: links (API search and free routes) and play counts (public pages)
    sp_runs, sp_last = _runs_today(conn, ("songs", "pipeline"), today)
    sp_total_ids = spotify_plays.export_ids(conn)
    sp_due = spotify_plays.due_ids(conn)
    last_check = health.last_check(conn) or {}
    unlinked = _n(conn, """SELECT count(*) FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                           AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')""", (since, until))
    linked_today = _n(conn, "SELECT count(DISTINCT track_id) FROM platform_item WHERE platform='spotify' AND first_seen_at >= ?", (today,))
    cards.append({"key": "spotify_api", "icon": "spotify", "name": "Spotify links", "what": "finding each song's Spotify ID (official search API and free routes)",
                  "backlog": {"label": "songs without a Spotify ID", "count": unlinked, "due": len(spotify_links.due(conn, since, until, today)), "due_label": "due a free-route try today"},
                  "api": {"ok": last_check.get("ok"), "message": last_check.get("message"), "retry_at": last_check.get("retry_at")},
                  "extra": [("Spotify API searches today", int(sp_runs.get("spotify_searches", 0))), ("Songs linked today", linked_today),
                            ("Catch-up", f"on until {cfg['songs'].get('spotify_catchup_until')}" if (cfg["songs"].get("spotify_catchup_until") or "") >= today else "off (normal order)")],
                  "last": sp_last})
    pl_runs, pl_last = _runs_today(conn, ("spotify_plays",), today)
    cards.append({"key": "spotify_plays", "icon": "spotify", "name": "Spotify plays", "what": "play counts from the public track pages (browser)",
                  "read": len(sp_total_ids) - len(sp_due), "total": len(sp_total_ids), "unit": "tracks", "last": pl_last})
    # JioSaavn
    js_total = _n(conn, "SELECT count(*) FROM platform_item p JOIN track t ON t.id=p.track_id WHERE p.platform='jiosaavn' AND t.first_published >= ?", (since,))
    js_runs, js_last = _runs_today(conn, ("jiosaavn",), today)
    js_unsearched = _n(conn, f"""SELECT count(*) FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                                 AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='jiosaavn')""", (since, until))
    js_hidden = _n(conn, "SELECT count(*) FROM state WHERE key LIKE 'js_hidden:%' AND value=?", (today,))
    cards.append({"key": "jiosaavn", "icon": "jiosaavn", "name": "JioSaavn", "what": "play counts (no quota)", "read": read_today("jiosaavn", "plays") + js_hidden,
                  "total": js_total, "unit": "tracks", "backlog": {"label": "songs without a JioSaavn track", "count": js_unsearched},
                  "extra": [("Count hidden by JioSaavn today (too few plays)", js_hidden)], "last": js_last})
    # Deezer
    dz_due = _n(conn, f"""SELECT count(*) FROM track t LEFT JOIN release r ON r.id=t.release_id
                          WHERE (t.first_published >= :since OR r.release_date >= :since) AND {songs.DEEZER_DUE}""", {"since": since, "today": today})
    dz_read = read_today("deezer", "rank")
    cards.append({"key": "deezer", "icon": "deezer", "name": "Deezer", "what": "rank by ISRC (no quota)", "read": dz_read, "total": dz_read + dz_due, "unit": "tracks", "last": sp_last})
    # Shorts and Instagram
    shorts_use.ensure(conn)
    sh_due = len(shorts_use.due(conn, cfg, today))
    sh_read = _n(conn, "SELECT count(*) FROM sound_use WHERE platform='youtube' AND day=?", (today,))
    sh_runs, sh_last = _runs_today(conn, ("shorts",), today)
    cards.append({"key": "shorts", "icon": "youtube", "name": "YouTube Shorts", "what": "Shorts made with the official sounds (public pages)",
                  "read": sh_read, "total": sh_read + sh_due, "unit": "sounds", "extra": [("Uploads without a sound page (rechecked weekly)", shorts_use.no_sound_count(conn, today)),
                            ("Pages not readable today", int(sh_runs.get("shorts_pages_failed", 0)))], "last": sh_last})
    ig_due = len(instagram_audio.due(conn, today))
    ig_read = _n(conn, "SELECT count(*) FROM sound_use WHERE platform='instagram' AND day=?", (today,))
    ig_runs, ig_last = _runs_today(conn, ("instagram",), today)
    cards.append({"key": "instagram", "icon": "instagram", "name": "Instagram Reels", "what": "Reels made with the official audio (public pages, browser)",
                  "read": ig_read, "total": ig_read + ig_due, "unit": "audios", "extra": [("Pages not shown today (login wall)", int(ig_runs.get("instagram_failed", 0)))], "last": ig_last})
    ch = alerts.charts_status(conn)
    cards.append({"key": "charts", "icon": "spotify", "name": "Spotify India chart", "what": "daily Top 200 (needs a session token)",
                  "extra": [("Newest daily chart", ch["daily_latest"] or "none"), ("Behind", "yes, paste a token" if ch["behind"] else "no")]})
    cl_runs, cl_last = _runs_today(conn, ("cleanup",), today)
    mb = lambda k: f"{cl_runs.get(k, 0) / 2**20:,.0f} MB"
    cards.append({"key": "cleanup", "icon": "disk", "name": "Daily cleanup", "what": "backup, old raw replies and CSVs compressed into archives (nothing deleted)",
                  "extra": [("Disk given back today", mb("cleanup_disk_freed")), ("Raw replies archived", f"{int(cl_runs.get('cleanup_raw_rows', 0)):,} rows"),
                            ("CSV exports compressed", mb("cleanup_outputs_saved")), ("Database shrunk by", mb("cleanup_db_shrunk"))],
                  "last": cl_last})
    # Lanes: running now and what the work loop would start next
    from . import pause
    paused = pause.get(conn)  # a paused job is not "next"
    nxt_api = jobs._due_work(cfg, {}, browser=False, paused=paused)
    nxt_browser = jobs._due_work(cfg, {}, api=False, paused=paused)
    lane = lambda m, nxt: dict({k: v for k, v in m.state(10**9).items() if k != "log"}, log=m.state(10**9).get("log", [])[-4:],
                                next={"job": nxt[0], "why": nxt[1]} if nxt else None)
    return {"day": today, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cards": cards,
            "lanes": {"api": lane(manager, nxt_api), "browser": lane(browser, nxt_browser)}}
