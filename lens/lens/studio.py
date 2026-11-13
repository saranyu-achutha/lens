"""Market Lens Console: web UI to configure, run and browse the pipeline.

Usage:
    python3 -m lens.studio [--host 127.0.0.1] [--port 3000]

Binding to a non-loopback host requires STUDIO_PASSWORD (env or .env); the UI then asks for a login (HTTP basic auth):
  client     any username with STUDIO_PASSWORD, or a named client login (lens/logins.py, e.g. timesnow): client pages, read-only
  developer  DEVELOPER_USERNAME + DEVELOPER_PASSWORD (.env): every page and action
"""
import argparse
import base64
import csv
import hmac
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import threading

from . import ai, alerts, collection, config, crawl_stats, db, exports, health, insights, jobs, logins, merge, metadata, pause, review, shield, song_daily, songs, spotify_charts, sysstats
from .discover_spotify import EXPORT_SQL
from . import spotify
from .spotify import Spotify, SpotifyError

STATIC = Path(__file__).resolve().parent / "static"
DOCS = Path(__file__).resolve().parent.parent / "docs"
REQUEST = threading.local()  # who is making the current request (for the decision log)
MANAGER = jobs.JobManager()
BROWSER = jobs.JobManager()  # second lane: the Spotify page job (browser) runs beside the API jobs
PASSWORD = None  # client login: any username with STUDIO_PASSWORD
DEV_LOGIN = (None, None)  # developer login: DEVELOPER_USERNAME + DEVELOPER_PASSWORD (.env)
# What the client login may read: its dashboard, platforms, reports, daily data and songs. Everything else
# (developer pages, raw data, review, runs, settings, docs) and every change (POST) is for the developer login only.
# Client logins read on screen only: no CSV downloads (user rule, 2026-11-12); every *.csv / download path is refused below too.
CLIENT_GET = {"/api/status", "/api/client", "/api/platform", "/api/report", "/api/labels/report", "/api/market_share", "/api/management", "/api/daily", "/api/shorts",
              "/api/song/compare", "/api/tracks", "/api/track", "/api/search", "/api/insights/song", "/api/meta/detail"}  # Cast & featuring is developer only (2026-11-13)

SORTS = {"song_date": "song_date", "release_date": "release_date", "title": "title", "artists": "artists",
         "yt_views": "yt_views", "yt_delta": "yt_delta", "yt_likes": "yt_likes", "yt_comments": "yt_comments",
         "deezer_rank": "deezer_rank", **{f"yt_views_{a}": f"yt_views_{a}" for a in
                                           ("video", "lyric", "audio", "short", "teaser", "other")}}


def q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def secret_status():
    s = config.read_secrets()
    def mask(v):
        return (v[:4] + "…" + v[-4:]) if v and len(v) > 10 else ("set" if v else "")
    out = {k: {"set": bool(s.get(k)), "hint": mask(s.get(k, "")) if not k.endswith("CLIENT_SECRET")
               else ("set" if s.get(k) else "")} for k in config.SECRET_KEYS}
    from .youtube import parse_keys
    out["YOUTUBE_API_KEY"]["count"] = len(parse_keys(s.get("YOUTUBE_API_KEY")))
    return out


def api_status(conn, _):
    cfg = config.load()
    if getattr(REQUEST, "role", "developer") == "client":  # no settings, keys or job details for the client login
        last_ok = q(conn, "SELECT finished_at FROM run_log WHERE status='ok' ORDER BY id DESC LIMIT 1")
        return {"role": "client", "today": date.today().isoformat(), "last_ok": last_ok[0]["finished_at"] if last_ok else None,
                "counts": {"last_capture": conn.execute("SELECT max(capture_date) FROM snapshot").fetchone()[0]},
                "config": {"youtube": {"labels": [{"name": l["name"], "enabled": l.get("enabled", True)} for l in cfg["youtube"]["labels"]]},
                           "discovery": {"since": cfg["discovery"]["since"], "until": cfg["discovery"].get("until")}},
                "meta_fields": metadata.FIELDS, "explore_fields": metadata.EXPLORE}
    nxt = jobs.next_scheduled(cfg)
    counts = q(conn, """SELECT (SELECT count(*) FROM release) releases, (SELECT count(*) FROM track) tracks,
                              (SELECT count(*) FROM artist) artists, (SELECT count(*) FROM snapshot) snapshots,
                              (SELECT max(capture_date) FROM snapshot) last_capture,
                              (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='pending') yt_pending""")[0]
    last_ok = q(conn, "SELECT finished_at FROM run_log WHERE status='ok' ORDER BY id DESC LIMIT 1")
    return {"role": "developer", "meta_fields": metadata.FIELDS, "explore_fields": metadata.EXPLORE, "config": cfg, "secrets": secret_status(), "job": MANAGER.state(10**9), "browser_job": BROWSER.state(10**9), "jobs": jobs.JOBS,
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None, "counts": counts,
            "last_ok": last_ok[0]["finished_at"] if last_ok else None, "today": date.today().isoformat()}


def tag_labels(rows, cfg=None):
    """Add music_label (the tracked label owning the song's YouTube channel) to song rows."""
    lab = config.channel_labels(cfg or config.load())
    for r in rows:
        r["music_label"] = lab.get(r.get("yt_channel"), config.FIRST_LABEL) if r.get("yt_channel") else ""
    return rows


# Focus: the client looks at songs released in the period (new releases). Older songs re-uploaded in the period
# stay collected but are hidden unless the request asks for focus=all. Status comes from Spotify's release date when
# linked, otherwise from the release-year check (lens/release_year.py).
FOCUS_SQL = " AND coalesce(release_status, '') != 'catalogue'"


def focus_on(qs):
    return (qs or {}).get("focus") != "all" and config.load().get("focus", {}).get("new_releases_only", True)


# songs released before the period start are stored only: never on the dashboards, reports or song lists (2026-11-06)
PERIOD_ONLY = " AND id NOT IN (SELECT id FROM track WHERE old_release IS NOT NULL)"


def focus(qs):
    return FOCUS_SQL if focus_on(qs) else ""


def api_overview(conn, qs):
    since, until = config.window(config.load())
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?{PERIOD_ONLY}{focus(qs)}"
    a = tracks_args() + [since, until]
    kpi = q(conn, f"""SELECT count(*) songs, sum(release_status='new') new, sum(release_status='catalogue') catalogue,
                             sum(release_status='not_on_spotify') not_on_spotify, sum(release_status='pending') spotify_pending, sum(release_status='unchecked') unchecked,
                             sum(yt_views) views, sum(yt_delta) views_delta, sum(yt_likes) likes, sum(yt_comments) comments,
                             sum(isrc IS NOT NULL) with_isrc, sum(deezer_rank IS NOT NULL) on_deezer,
                             sum(yt_views >= 1000000) hits, sum(yt_delta > 0) growing
                      FROM ({W})""", a)[0]
    kpi.update(q(conn, """SELECT (SELECT max(capture_date) FROM snapshot) as_of,
                                 (SELECT count(*) FROM platform_item WHERE platform IN ('youtube','spotify')
                                    AND review_state='pending') review""")[0])
    kpi["since"], kpi["until"] = since, until
    kpi["on_platform"] = {r["platform"]: r["songs"] for r in q(conn, f"""
        SELECT pi.platform, count(DISTINCT pi.track_id) songs FROM platform_item pi
        WHERE pi.track_id IN (SELECT id FROM ({W})) AND pi.review_state IN ('auto','approved','song')
          AND pi.platform IN ('youtube','ytmusic','spotify','jiosaavn','apple','deezer') GROUP BY pi.platform""", a)}
    kpi.update(q(conn, f"""SELECT """ + ", ".join(f"sum(yt_views_{t}) views_{t}" for t in
                                                  ("video", "lyric", "audio", "short", "teaser", "other")) + f" FROM ({W})", a)[0])
    by_day = q(conn, f"""SELECT song_date day, count(*) songs, sum(release_status='new') new FROM ({W})
                         GROUP BY song_date ORDER BY song_date""", a)
    channels = q(conn, f"""SELECT coalesce(yt_channel, '(Spotify only)') channel, count(*) songs,
                                  sum(release_status='new') new, sum(yt_views) views
                           FROM ({W}) GROUP BY 1 ORDER BY songs DESC""", a)
    lab = config.channel_labels(config.load())
    labels = {}
    for c in channels:
        c["label"] = lab.get(c["channel"], "" if c["channel"].startswith("(") else config.FIRST_LABEL)
        t = labels.setdefault(c["label"] or "(Spotify only)", {"label": c["label"] or "(Spotify only)", "songs": 0, "new": 0, "views": 0})
        for k in ("songs", "new", "views"):
            t[k] += c[k] or 0
    top = q(conn, W + " AND yt_views IS NOT NULL ORDER BY yt_views DESC LIMIT 10", a)
    risers = q(conn, W + " AND yt_delta IS NOT NULL ORDER BY yt_delta DESC LIMIT 10", a)
    return {"kpi": kpi, "by_day": by_day, "channels": channels, "top": tag_labels(top), "risers": tag_labels(risers),
            "labels": sorted(labels.values(), key=lambda t: -t["songs"])}


def api_platform(conn, qs):
    """Dashboard of one platform (youtube / spotify / apple / deezer) for the song period:
    {kpis: [[label, value, sub]], chart: {title, bars: [{label, value}]}, table: {title, cols, rows}}."""
    p = (qs.get("p") or "youtube").lower()
    since, until = config.window(config.load())
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?{PERIOD_ONLY}{focus(qs)}"
    a = tracks_args() + [since, until]
    songs = q(conn, f"SELECT count(*) n FROM ({W})", a)[0]["n"] or 0
    pct = lambda n: f"{round(100 * (n or 0) / songs) if songs else 0}% of {songs:,} songs"
    out = {"platform": p, "since": since, "until": until, "songs": songs}
    if p == "all":  # cross-platform: daily totals and gains per platform over the period's songs (song_daily)
        song_daily.ensure(conn)
        ids = f"SELECT id FROM ({W})"
        plats = [("youtube", "YouTube", "yt_views"), ("ytmusic", "YouTube Music", "ytm_plays"), ("spotify", "Spotify", "sp_plays"), ("jiosaavn", "JioSaavn", "js_plays")]
        daily = q(conn, f"""SELECT day, {', '.join(f"sum({c}) {c}, sum({c}_gain) {c}_gain, count({c}) {c}_n" for _, _, c in plats)}
                            FROM song_daily WHERE song_id IN ({ids}) GROUP BY day ORDER BY day""", a)
        rows, tiles = [], []
        allp = 0
        for plat, label, c in plats:
            last = next((d for d in reversed(daily) if d[c] is not None), None)
            week = [d[c + "_gain"] for d in daily[-7:] if d[c + "_gain"] is not None]
            top = q(conn, f"""SELECT t.title, d.{c} v FROM song_daily d JOIN track t ON t.id=d.song_id
                             WHERE d.day=? AND d.song_id IN ({ids}) AND d.{c} IS NOT NULL ORDER BY d.{c} DESC LIMIT 1""",
                    [last["day"] if last else ""] + a)
            r = {"platform": plat, "label": label, "songs": last[c + "_n"] if last else 0, "total": last[c] if last else None,
                 "day": last["day"] if last else None, "gain_1d": daily[-1][c + "_gain"] if daily else None,
                 "gain_7d": sum(week) if week else None, "top": top[0]["title"] if top else None, "top_value": top[0]["v"] if top else None}
            allp += r["total"] or 0
            rows.append(r)
        for r in rows:
            r["share"] = round(100 * r["total"] / allp, 1) if allp and r["total"] else None
            tiles.append([r["label"], r["total"], f"{'+' + format(int(r['gain_1d']), ',') if r['gain_1d'] else 'no gain yet'} last day · {r['songs']:,} songs"])
        out["kpis"] = [["All platforms", allp, "views + plays, latest reading of each"]] + tiles
        out["lines"] = [{"platform": plat, "name": label, "points": [{"x": d["day"], "y": d[c + "_gain"]} for d in daily if d[c + "_gain"] is not None]}
                        for plat, label, c in plats]
        out["chart"] = {"title": "Daily gain by platform", "bars": []}
        out["table"] = {"title": "Platforms compared", "cols": ["Platform", "Songs", "Total", "Gain last day", "Gain 7 days", "Share", "Top song"],
                        "rows": [[None, r["label"], r["songs"], r["total"], r["gain_1d"], r["gain_7d"], f"{r['share']}%" if r["share"] is not None else "–",
                                  f"{r['top']} ({r['top_value']:,.0f})" if r["top"] else "–"] for r in rows]}
    elif p == "youtube":
        k = q(conn, f"""SELECT sum(yt_videos > 0) songs, sum(yt_videos) uploads, sum(yt_views) views, sum(yt_likes) likes,
                               sum(yt_comments) comments, sum(yt_delta) delta, sum(yt_views >= 1000000) hits FROM ({W})""", a)[0]
        allup = q(conn, "SELECT count(*) n FROM platform_item WHERE platform='youtube' AND published_at >= ?", (since,))[0]["n"]
        out["kpis"] = [["Songs on YouTube", k["songs"], pct(k["songs"])], ["Uploads", allup, f"{k['uploads'] or 0:,} linked to songs"],
                       ["Views", k["views"], f"{int(k['likes'] or 0):,} likes · {int(k['comments'] or 0):,} comments"],
                       ["Views in the last day", k["delta"], "all linked uploads"], ["Songs with 1M+ views", k["hits"], pct(k["hits"])]]
        t = q(conn, f"SELECT " + ", ".join(f"sum(yt_views_{x}) {x}" for x in ("video", "lyric", "audio", "short", "teaser", "other")) + f" FROM ({W})", a)[0]
        out["chart"] = {"title": "Views by upload type", "bars": [{"label": x, "value": t[x] or 0} for x in t]}
        rows = q(conn, W + " AND yt_views IS NOT NULL ORDER BY yt_views DESC LIMIT 10", a)
        out["table"] = {"title": "Top 10 songs by YouTube views", "cols": ["Song", "Channel", "First upload", "Uploads", "Views", "Δ day"],
                        "rows": [[r["id"], r["title"], r["yt_channel"], r["song_date"], r["yt_videos"], r["yt_views"], r["yt_delta"]] for r in rows]}
    elif p == "spotify":
        k = q(conn, f"""SELECT sum(release_status='new') new, sum(release_status='catalogue') cat, sum(release_status='not_on_spotify') nf,
                               sum(release_status='pending') rev, sum(release_status='unchecked') todo FROM ({W})""", a)[0]
        linked = (k["new"] or 0) + (k["cat"] or 0)
        spotify_charts.ensure(conn)
        last = q(conn, "SELECT max(chart_date) d FROM chart_entry WHERE chart='regional-in-daily'")[0]["d"]
        on = q(conn, f"""SELECT ce.rank, ce.appearances, ce.track_name, t.id, t.title, t.yt_views, t.song_date FROM chart_entry ce
                         JOIN platform_item pi ON pi.platform='spotify' AND pi.platform_id=ce.spotify_id AND pi.review_state IN ('auto','approved')
                         JOIN ({TRACKS_SQL}) t ON t.id=pi.track_id
                         WHERE ce.chart='regional-in-daily' AND ce.chart_date=? ORDER BY ce.rank""", tracks_args() + [last]) if last else []
        # Daily play counts read from the public track pages (lens/spotify_plays.py). Spotify shows one recording's count on all
# its copies (single, album, compilations), so a song's count is the highest of its copies, not their sum.
        pl = q(conn, f"""WITH sp AS (
              SELECT pi.track_id,
                     (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1) now,
                     (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1 OFFSET 1) prev,
                     (SELECT max(capture_date) FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays') day
              FROM platform_item pi WHERE pi.platform='spotify' AND pi.review_state IN ('auto','approved') AND pi.track_id IS NOT NULL)
            SELECT t.id, t.title, t.song_date, t.yt_views, max(sp.now) plays, max(sp.now) - max(sp.prev) delta, max(sp.day) day
            FROM ({W}) t JOIN sp ON sp.track_id=t.id WHERE sp.now IS NOT NULL GROUP BY t.id ORDER BY plays DESC""", a)
        rank_today = {r["id"]: r["rank"] for r in reversed(on)}
        out["kpis"] = [["Spotify plays", sum(r["plays"] or 0 for r in pl), f"{len(pl):,} songs with a public count" + (f" · read {max(r['day'] for r in pl)}" if pl else "")],
                       ["Plays in the last day", sum(r["delta"] or 0 for r in pl) if any(r["delta"] is not None for r in pl) else None, "after two daily readings"],
                       ["Songs linked to Spotify", linked, pct(linked)], ["New releases", k["new"], f"{k['cat'] or 0:,} older releases (on Spotify 30+ days before)"],
                       ["Not found on Spotify", k["nf"], f"{k['todo'] or 0:,} still to look up · {k['rev'] or 0} in review"],
                       ["On India Top 200 today", len({r["id"] for r in on}), f"daily chart of {last}" if last else "no chart data"]]
        days = q(conn, """SELECT ce.chart_date d, count(DISTINCT pi.track_id) n FROM chart_entry ce
                          JOIN platform_item pi ON pi.platform='spotify' AND pi.platform_id=ce.spotify_id AND pi.review_state IN ('auto','approved')
                          WHERE ce.chart='regional-in-daily' AND ce.chart_date >= date(?, '-59 day') GROUP BY 1 ORDER BY 1""", (last or until,))
        out["chart"] = {"title": "Our songs in the Spotify India Top 200, by day", "bars": [{"label": r["d"][5:], "value": r["n"]} for r in days]}
        if pl:
            out["table"] = {"title": "Top songs by Spotify plays", "cols": ["Song", "Spotify plays", "Δ day", "India Top 200 today", "YouTube views"],
                            "rows": [[r["id"], r["title"], r["plays"], r["delta"], rank_today.get(r["id"]), r["yt_views"]] for r in pl[:25]]}
        else:
            out["table"] = {"title": f"Our songs on the Spotify India Top 200 ({last or '–'})", "cols": ["Song", "Rank", "Days on chart (all-time)", "First upload", "YouTube views"],
                            "rows": [[r["id"], r["title"], r["rank"], r["appearances"], r["song_date"], r["yt_views"]] for r in on[:25]]}
    elif p == "ytmusic":
        rows = q(conn, f"""WITH ym AS (
              SELECT pi.track_id, pi.channel,
                     (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1) now,
                     (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1 OFFSET 1) prev
              FROM platform_item pi WHERE pi.platform='ytmusic' AND pi.track_id IS NOT NULL)
            SELECT t.id, t.title, t.yt_channel, t.yt_views, max(ym.channel) topic, sum(ym.now) plays, sum(ym.now - ym.prev) delta
            FROM ({W}) t JOIN ym ON ym.track_id=t.id GROUP BY t.id ORDER BY plays DESC""", a)
        looked = q(conn, "SELECT count(*) n FROM state WHERE key LIKE 'ytm_lookup:%'")[0]["n"]
        lab = config.channel_labels(config.load())
        by = {}
        for r in rows:
            by[lab.get(r["yt_channel"], "Other")] = by.get(lab.get(r["yt_channel"], "Other"), 0) + (r["plays"] or 0)
        out["kpis"] = [["Songs with a YouTube Music track", len(rows), pct(len(rows))],
                       ["YouTube Music plays", sum(r["plays"] or 0 for r in rows), "separate audio tracks only; plays of a label's video are in YouTube views"],
                       ["Plays in the last day", sum(r["delta"] or 0 for r in rows) if any(r["delta"] is not None for r in rows) else None, "after two daily readings"],
                       ["Songs searched", looked, f"{max(songs - looked, 0):,} still to search (40 a run)"]]
        out["chart"] = {"title": "YouTube Music plays by label", "bars": [{"label": k, "value": v} for k, v in sorted(by.items(), key=lambda x: -x[1])]}
        out["table"] = {"title": "Top songs by YouTube Music plays", "cols": ["Song", "Topic channel", "YT Music plays", "Δ day", "YouTube views"],
                        "rows": [[r["id"], r["title"], r["topic"], r["plays"], r["delta"], r["yt_views"]] for r in rows[:25]]}
    elif p == "jiosaavn":
        rows = q(conn, f"""WITH js AS (
              SELECT pi.track_id, pi.title js_title,
                     (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1) now,
                     (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1 OFFSET 1) prev
              FROM platform_item pi WHERE pi.platform='jiosaavn' AND pi.track_id IS NOT NULL)
            SELECT t.id, t.title, t.yt_channel, t.yt_views, max(js.js_title) js_title, sum(js.now) plays, sum(js.now - js.prev) delta
            FROM ({W}) t JOIN js ON js.track_id=t.id GROUP BY t.id ORDER BY plays DESC""", a)
        looked = q(conn, "SELECT count(*) n FROM state WHERE key LIKE 'js_lookup:%'")[0]["n"]
        lab = config.channel_labels(config.load())
        by = {}
        for r in rows:
            by[lab.get(r["yt_channel"], "Other")] = by.get(lab.get(r["yt_channel"], "Other"), 0) + (r["plays"] or 0)
        out["kpis"] = [["Songs on JioSaavn", len(rows), pct(len(rows))], ["JioSaavn plays", sum(r["plays"] or 0 for r in rows), "public play counts, read daily"],
                       ["Plays in the last day", sum(r["delta"] or 0 for r in rows) if any(r["delta"] is not None for r in rows) else None, "after two daily readings"],
                       ["Songs searched", looked, f"{max(songs - looked, 0):,} still to search (400 a run)"]]
        out["chart"] = {"title": "JioSaavn plays by label", "bars": [{"label": k, "value": v} for k, v in sorted(by.items(), key=lambda x: -x[1])]}
        out["table"] = {"title": "Top songs by JioSaavn plays", "cols": ["Song", "JioSaavn title", "JioSaavn plays", "Δ day", "YouTube views"],
                        "rows": [[r["id"], r["title"], r["js_title"], r["plays"], r["delta"], r["yt_views"]] for r in rows[:25]]}
    elif p == "apple":
        k = q(conn, f"""SELECT count(DISTINCT t.id) n, sum(t.release_status='not_on_spotify') gap FROM ({W}) t
                        WHERE EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='apple')""", a)[0]
        out["kpis"] = [["Songs on Apple Music", k["n"], pct(k["n"])], ["Found only on Apple", k["gap"], "not found on Spotify"],
                       ["Lookups", "≈20 / min", "iTunes Search API, India store"]]
        g = q(conn, f"""SELECT m.value label, count(*) value FROM song_meta m WHERE m.field='genre' AND m.source LIKE '%apple%'
                        AND m.track_id IN (SELECT id FROM ({W})) GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", a)
        out["chart"] = {"title": "Apple Music genres", "bars": g}
        rows = q(conn, f"""SELECT t.id, t.title, p.title apple_title, p.published_at rel, p.url, t.yt_views FROM ({W}) t
                           JOIN platform_item p ON p.track_id=t.id AND p.platform='apple' ORDER BY t.yt_views DESC LIMIT 10""", a)
        out["table"] = {"title": "Top 10 Apple-linked songs (by YouTube views)", "cols": ["Song", "Apple Music title", "Apple release", "YouTube views"],
                        "rows": [[r["id"], r["title"], r["apple_title"], (r["rel"] or "")[:10], r["yt_views"]] for r in rows]}
    elif p == "deezer":
        k = q(conn, f"SELECT count(deezer_rank) n, avg(deezer_rank) avg, max(deezer_rank) top FROM ({W})", a)[0]
        out["kpis"] = [["Songs with a Deezer rank", k["n"], pct(k["n"])], ["Average rank", round(k["avg"] or 0), "Deezer popularity score (higher = more popular)"],
                       ["Highest rank", k["top"], "read daily by ISRC"]]
        b = q(conn, f"""SELECT CASE WHEN deezer_rank >= 300000 THEN '300K+' WHEN deezer_rank >= 200000 THEN '200–300K'
                                    WHEN deezer_rank >= 100000 THEN '100–200K' WHEN deezer_rank >= 50000 THEN '50–100K' ELSE 'under 50K' END label,
                               count(*) value, min(deezer_rank) lo FROM ({W}) WHERE deezer_rank IS NOT NULL GROUP BY 1 ORDER BY lo""", a)
        out["chart"] = {"title": "Songs by Deezer rank", "bars": [{"label": r["label"], "value": r["value"]} for r in b]}
        rows = q(conn, W + " AND deezer_rank IS NOT NULL ORDER BY deezer_rank DESC LIMIT 10", a)
        out["table"] = {"title": "Top 10 songs by Deezer rank", "cols": ["Song", "Deezer rank", "First upload", "YouTube views"],
                        "rows": [[r["id"], r["title"], r["deezer_rank"], r["song_date"], r["yt_views"]] for r in rows]}
    else:
        raise ValueError("unknown platform")
    return out


# Instagram is hidden from every client login (user rule, 2026-11-12): client JSON loses every Instagram key, list entry
# and platform row; client CSV downloads lose Instagram columns. Developers see everything.
_IG = re.compile(r"instagram|reels|(^|_)ig(_|$)", re.I)
_DROP = object()


def no_instagram(x):
    if isinstance(x, dict):
        if any(isinstance(x.get(k), str) and _IG.search(x[k]) for k in ("platform", "key", "metric", "source", "kind")):
            return _DROP
        out = {}
        for k, v in x.items():
            if _IG.search(str(k)):
                continue
            v = no_instagram(v)
            if v is not _DROP:
                out[k] = v
        return out
    if isinstance(x, (list, tuple)):
        if x and isinstance(x[0], str) and _IG.search(x[0]) and len(x) <= 4:  # a ("ig_reels", "Instagram Reels") pair
            return _DROP
        return [v for v in (no_instagram(v) for v in x)
                if v is not _DROP and not (isinstance(v, str) and re.fullmatch(r"instagram|ig_reels|reels", v, re.I))]
    return x


def no_instagram_csv(data):
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return data
    keep = [i for i, h in enumerate(rows[0]) if not _IG.search(h)]
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([r[i] for i in keep if i < len(r)])
    return ("\ufeff" if isinstance(data, bytes) and data.startswith(b"\xef\xbb\xbf") else "").encode() + buf.getvalue().encode()


# Daily data page: song_daily rows, by day (all songs) or by song (all days), with the change from the previous day.
DAILY_COLS = [("yt_views", "YouTube views"), ("yt_likes", "YouTube likes"), ("yt_comments", "YouTube comments"),
              ("ytm_plays", "YouTube Music plays"), ("sp_plays", "Spotify plays"), ("js_plays", "JioSaavn plays"), ("dz_rank", "Deezer rank"),
              ("chart_rank", "Spotify India chart"), ("yt_shorts", "YouTube Shorts made (official audio)"),
              ("ig_reels", "Instagram Reels made (official audio)")]


GAIN = {"yt_views", "yt_likes", "yt_comments", "ytm_plays", "sp_plays", "js_plays", "yt_shorts", "ig_reels"}


def daily_change(r, k):
    """Change since the previous day: the stored gain for counts, the difference for ranks."""
    if k in GAIN:
        return r.get(k + "_gain")
    return (r[k] - r["prev_" + k]) if r.get(k) is not None and r.get("prev_" + k) is not None else None


def daily_rows(conn, qs):
    song_daily.ensure(conn)
    days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM song_daily ORDER BY 1")]
    cols = ", ".join(f"d.{k}, p.{k} prev_{k}" for k, _ in DAILY_COLS) + ", " + ", ".join(f"d.{k}_gain" for k in sorted(GAIN))
    if qs.get("song"):  # one song, every day
        rows = q(conn, f"""SELECT d.day, {cols}, d.yt_uploads_read FROM song_daily d
                           LEFT JOIN song_daily p ON p.song_id=d.song_id AND p.day=(SELECT max(day) FROM song_daily x WHERE x.song_id=d.song_id AND x.day < d.day)
                           WHERE d.song_id=? ORDER BY d.day DESC""", (int(qs["song"]),))
        t = tag_labels(q(conn, """SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) first_upload, t.release_status,
                    (SELECT channel FROM platform_item x WHERE x.track_id=t.id AND x.platform='youtube' ORDER BY published_at LIMIT 1) yt_channel,
                    (SELECT platform_id FROM platform_item x WHERE x.track_id=t.id AND x.platform='youtube'
                       AND x.review_state IN ('auto','approved','song') ORDER BY published_at LIMIT 1) yt_video_id,
                    (SELECT platform_id FROM platform_item x WHERE x.track_id=t.id AND x.platform='spotify'
                       AND x.review_state IN ('auto','approved') ORDER BY id LIMIT 1) spotify_id
                  FROM track t WHERE t.id=?""", (int(qs["song"]),)))
        if t:  # one link per platform, for the "Open on" buttons
            t[0]["links"] = {p: u for p, u in conn.execute(
                """SELECT platform, url FROM platform_item WHERE id IN (SELECT min(id) FROM platform_item WHERE track_id=? AND url IS NOT NULL
                   AND platform != 'youtube' AND review_state IN ('auto','approved','song') GROUP BY platform)""", (int(qs["song"]),))}
            if t[0]["yt_video_id"]:
                t[0]["links"]["youtube"] = f"https://www.youtube.com/watch?v={t[0]['yt_video_id']}"
        return {"mode": "song", "song": t[0] if t else None, "days": days, "rows": rows}
    day = qs.get("day") or (days[-1] if days else None)
    prev = max((d for d in days if day and d < day), default=None)
    rows = tag_labels(q(conn, f"""SELECT d.song_id id, t.title, substr(t.first_published,1,10) song_date, d.yt_uploads_read, {cols},
                                    (SELECT channel FROM platform_item x WHERE x.track_id=t.id AND x.platform='youtube' ORDER BY published_at LIMIT 1) yt_channel
                                  FROM song_daily d JOIN track t ON t.id=d.song_id
                                  LEFT JOIN song_daily p ON p.song_id=d.song_id AND p.day=?
                                  WHERE d.day=?{" AND coalesce(t.release_status, '') != 'catalogue'" if focus_on(qs) else ""}""", (prev, day)))
    lab, txt = qs.get("label") or "", (qs.get("q") or "").lower()
    rows = [r for r in rows if (not lab or r["music_label"] == lab) and (not txt or txt in (r["title"] or "").lower())]
    sort = qs.get("sort") or "yt_views"
    key = (lambda r: daily_change(r, sort[:-6]) if daily_change(r, sort[:-6]) is not None else float("-inf")) \
        if sort.endswith("_delta") else (lambda r: r.get(sort) if r.get(sort) is not None else float("-inf"))
    rows.sort(key=key, reverse=True)
    return {"mode": "day", "day": day, "prev": prev, "days": days, "rows": rows}


def api_daily(conn, qs):
    d = daily_rows(conn, qs)
    d["cols"] = [{"key": k, "label": l} for k, l in DAILY_COLS]
    if d["mode"] == "day":
        d["total"] = len(d["rows"])
        off, lim = int(qs.get("offset") or 0), int(qs.get("limit") or 100)
        d["rows"] = d["rows"][off:off + lim]
        # 8-day trend of YouTube views gained, for a small sparkline per song
        ids = [r["id"] for r in d["rows"]]
        if ids and d["day"]:
            spark = {}
            for sid, day, g in conn.execute(f"""SELECT song_id, day, yt_views_gain FROM song_daily WHERE song_id IN ({",".join("?" * len(ids))})
                                                AND day <= ? AND day > date(?, '-8 day') ORDER BY day""", (*ids, d["day"], d["day"])):
                spark.setdefault(sid, []).append(g)
            for r in d["rows"]:
                r["spark"] = spark.get(r["id"], [])
    return d


def daily_csv(conn, qs):
    d = daily_rows(conn, qs)
    buf = io.StringIO()
    w = exports.IdFreeWriter(buf)  # downloads carry no IDs or links
    metrics = [k for k, _ in DAILY_COLS]
    if d["mode"] == "song":
        w.writerow(["day"] + [x for k in metrics for x in (k, k + "_change")] + ["yt_uploads_read"])
        for r in d["rows"]:
            w.writerow([r["day"]] + [x for k in metrics for x in (r[k], daily_change(r, k))] + [r["yt_uploads_read"]])
        title = q(conn, "SELECT coalesce(yt_title, title) t FROM track WHERE id=?", (int(qs["song"]),))
        name = f"market_lens_song_{exports.slug(title[0]['t'] if title else '')}_daily.csv"
    else:
        w.writerow(["song_id", "song", "label", "channel", "first_upload", "day"] + [x for k in metrics for x in (k, k + "_change")] + ["yt_uploads_read"])
        for r in d["rows"]:
            w.writerow([r["id"], r["title"], r["music_label"], r["yt_channel"], r["song_date"], d["day"]] +
                       [x for k in metrics for x in (r[k], daily_change(r, k))] + [r["yt_uploads_read"]])
        name = f"market_lens_daily_{d['day']}.csv"
    return buf.getvalue(), name


# Day-by-day report: each song's value on two dates and the change, per platform metric.
REPORT_METRICS = [  # key, label, platform, stored metric, how a song's items combine
    ("yt_views", "YouTube views", "youtube", "views", "sum"), ("yt_likes", "YouTube likes", "youtube", "likes", "sum"),
    ("yt_comments", "YouTube comments", "youtube", "comments", "sum"), ("ytm_plays", "YouTube Music plays", "ytmusic", "plays", "sum"),
    ("sp_plays", "Spotify plays", "spotify", "plays", "max"), ("js_plays", "JioSaavn plays", "jiosaavn", "plays", "sum"),
    ("dz_rank", "Deezer rank", "deezer", "rank", "max")]


def report_data(conn, qs):
    since, until = config.window(config.load())
    days = [r["d"] for r in q(conn, "SELECT DISTINCT capture_date d FROM snapshot ORDER BY 1")]
    if not days:
        return {"days": [], "rows": [], "totals": {}, "metrics": [], "capture": []}
    to = qs.get("to") or days[-1]
    frm = qs.get("from") or (days[-2] if len(days) > 1 else days[-1])
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?{PERIOD_ONLY}{focus(qs)}"
    songs = {r["id"]: r for r in tag_labels(q(conn, f"SELECT id, title, yt_channel, song_date FROM ({W})", tracks_args() + [since, until]))}
    items = q(conn, """SELECT id, track_id, platform FROM platform_item WHERE track_id IS NOT NULL
                       AND ((platform='youtube' AND review_state IN ('auto','approved','song')) OR (platform='spotify' AND review_state IN ('auto','approved'))
                            OR platform IN ('ytmusic','deezer','jiosaavn'))""")
    items = [i for i in items if i["track_id"] in songs]
    ids = {i["id"]: i for i in items}

    def at(day):  # every item's reading taken ON `day` (an older reading would hide a gap and inflate the change)
        out = {}
        for r in conn.execute("SELECT platform_item_id, metric, value FROM snapshot WHERE capture_date = ?", (day,)):
            if r[0] in ids:
                out[(r[0], r[1])] = r[2]
        return out

    A, B = at(frm), at(to)
    per = {}
    for (iid, metric) in set(A) | set(B):
        it = ids[iid]
        for key, _l, plat, m, how in REPORT_METRICS:
            if plat == it["platform"] and m == metric:
                row = per.setdefault(it["track_id"], {})
                a, b = A.get((iid, metric)), B.get((iid, metric))
                # Only uploads read on both dates count, so from + change = to (a new upload's first reading is not growth).
                if a is None or b is None:
                    continue
                if how == "sum":
                    for side, v in (("from", a), ("to", b), ("delta", b - a)):
                        row[f"{key}_{side}"] = (row.get(f"{key}_{side}") or 0) + v
                else:  # copies sharing one count (Spotify) or a rank: the highest, and the change of that
                    row[f"{key}_from"] = max(row.get(f"{key}_from") or a, a)
                    row[f"{key}_to"] = max(row.get(f"{key}_to") or b, b)
                    row[f"{key}_delta"] = row[f"{key}_to"] - row[f"{key}_from"]
    rows = []
    for tid, v in per.items():
        sng = songs[tid]
        r = {"id": tid, "title": sng["title"], "label": sng["music_label"], "channel": sng["yt_channel"], "song_date": sng["song_date"]}
        for key, *_ in REPORT_METRICS:
            for side in ("from", "to", "delta"):
                r[f"{key}_{side}"] = v.get(f"{key}_{side}")
        rows.append(r)
    lab, txt = qs.get("label") or "", (qs.get("q") or "").lower()
    rows = [r for r in rows if (not lab or r["label"] == lab) and (not txt or txt in (r["title"] or "").lower())]
    sort = qs.get("sort") or "yt_views_delta"
    rows.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0)))
    totals = {f"{k}_{side}": sum(r[f"{k}_{side}"] or 0 for r in rows) for k, *_ in REPORT_METRICS if k != "dz_rank" for side in ("from", "to", "delta")}
    # Capture check: share of a platform's items with a reading on the `to` date.
    capture = []
    for plat, metric in (("youtube", "views"), ("ytmusic", "plays"), ("spotify", "plays"), ("jiosaavn", "plays"), ("deezer", "rank")):
        want = [i for i in items if i["platform"] == plat]
        got = conn.execute(f"""SELECT count(DISTINCT platform_item_id) FROM snapshot WHERE capture_date=? AND metric=?
                               AND platform_item_id IN ({",".join(str(i["id"]) for i in want) or "0"})""", (to, metric)).fetchone()[0]
        capture.append({"platform": plat, "items": len(want), "read": got})
    return {"from": frm, "to": to, "days": days, "sort": sort, "total": len(rows), "totals": totals, "capture": capture,
            "metrics": [{"key": k, "label": l} for k, l, *_ in REPORT_METRICS], "rows": rows}


def api_report(conn, qs):
    d = report_data(conn, qs)
    off, lim = int(qs.get("offset") or 0), int(qs.get("limit") or 100)
    d["rows"] = d["rows"][off:off + lim]
    return d


def report_csv(conn, qs):
    d = report_data(conn, qs)
    buf = io.StringIO()
    w = exports.IdFreeWriter(buf)  # downloads carry no IDs or links
    head = ["song_id", "song", "label", "channel", "first_upload"]
    for m in d["metrics"]:
        head += [f"{m['label']} {d['from']}", f"{m['label']} {d['to']}", f"{m['label']} change"]
    w.writerow(head)
    for r in d["rows"]:
        w.writerow([r["id"], r["title"], r["label"], r["channel"], r["song_date"]] +
                   [r[f"{m['key']}_{s}"] for m in d["metrics"] for s in ("from", "to", "delta")])
    return buf.getvalue(), d["from"], d["to"]


# Per-track rollups of the latest (and previous-day) readings of linked videos, plus Deezer rank.
YT_CTE = """
yt_item AS (
  SELECT pi.track_id, pi.review_state, pi.channel, pi.asset_type,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='views' ORDER BY capture_date DESC LIMIT 1) v_now,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='views' ORDER BY capture_date DESC LIMIT 1 OFFSET 1) v_prev,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='likes' ORDER BY capture_date DESC LIMIT 1) l_now,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='comments' ORDER BY capture_date DESC LIMIT 1) c_now
  FROM platform_item pi WHERE pi.platform='youtube' AND pi.track_id IS NOT NULL),
yt AS (
  SELECT track_id,
         sum(review_state IN ('auto','approved','song')) yt_videos,
         sum(review_state='pending') yt_pending,
         sum(CASE WHEN review_state IN ('auto','approved','song') THEN v_now END) yt_views,
         sum(CASE WHEN review_state IN ('auto','approved','song') THEN v_now - v_prev END) yt_delta,
         sum(CASE WHEN review_state IN ('auto','approved','song') THEN l_now END) yt_likes,
         sum(CASE WHEN review_state IN ('auto','approved','song') THEN c_now END) yt_comments,
         max(channel) yt_channel,
         sum(CASE WHEN review_state IN ('auto','approved','song') AND asset_type='video' THEN v_now END) yt_views_video,
         sum(review_state IN ('auto','approved','song') AND asset_type='video') yt_n_video,
         sum(CASE WHEN review_state IN ('auto','approved','song') AND asset_type='lyric' THEN v_now END) yt_views_lyric,
         sum(review_state IN ('auto','approved','song') AND asset_type='lyric') yt_n_lyric,
         sum(CASE WHEN review_state IN ('auto','approved','song') AND asset_type='audio' THEN v_now END) yt_views_audio,
         sum(review_state IN ('auto','approved','song') AND asset_type='audio') yt_n_audio,
         sum(CASE WHEN review_state IN ('auto','approved','song') AND asset_type='short' THEN v_now END) yt_views_short,
         sum(review_state IN ('auto','approved','song') AND asset_type='short') yt_n_short,
         sum(CASE WHEN review_state IN ('auto','approved','song') AND asset_type='teaser' THEN v_now END) yt_views_teaser,
         sum(review_state IN ('auto','approved','song') AND asset_type='teaser') yt_n_teaser,
         sum(CASE WHEN review_state IN ('auto','approved','song') AND asset_type='other' THEN v_now END) yt_views_other,
         sum(review_state IN ('auto','approved','song') AND asset_type='other') yt_n_other
  FROM yt_item GROUP BY track_id),
dz AS (
  SELECT p.track_id,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND metric='rank' ORDER BY capture_date DESC LIMIT 1) deezer_rank,
         p.url deezer_url
  FROM platform_item p WHERE p.platform='deezer' AND p.track_id IS NOT NULL GROUP BY p.track_id),
js AS (
  SELECT p.track_id, p.url js_url, p.title js_title,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND metric='plays' ORDER BY capture_date DESC LIMIT 1) js_plays
  FROM platform_item p WHERE p.platform='jiosaavn' AND p.track_id IS NOT NULL GROUP BY p.track_id)
"""

TRACKS_SQL = """
SELECT * FROM (
  WITH """ + YT_CTE.strip() + """
  SELECT t.id, t.isrc, t.title, t.yt_title, t.source, t.duration_ms, t.explicit, t.first_published,
         t.orig_release, t.orig_release_source, t.release_check,
         coalesce(t.release_status, CASE WHEN r.id IS NULL THEN 'unchecked' WHEN r.album_type='compilation' THEN 'catalogue'
                                         WHEN r.release_date >= ? THEN 'new' ELSE 'catalogue' END) release_status,
         r.title release_title, r.release_date, r.album_type, r.label,
         sp.url, sp.platform_id spotify_id, sp.review_state spotify_state,
         substr(t.first_published,1,10) song_date,
         (SELECT group_concat(name, ', ') FROM (SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
          WHERE ta.track_id=t.id ORDER BY ta.position)) artists,
         coalesce(yt.yt_videos, 0) yt_videos, coalesce(yt.yt_pending, 0) yt_pending, yt.yt_views, yt.yt_delta,
         yt.yt_likes, yt.yt_comments, yt.yt_channel, dz.deezer_rank, dz.deezer_url, js.js_url, js.js_title, js.js_plays,
         (SELECT url FROM platform_item p WHERE p.track_id=t.id AND p.platform='ytmusic' ORDER BY p.id LIMIT 1) ytm_url,
         (SELECT url FROM platform_item p WHERE p.track_id=t.id AND p.platform='apple' ORDER BY p.id LIMIT 1) apple_url,
         (SELECT url FROM platform_item p WHERE p.track_id=t.id AND p.platform='musicbrainz' ORDER BY p.id LIMIT 1) mb_url,
         yt.yt_views_video, yt.yt_n_video,
         yt.yt_views_lyric, yt.yt_n_lyric,
         yt.yt_views_audio, yt.yt_n_audio,
         yt.yt_views_short, yt.yt_n_short,
         yt.yt_views_teaser, yt.yt_n_teaser,
         yt.yt_views_other, yt.yt_n_other,
         (SELECT count(*) FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify'
            AND p.review_state IN ('auto','approved')) spotify_versions,
         (SELECT p.platform_id FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
            AND p.review_state IN ('auto','approved','song')
          ORDER BY (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND metric='views'
                    ORDER BY capture_date DESC LIMIT 1) DESC LIMIT 1) yt_video_id
  FROM track t
  LEFT JOIN platform_item sp ON sp.track_id=t.id AND sp.platform='spotify'
    AND sp.id=(SELECT id FROM platform_item WHERE track_id=t.id AND platform='spotify'
               ORDER BY review_state NOT IN ('auto','approved'), match_method='same_recording', id LIMIT 1)
  LEFT JOIN release r ON r.id=t.release_id
  LEFT JOIN yt ON yt.track_id=t.id
  LEFT JOIN dz ON dz.track_id=t.id
  LEFT JOIN js ON js.track_id=t.id
) r
"""


def tracks_args():
    """TRACKS_SQL takes the window start (for the new/catalogue status) as its first parameter."""
    return [config.load()["discovery"]["since"]]


def api_tracks(conn, qs):
    since, until = config.window(config.load())
    where, args = ["song_date >= ?", "song_date <= ?"], [qs.get("since") or since, qs.get("until") or until]
    if focus_on(qs):
        where.append("coalesce(release_status, '') != 'catalogue'")
    if qs.get("all"):
        where, args = [], []
    if qs.get("q"):
        where.append("(title LIKE ? OR yt_title LIKE ? OR artists LIKE ? OR release_title LIKE ? OR isrc LIKE ?"
                     " OR id IN (SELECT track_id FROM song_meta WHERE value LIKE ?))")
        args += [f"%{qs['q']}%"] * 6
    for f in ("language", "category"):
        if qs.get(f):
            where.append("id IN (SELECT track_id FROM song_meta WHERE field=? AND value=?)")
            args += [f, qs[f]]
    if qs.get("meta"):  # field:value_key, from the Explore page
        f, _, k = qs["meta"].partition(":")
        where.append("id IN (SELECT track_id FROM song_meta WHERE field=? AND value_key=?)")
        args += [f, k]
    if qs.get("status"):
        where.append("release_status=?")
        args.append(qs["status"])
    cfg = config.load()
    if qs.get("label"):
        titles = [t for t, lb in config.channel_labels(cfg).items() if lb == qs["label"]]
        where.append("yt_channel IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(titles))
    if qs.get("channel"):
        where.append("yt_channel=?")
        args.append(qs["channel"])
    if qs.get("day"):
        where.append("song_date=?")
        args.append(qs["day"])
    sort = SORTS.get(qs.get("sort"), "song_date")
    order = "ASC" if qs.get("order") == "asc" else "DESC"
    w = (" WHERE " + " AND ".join(where)) if where else ""
    limit = min(int(qs.get("limit", 50)), 500)
    offset = int(qs.get("offset", 0))
    a = tracks_args() + args
    total = conn.execute(f"SELECT count(*) FROM ({TRACKS_SQL}{w})", a).fetchone()[0]
    rows = q(conn, f"{TRACKS_SQL}{w} ORDER BY {sort} {order} NULLS LAST, r.id DESC LIMIT ? OFFSET ?",
             a + [limit, offset])
    lab = config.channel_labels(cfg)
    seen = [r[0] for r in conn.execute("SELECT DISTINCT channel FROM platform_item WHERE platform='youtube' "
                                       "AND channel IS NOT NULL ORDER BY 1")]
    channels = [{"title": c, "label": lab.get(c, config.FIRST_LABEL)} for c in seen]
    fl = metadata.flat(conn, [r["id"] for r in rows])
    for r in rows:
        r["meta"] = {f: fl[r["id"]].get(f) for f in ("singer", "music_director", "lyricist", "film", "language", "category")}
    facets = {f: [r[0] for r in conn.execute("SELECT value FROM song_meta WHERE field=? GROUP BY value ORDER BY count(*) DESC", (f,))]
              for f in ("language", "category")}
    return {"facets": facets, "total": total, "rows": tag_labels(rows, cfg), "channels": channels,
            "labels": [lb["name"] for lb in cfg["youtube"]["labels"]]}


# Platform data page: every item (video / track) of one platform with its dates and its stats on a chosen day,
# compared with another day. Filtering, sorting and paging run in SQL, so only one page leaves the server.
ITEM_PLATFORMS = {  # platform: (label, metrics, review states that count)
    "youtube": ("YouTube", ["views", "likes", "comments"], ("auto", "approved", "song")),
    "ytmusic": ("YouTube Music", ["plays"], ("auto", "approved")),
    "spotify": ("Spotify", ["plays"], ("auto", "approved")),
    "jiosaavn": ("JioSaavn", ["plays"], ("auto", "approved")),
    "apple": ("Apple Music", [], ("auto", "approved")),
    "deezer": ("Deezer", ["rank"], ("auto", "approved")),
}


def _items_query(conn, qs):
    plat = qs.get("platform") if qs.get("platform") in ITEM_PLATFORMS else "youtube"
    _, metrics, states = ITEM_PLATFORMS[plat]
    # Days this platform has readings for (uses the day-first index); the default is its latest.
    days = [r[0] for r in conn.execute("""SELECT DISTINCT s.capture_date FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                                          WHERE s.metric=? AND p.platform=? ORDER BY 1""", (metrics[0], plat))] if metrics else []
    day = qs.get("day") or (days[-1] if days else date.today().isoformat())
    cmp = qs.get("cmp") or max((d for d in days if d < day), default=None)
    joins, cols, args = [], [], []
    for m in metrics:
        joins.append(f"LEFT JOIN snapshot a_{m} ON a_{m}.platform_item_id=p.id AND a_{m}.metric=? AND a_{m}.capture_date=?")
        joins.append(f"LEFT JOIN snapshot b_{m} ON b_{m}.platform_item_id=p.id AND b_{m}.metric=? AND b_{m}.capture_date=?")
        args += [m, day, m, cmp or ""]
        cols.append(f"a_{m}.value {m}, CASE WHEN a_{m}.value IS NOT NULL AND b_{m}.value IS NOT NULL THEN a_{m}.value - b_{m}.value END {m}_change")
    where = ["p.platform=?"]
    wargs = [plat]
    if focus_on(qs):  # items of new-release songs only (YouTube uploads that are not songs are hidden too)
        where.append("coalesce(t.release_status, '') != 'catalogue' AND p.track_id IS NOT NULL")
    if plat == "youtube":  # every collected upload of the period; linked ones show their song
        where.append("p.published_at >= ?")
        wargs.append(config.window(config.load())[0])
    else:
        where.append(f"p.track_id IS NOT NULL AND p.review_state IN ({','.join('?' * len(states))})")
        wargs += list(states)
    if qs.get("q"):
        where.append("(p.title LIKE ? OR t.title LIKE ?)")
        wargs += [f"%{qs['q']}%"] * 2
    lab = config.channel_labels(config.load())
    if qs.get("label"):
        chans = [c for c, l in lab.items() if l == qs["label"]] or ["-"]
        ch_expr = "p.channel" if plat == "youtube" else "(SELECT y.channel FROM platform_item y WHERE y.track_id=p.track_id AND y.platform='youtube' ORDER BY y.published_at LIMIT 1)"
        where.append(f"{ch_expr} IN ({','.join('?' * len(chans))})")
        wargs += chans
    if qs.get("rel_from"):
        where.append("substr(p.published_at,1,10) >= ?")
        wargs.append(qs["rel_from"])
    if qs.get("rel_to"):
        where.append("substr(p.published_at,1,10) <= ?")
        wargs.append(qs["rel_to"])
    all_where = list(where)
    if metrics and qs.get("read") != "all":  # default: only items read on the chosen day
        where.append(f"a_{metrics[0]}.value IS NOT NULL")
    base = f"""FROM platform_item p LEFT JOIN track t ON t.id=p.track_id {' '.join(joins)} WHERE {' AND '.join(where)}"""
    base_all = f"""FROM platform_item p LEFT JOIN track t ON t.id=p.track_id {' '.join(joins)} WHERE {' AND '.join(all_where)}"""
    select = f"""SELECT p.id, p.platform_id, p.title, p.url, p.channel, p.asset_type, substr(p.published_at,1,10) released,
                        substr(p.first_seen_at,1,10) first_seen, p.track_id song_id, t.title song,
                        (SELECT count(DISTINCT capture_date) FROM snapshot s WHERE s.platform_item_id=p.id) days_tracked
                        {', ' + ', '.join(cols) if cols else ''} {base}"""
    sortable = {"song", "title", "released", "first_seen", "days_tracked", "channel"} | set(metrics) | {f"{m}_change" for m in metrics}
    sort = qs.get("sort") if qs.get("sort") in sortable else (f"{metrics[0]}_change" if metrics else "released")
    order = "ASC" if qs.get("order") == "asc" else "DESC"
    return plat, metrics, day, cmp, days, select, (base, base_all), args + wargs, sort, order


# Cross-platform comparison of one song: per platform its release date, latest total, recent gains, share of all
# plays, link, and the daily series (from song_daily) for a chart.
COMPARE = [("youtube", "YouTube", "yt_views", "views"), ("ytmusic", "YouTube Music", "ytm_plays", "plays"),
           ("spotify", "Spotify", "sp_plays", "plays"), ("jiosaavn", "JioSaavn", "js_plays", "plays"), ("deezer", "Deezer", "dz_rank", "rank")]


def api_song_compare(conn, qs):
    sid = int(qs["id"])
    song_daily.ensure(conn)
    days = q(conn, "SELECT * FROM song_daily WHERE song_id=? ORDER BY day", (sid,))
    t = q(conn, "SELECT id, title FROM track WHERE id=?", (sid,))
    out = []
    for plat, label, col, unit in COMPARE:
        info = q(conn, f"""SELECT min(substr(published_at,1,10)) released, count(*) items,
                              (SELECT url FROM platform_item x WHERE x.track_id=? AND x.platform=? ORDER BY
                                 {"(SELECT max(value) FROM snapshot s WHERE s.platform_item_id=x.id AND s.metric='views') DESC" if plat == "youtube" else "x.id"} LIMIT 1) url
                           FROM platform_item WHERE track_id=? AND platform=?
                             AND (platform NOT IN ('youtube','spotify') OR review_state IN ('auto','approved','song'))""", (sid, plat, sid, plat))[0]
        series = [{"day": d["day"], "total": d.get(col), "gain": d.get(col + "_gain") if col != "dz_rank" else None} for d in days]
        have = [x for x in series if x["total"] is not None]
        last = have[-1] if have else None
        week = [x["gain"] for x in series[-7:] if x["gain"] is not None]
        out.append({"platform": plat, "label": label, "unit": unit, "released": info["released"], "items": info["items"], "url": info["url"],
                    "latest": last["total"] if last else None, "latest_day": last["day"] if last else None,
                    "gain_1d": series[-1]["gain"] if series else None, "gain_7d": sum(week) if week else None, "series": series})
    plays = [o for o in out if o["unit"] != "rank" and o["latest"]]
    tot = sum(o["latest"] for o in plays)
    for o in out:
        o["share"] = round(100 * o["latest"] / tot, 1) if tot and o in plays else None
    return {"song": t[0] if t else None, "platforms": out, "days": [d["day"] for d in days], "total_plays": tot}


def api_items(conn, qs):
    plat, metrics, day, cmp, days, select, base, args, sort, order = _items_query(conn, qs)
    off, lim = max(int(qs.get("offset") or 0), 0), min(max(int(qs.get("limit") or 100), 1), 500)
    rows = q(conn, f"{select} ORDER BY {sort} IS NULL, {sort} {order} LIMIT ? OFFSET ?", args + [lim, off])
    lab = config.channel_labels(config.load())
    for r in rows:
        r["label"] = lab.get(r["channel"], "")
    base, base_all = base
    agg = "".join(f", sum(a_{m}.value) {m}, sum(CASE WHEN a_{m}.value IS NOT NULL AND b_{m}.value IS NOT NULL THEN a_{m}.value - b_{m}.value END) {m}_change"
                  for m in metrics if m != "rank")
    tot = q(conn, f"SELECT count(*) n{agg}, sum(substr(p.published_at,1,10)=?) new_on_day {base}", [day] + args)[0]
    tracked = q(conn, f"SELECT count(*) n {base_all}", args)[0]["n"] if metrics and qs.get("read") != "all" else tot["n"]
    return {"platform": plat, "label": ITEM_PLATFORMS[plat][0], "metrics": metrics, "day": day, "cmp": cmp, "days": days,
            "sort": sort, "order": order.lower(), "total": tot["n"], "totals": tot, "tracked": tracked, "rows": rows,
            "platforms": [{"key": k, "label": v[0]} for k, v in ITEM_PLATFORMS.items()],
            "labels": sorted(set(lab.values()))}


def api_item_history(conn, qs):
    iid = int(qs["id"])
    item = q(conn, """SELECT p.id, p.platform, p.platform_id, p.title, p.url, p.channel, substr(p.published_at,1,10) released,
                             substr(p.first_seen_at,1,10) first_seen, t.title song, p.track_id song_id
                      FROM platform_item p LEFT JOIN track t ON t.id=p.track_id WHERE p.id=?""", (iid,))
    if not item:
        raise ValueError("item not found")
    rows = q(conn, "SELECT capture_date day, metric, value FROM snapshot WHERE platform_item_id=? ORDER BY capture_date", (iid,))
    by = {}
    for r in rows:
        by.setdefault(r["day"], {"day": r["day"]})[r["metric"]] = r["value"]
    hist = [by[d] for d in sorted(by)]
    for i, h in enumerate(hist):  # change from the previous calendar day only (a gap leaves it empty)
        prev = hist[i - 1] if i and (date.fromisoformat(h["day"]) - date.fromisoformat(hist[i - 1]["day"])).days == 1 else None
        for m in ITEM_PLATFORMS.get(item[0]["platform"], ("", [], ()))[1]:
            h[m + "_change"] = (h[m] - prev[m]) if prev and h.get(m) is not None and prev.get(m) is not None else None
    return {"item": item[0], "metrics": ITEM_PLATFORMS.get(item[0]["platform"], ("", [], ()))[1], "history": hist}


def items_csv(conn, qs):
    plat, metrics, day, cmp, days, select, _, args, sort, order = _items_query(conn, qs)
    buf = io.StringIO()
    w = exports.IdFreeWriter(buf)  # downloads carry no IDs or links
    head = ["item_id", "platform_id", "song_id", "song", "title", "url", "channel", "released", "first_seen", "days_tracked"]
    head += [x for m in metrics for x in (f"{m} {day}", f"{m} change vs {cmp}")]
    w.writerow(head)
    for r in conn.execute(f"{select} ORDER BY {sort} IS NULL, {sort} {order}", args):
        r = dict(r)
        w.writerow([r["id"], r["platform_id"], r["song_id"], r["song"], r["title"], r["url"], r["channel"], r["released"],
                    r["first_seen"], r["days_tracked"]] + [x for m in metrics for x in (r[m], r[m + "_change"])])
    return buf.getvalue(), f"market_lens_{plat}_{day}.csv"


def api_track(conn, qs):
    tid = int(qs["id"])
    t = q(conn, TRACKS_SQL + " WHERE id=?", tracks_args() + [tid])
    if not t:
        return {"error": "not found"}
    items = q(conn, """SELECT pi.id, platform, platform_id, asset_type, title, url, match_method, match_score,
                              review_state, match_detail, channel, substr(published_at,1,10) published,
                              (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='views'
                               ORDER BY capture_date DESC LIMIT 1) views,
                              (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='likes'
                               ORDER BY capture_date DESC LIMIT 1) likes,
                              (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='comments'
                               ORDER BY capture_date DESC LIMIT 1) comments
                       FROM platform_item pi WHERE track_id=? ORDER BY platform, views DESC""", (tid,))
    for i in items:
        i["match_detail"] = json.loads(i["match_detail"]) if i["match_detail"] else None
        if i["match_detail"]:
            i["match_detail"].pop("candidate", None)
    history = q(conn, """SELECT s.capture_date, pi.platform, s.metric, s.value FROM snapshot s
                         JOIN platform_item pi ON pi.id=s.platform_item_id
                         WHERE pi.track_id=? ORDER BY s.capture_date""", (tid,))
    releases = q(conn, """SELECT DISTINCT r.title, r.release_date, r.album_type, r.label, r.upc, r.label_match,
                                 r.discovered_via, r.spotify_album_id
                          FROM release r WHERE r.id=(SELECT release_id FROM track WHERE id=?)""", (tid,))
    by_type = {}
    for i in items:
        if i["platform"] == "youtube" and i["review_state"] in ("auto", "approved", "song"):
            b = by_type.setdefault(i["asset_type"] or "other", {"type": i["asset_type"] or "other", "videos": 0,
                                                                  "views": 0, "likes": 0, "comments": 0})
            b["videos"] += 1
            for k in ("views", "likes", "comments"):
                b[k] += i[k] or 0
    aliases = [r[0] for r in conn.execute("SELECT song_key FROM song_alias WHERE track_id=?", (tid,))]
    chart = spotify_charts.song_chart(conn, tid)
    if chart:  # daily India chart rank as one more series for the drawer's trend chart
        history = list(history) + [{"platform": "spotify_chart", "metric": "chart_rank", "capture_date": h["chart_date"], "value": h["rank"]}
                                   for h in chart["history"]]
    # Soft delete: uploads and links removed from this song stay stored and can be restored (the decision's exact undo).
    removed = q(conn, """SELECT d.id decision_id, d.decided_at, d.decided_by, d.platform, p.title, p.url, p.channel
                         FROM review_decision d JOIN platform_item p ON p.id=d.item_id
                         WHERE d.track_id=? AND d.action='reject' AND d.undone_at IS NULL ORDER BY d.id DESC""", (tid,))
    return {"meta": metadata.for_song(conn, tid), "track": tag_labels(t)[0], "items": items, "history": history, "removed": removed,
            "releases": releases, "chart": chart and {k: v for k, v in chart.items() if k != "history"},
            "by_type": sorted(by_type.values(), key=lambda b: -b["views"]), "merged_names": aliases}


def api_runs(conn, _):
    rows = q(conn, "SELECT id, job, started_at, finished_at, status, stats FROM run_log ORDER BY id DESC LIMIT 50")
    for r in rows:
        r["stats"] = json.loads(r["stats"]) if r["stats"] else {}
    return {"runs": rows}


def api_seeds(conn, _):
    return {"seeds": [{"id": i, "note": n} for i, n in config.read_seeds()]}


def spotify_client():
    s = config.read_secrets()
    return Spotify.from_secrets(s, market=config.load()["discovery"]["market"])


def api_artist_search(conn, qs):
    sp = spotify_client()
    out = []
    for item in sp.search(qs.get("q", ""), "artist"):
        if isinstance(item, tuple) or len(out) >= 10:
            break
        out.append({"id": item["id"], "name": item["name"],
                    "followers": (item.get("followers") or {}).get("total"),
                    "image": ((item.get("images") or [{}])[-1] or {}).get("url")})
    return {"artists": out}


def post_config(conn, body):
    cfg = config.load()
    for section in ("discovery", "schedule", "health", "youtube", "worker"):
        cfg.setdefault(section, {}).update(body.get(section, {}))
    if "merge" in body:
        m = body["merge"]
        cfg["merge"]["across_labels"] = bool(m.get("across_labels", cfg["merge"]["across_labels"]))
        for rule, p in (m.get("rules") or {}).items():
            if rule not in merge.RULES:
                raise ValueError(f"unknown merge rule {rule}")
            r = cfg["merge"]["rules"].setdefault(rule, {})
            r.update(p)
            if r.get("action") not in ("suggest", "auto"):
                raise ValueError("merge action must be suggest or auto")
            for k, lo, hi in (("min_similarity", 0.5, 1), ("max_days_apart", 0, 3650), ("max_length_diff_s", 0, 600),
                              ("min_words", 1, 10)):
                if k in r:
                    r[k] = float(r[k]) if k == "min_similarity" else int(r[k])
                    if not lo <= r[k] <= hi:
                        raise ValueError(f"{rule}: {k} must be between {lo} and {hi}")
    d, s = cfg["discovery"], cfg["schedule"]
    for k in ("since", "until"):
        if d[k]:
            date.fromisoformat(d[k])
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s["time"]):
        raise ValueError("schedule time must be HH:MM")
    re.compile(d["label_regex"])
    d["max_calls_per_run"] = int(d["max_calls_per_run"])
    d["isrc_lookups_per_run"] = int(d["isrc_lookups_per_run"])
    if d["max_calls_per_run"] < 50 or d["isrc_lookups_per_run"] < 0:
        raise ValueError("call budget must be at least 50 and ISRC lookups 0 or more")
    y = cfg["youtube"]
    for k, lo in (("every_hours", 1), ("fresh_days", 0), ("lookback_days", 0)):
        y[k] = int(y[k])
        if y[k] < lo:
            raise ValueError(f"youtube {k} must be at least {lo}")
    h = cfg["health"]
    h["recheck_minutes"] = int(h["recheck_minutes"])
    if h["recheck_minutes"] < 5:
        raise ValueError("re-check interval must be at least 5 minutes")
    d["label_queries"] = [x.strip() for x in d["label_queries"] if x.strip()]
    if not d["label_queries"]:
        raise ValueError("at least one label query is needed")
    return {"config": config.save(cfg)}


def post_secrets(conn, body):
    config.write_secrets({k: body.get(k, "") for k in config.SECRET_KEYS})
    return {"secrets": secret_status()}


def post_test_spotify(conn, body):
    return health.check_spotify(conn, trigger="manual")


def api_health(conn, _):
    checks = q(conn, "SELECT id, checked_at, ok, status, message, trigger FROM api_check WHERE source='spotify' "
                     "ORDER BY id DESC LIMIT 30")
    cfg = config.load()["health"]
    return {"spotify": {"last": checks[0] if checks else None, "history": checks}, "config": cfg}


def api_datasets(conn, _):
    return exports.listing(conn)


def api_dataset_view(conn, qs):
    return exports.view(conn, qs)


def api_raw(conn, qs):
    where, args = [], []
    if qs.get("run_id"):
        where.append("run_id=?")
        args.append(int(qs["run_id"]))
    if qs.get("result") == "ok":
        where.append("status BETWEEN 200 AND 299")
    elif qs.get("result") == "error":
        where.append("(status IS NULL OR status NOT BETWEEN 200 AND 299)")
    if qs.get("q"):
        where.append("url LIKE ?")
        args.append(f"%{qs['q']}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    offset, limit = int(qs.get("offset", 0)), min(int(qs.get("limit", 50)), 500)
    total = conn.execute(f"SELECT count(*) FROM raw_capture{w}", args).fetchone()[0]
    rows = q(conn, f"SELECT id, run_id, source, url, status, captured_at, length(body) size FROM raw_capture{w} "
                   "ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset])
    runs = q(conn, "SELECT run_id, count(*) n, sum(status NOT BETWEEN 200 AND 299) errors FROM raw_capture "
                   "GROUP BY run_id ORDER BY run_id DESC")
    return {"total": total, "rows": rows, "runs": runs}


def api_raw_item(conn, qs):
    r = q(conn, "SELECT * FROM raw_capture WHERE id=?", (int(qs["id"]),))
    if not r:
        raise ValueError("not found")
    return r[0]


def post_seeds(conn, body):
    seeds = [(s["id"].strip(), (s.get("note") or "").replace("\n", " ").strip())
             for s in body.get("seeds", []) if re.fullmatch(r"[A-Za-z0-9]{22}", s.get("id", "").strip())]
    config.write_seeds(seeds)
    return api_seeds(conn, None)


def post_run(conn, body):
    for k in ("since", "until"):
        if body.get(k):
            date.fromisoformat(body[k])  # ValueError -> 400
    if body.get("since") and body.get("until") and body["since"] > body["until"]:
        raise ValueError("'from' is after 'to'")
    if body.get("job") == "spotify_second_account":
        s = config.read_secrets()
        if not (s.get("SPOTIFY_CLIENT_ID_2") and s.get("SPOTIFY_CLIENT_SECRET_2")):
            raise ValueError("Configure SPOTIFY_CLIENT_ID_2 and SPOTIFY_CLIENT_SECRET_2 first")
    run_id = MANAGER.start(body.get("job", "spotify_discovery"), since=body.get("since"), until=body.get("until"))
    return {"run_id": run_id}


def api_shield(conn, qs):
    """Developer: client logins' access today (requests, IPs, sessions, songs opened, blocks) and the latest blocks."""
    return shield.summary()


def api_heal(conn, qs):
    """Page readers' health (developer): which way each reader found its values today and this week, held outcomes,
    learned markers, kept samples and suspicious values (lens/heal.py)."""
    from . import heal
    return heal.summary(conn)


def api_additions(conn, qs):
    """Daily additions page (developer): songs added each day, and the songs first linked on each platform that day."""
    from . import additions
    one = lambda k, d="": ((qs.get(k) or [d])[0] if isinstance(qs.get(k), list) else (qs.get(k) or d))
    try:
        days = int(one("days", "30"))
    except ValueError:
        days = 30
    return additions.report(conn, config.load(), days, one("label") or None)


def api_freshness(conn, qs):
    """Missed readings page: items of the songs in scope not read within the hours, per platform, with the last runs."""
    from . import freshness
    cfg = config.load()
    plat = (qs.get("platform") or [""])[0] if isinstance(qs.get("platform"), list) else (qs.get("platform") or "")
    rows = freshness.missed(conn, cfg, plat or None)
    return {"settings": freshness.settings(cfg), "counts": freshness.counts(conn, cfg), "names": freshness.NAMES,
            "runs": freshness.last_runs(conn), "stuck": freshness.stuck(), "missed": rows[:3000], "missed_total": len(rows),
            "marked": freshness.marked(conn, cfg),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def post_freshness_retry(conn, body):
    """Missed readings page: start the reading job of a platform (or of every platform with missed items) now."""
    from . import freshness
    plats = [body["platform"]] if body.get("platform") else \
        [p for p, c in freshness.counts(conn, config.load()).items() if c["missed"]]
    started, busy = [], []
    for p in plats:
        if p not in freshness.PLATFORMS:
            raise ValueError("unknown platform")
        job = freshness.PLATFORMS[p][1]
        if job in [x["job"] for x in started]:
            continue
        mgr = BROWSER if job in jobs.BROWSER_JOBS else MANAGER
        try:
            started.append({"platform": p, "job": job, "run_id": mgr.start(job, trigger="manual")})
        except RuntimeError as e:  # its lane is busy (or the job is paused): the watchdog starts it when the lane is free
            busy.append({"platform": p, "job": job, "why": str(e)})
    return {"started": started, "busy": busy}


def post_freshness_mark(conn, body):
    from . import freshness
    if body.get("clear"):
        freshness.clear_mark(conn, body["item_id"], body["metric"])
    else:
        freshness.mark_unavailable(conn, body["item_id"], body["platform"])
    return {"ok": True}


def api_label_report(conn, qs):
    """Reports → Labels: ?view=daily&month=YYYY-MM (a month day by day, default this month) or ?view=monthly (completed
    months); ?by= ranks the labels (releases, yt, audio, sp, ytm, js)."""
    from . import label_report
    by = qs.get("by") if qs.get("by") in label_report.METRICS else "yt"
    if qs.get("view") == "monthly":
        return label_report.monthly(conn, by)
    month = qs.get("month") or None
    if month:
        date.fromisoformat(month + "-01")  # ValueError -> 400
    return label_report.daily(conn, month, by)


def api_market_share(conn, qs):
    """Market share page: ?days=N (default market_share.days), ?month=YYYY-MM or ?since=&until= (songs released then)."""
    from . import market_share
    days = int(qs["days"]) if (qs.get("days") or "").isdigit() else None
    since, until = qs.get("since") or None, qs.get("until") or None
    if qs.get("month"):
        m = qs["month"]
        first = date.fromisoformat(m + "-01")
        since, until = first.isoformat(), ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).isoformat()
    for x in (since, until):
        if x:
            date.fromisoformat(x)
    d = market_share.compute(conn, days, since, until)
    if d.get("months") is None:  # the month list always covers the whole period
        d["months"] = sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT substr(first_published,1,7) FROM track WHERE source='youtube' AND first_published >= ? AND old_release IS NULL",
            (d["period"][0],))}, reverse=True)
    return d


def post_market_share_settings(conn, body):
    from . import market_share
    return market_share.save_days(body.get("days"))


def api_management(conn, qs):
    """Management summary: ?genre=category|apple, ?method=split|primary|full (several genres on one song)."""
    from . import management
    g = "apple" if qs.get("genre") == "apple" else "category"
    m = qs.get("method") if qs.get("method") in management.GENRE_METHODS else "split"
    return management.compute(conn, g, m)


def api_cast(conn, qs):
    """Cast & featuring: every cast member's growth on each platform, charts and rule-based insights (client + developer)."""
    from . import cast_report
    return cast_report.compute(conn, days=int(qs.get("days") or 30), label=qs.get("label") or None,
                               language=qs.get("language") or None, sort=qs.get("sort") or "yt", order=qs.get("order") or "desc",
                               q=(qs.get("q") or "").strip()[:80] or None, limit=int(qs.get("limit") or 50),
                               offset=int(qs.get("offset") or 0), platform=qs.get("platform") or "yt")


def api_cast_detail(conn, qs):
    """One cast member: platform KPIs and ranks, daily series per platform, songs, platform mix vs market, collaborators."""
    from . import cast_report
    if not qs.get("key"):
        raise ValueError("key is required")
    return cast_report.detail(conn, qs["key"], days=int(qs.get("days") or 30), field=qs.get("field") or "cast")  # any credit role (developer)


def api_live(conn, qs):
    """Status page: per source read today / waiting, quota used and left, backlogs, and both job lanes."""
    from . import live_status
    y = api_youtube_status(conn, None)
    return live_status.compute(conn, MANAGER, BROWSER, {"units_today": y["units_today"], "units_limit": y["units_limit"],
                                                        "keys": len(y["keys"]), "resets": y["quota_resets"]})


def api_alerts(conn, qs):
    """Developer alerts: checked now, with what is open, snoozed and recently closed. If a job holds the database for a
    long write, the stored list is shown as it is (the work loop refreshes it every 10 minutes)."""
    try:
        alerts.sync(conn)
    except sqlite3.OperationalError:
        conn.rollback()
    return alerts.listing(conn)


def post_alert_snooze(conn, body):
    alerts.snooze(conn, body["key"], int(body.get("hours", 24)))
    return alerts.listing(conn)


def post_charts_token(conn, body):
    """Save a charts.spotify.com session token (never sent back) and import the missing chart days in the background."""
    alerts.save_charts_token(body.get("token"))
    return {"started": alerts.import_charts()}


def post_instagram_link(conn, body):
    """Developer: attach an Instagram audio link to a song (or remove one with remove=<item id>)."""
    from . import instagram_audio
    if body.get("remove"):
        instagram_audio.unlink(conn, int(body["remove"]))
        return {"ok": True}
    return {"ok": True, "item_id": instagram_audio.link(conn, int(body["song"]), body.get("url") or "", who="developer")}


def post_jobs_pause(conn, body):
    """Pause jobs: every job (no 'jobs') or the listed ones, for 'minutes' (default 30; it ends by itself).
    'now': also stop the running jobs it covers at their next safe point (data read so far is kept)."""
    jobs_ = body.get("jobs") or None
    if jobs_:
        unknown = [j for j in jobs_ if j not in jobs.JOBS]
        if unknown:
            raise ValueError(f"unknown jobs: {', '.join(unknown)}")
    p = pause.pause(conn, REQUEST.actor, (body.get("reason") or "").strip()[:200], body.get("minutes") or pause.DEFAULT_MINUTES,
                    jobs_, now=bool(body.get("now")))
    new = {"all": jobs_ is None, "jobs": jobs_}  # "now" stops only the jobs this request covers
    stopping = [j for j in (MANAGER.pause_now(new), BROWSER.pause_now(new)) if j] if body.get("now") else []
    return {"pause": p, "stopping": stopping}


def post_jobs_resume(conn, body):
    ended = pause.resume(conn, REQUEST.actor, (body.get("reason") or "").strip()[:200], body.get("jobs") or None)
    return {"resumed": bool(ended), "was": ended}


def post_cancel(conn, body):
    """Stop the running job in both lanes (API jobs and the browser job)."""
    return {"cancelled": MANAGER.cancel(), "cancelled_browser": BROWSER.cancel()}


MATCH_ROW_SQL = """
SELECT pi.id item_id, pi.platform_id video_id, pi.title video_title, pi.channel, pi.asset_type,
       substr(pi.published_at,1,10) published, pi.duration_ms video_ms, pi.url, pi.review_state, pi.match_method,
       pi.match_score, pi.match_detail,
       (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='views' ORDER BY capture_date DESC LIMIT 1) views,
       t.id track_id, t.title track_title, t.duration_ms track_ms, r.release_date, r.title release_title, r.album_type,
       (SELECT group_concat(name, ', ') FROM (SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
        WHERE ta.track_id=t.id ORDER BY ta.position)) artists
FROM platform_item pi LEFT JOIN track t ON t.id=pi.track_id LEFT JOIN release r ON r.id=t.release_id
WHERE pi.platform='youtube'
"""


def api_matching(conn, qs):
    state = qs.get("state", "pending")
    limit, offset = min(int(qs.get("limit", 50)), 200), int(qs.get("offset", 0))
    like = f"%{qs.get('q', '')}%"
    since = config.load()["discovery"]["since"]
    until = config.window(config.load())[1]
    # Spotify links of the songs the Console shows: in the period, and older releases only with "All songs" on
    sp_window = "t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?" + focus(qs).replace("release_status", "t.release_status")
    if state == "unmatched_tracks":
        base = TRACKS_SQL + " WHERE yt_videos = 0 AND song_date >= ? AND (title LIKE ? OR artists LIKE ? OR release_title LIKE ?)"
        args = tracks_args() + [since, like, like, like]
        total = conn.execute(f"SELECT count(*) FROM ({base})", args).fetchone()[0]
        rows = q(conn, base + " ORDER BY yt_pending DESC, release_date DESC LIMIT ? OFFSET ?", args + [limit, offset])
    elif state in ("spotify_pending", "spotify_linked"):
        base = """SELECT sp.id item_id, sp.platform_id spotify_id, sp.title spotify_title, sp.url, sp.match_score,
                         sp.match_detail, substr(sp.published_at,1,10) spotify_release, sp.duration_ms spotify_ms,
                         t.id track_id, t.yt_title, t.first_published,
                         (SELECT title FROM platform_item v WHERE v.track_id=t.id AND v.platform='youtube'
                          ORDER BY v.asset_type IN ('audio','lyric') DESC LIMIT 1) video_title,
                         (SELECT url FROM platform_item v WHERE v.track_id=t.id AND v.platform='youtube'
                          ORDER BY v.asset_type IN ('audio','lyric') DESC LIMIT 1) video_url
                  FROM platform_item sp JOIN track t ON t.id=sp.track_id
                  WHERE sp.platform='spotify' AND sp.review_state IN ({states}) AND {window}
                    AND (sp.title LIKE ? OR t.yt_title LIKE ?)""".format(
            states="'pending'" if state == "spotify_pending" else "'auto','approved'", window=sp_window)
        args = [since, until, like, like]
        total = conn.execute(f"SELECT count(*) FROM ({base})", args).fetchone()[0]
        order = "sp.match_score DESC" if state == "spotify_pending" else "sp.match_score ASC"  # weakest first
        rows = q(conn, base + f" ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
        for r in rows:
            d = json.loads(r["match_detail"]) if r["match_detail"] else {}
            cand = d.pop("candidate", {}) or {}
            r["match_detail"] = d
            if cand:
                r["spotify_artists"] = ", ".join(a["name"] for a in cand.get("artists", []))
                r["spotify_album"] = (cand.get("album") or {}).get("name")
                r["isrc"] = (cand.get("external_ids") or {}).get("isrc")
            else:
                x = q(conn, """SELECT t.isrc, r.title album,
                                      (SELECT group_concat(name, ', ') FROM (SELECT a.name FROM track_artist ta
                                       JOIN artist a ON a.id=ta.artist_id WHERE ta.track_id=t.id ORDER BY ta.position)) artists
                               FROM track t LEFT JOIN release r ON r.id=t.release_id WHERE t.id=?""", (r["track_id"],))[0]
                r["spotify_artists"], r["spotify_album"], r["isrc"] = x["artists"], x["album"], x["isrc"]
    else:
        cond = {"pending": "pi.review_state='pending'", "auto": "pi.review_state='auto'",
                "approved": "pi.review_state='approved'",
                "unmatched_videos": "pi.track_id IS NULL AND pi.asset_type IN ('video','lyric','audio','other') "
                                    f"AND pi.published_at >= '{since}'"}[state]
        base = MATCH_ROW_SQL + f" AND {cond} AND (pi.title LIKE ? OR coalesce(t.title,'') LIKE ?)"
        args = [like, like]
        total = conn.execute(f"SELECT count(*) FROM ({base})", args).fetchone()[0]
        order = "views DESC" if state == "unmatched_videos" else "pi.match_score DESC"
        rows = q(conn, base + f" ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
        for r in rows:
            r["match_detail"] = json.loads(r["match_detail"]) if r["match_detail"] else None
    s = q(conn, f"""SELECT count(*) tracks, sum(yt_videos > 0) matched,
                           (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='pending') pending,
                           (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='auto') auto,
                           (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='approved') approved,
                           (SELECT count(*) FROM platform_item WHERE platform='youtube' AND track_id IS NULL
                              AND asset_type IN ('video','lyric','audio','other') AND published_at >= ?) unmatched_videos,
                           (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='song') grouped,
                           (SELECT count(*) FROM platform_item sp JOIN track t ON t.id=sp.track_id WHERE sp.platform='spotify'
                              AND sp.review_state='pending' AND {sp_window}) spotify_pending,
                           (SELECT count(*) FROM platform_item sp JOIN track t ON t.id=sp.track_id WHERE sp.platform='spotify'
                              AND sp.review_state IN ('auto','approved') AND {sp_window}) spotify_linked
                    FROM ({TRACKS_SQL}) WHERE song_date >= ?""", [since, since, until, since, until] + tracks_args() + [since])[0]
    s["unmatched_tracks"] = (s["tracks"] or 0) - (s["matched"] or 0)
    return {"total": total, "rows": rows, "summary": s}


def refresh_meta(conn, track_ids):
    """Rebuild the metadata of songs a decision changed (no API calls)."""
    ids = [r[0] for r in conn.execute(f"SELECT id FROM track WHERE id IN ({','.join('?' * len(track_ids))})", list(track_ids))] \
        if track_ids else []
    if ids:
        cfg = config.load()
        metadata.build(conn, cfg, *config.window(cfg), log=lambda m: None, track_ids=ids)


def post_match_decide(conn, body):
    item = q(conn, "SELECT id, track_id, platform, match_detail, title FROM platform_item WHERE id=? AND platform IN ('youtube','spotify')",
             (int(body["item_id"]),))
    if not item:
        raise ValueError("item not found")
    item, action = item[0], body.get("action")
    if action not in ("approve", "reject"):
        raise ValueError("action must be approve or reject")
    # Snapshot everything this decision can touch, including a track it may merge into (same ISRC).
    tids = {item["track_id"]}
    cand = (json.loads(item["match_detail"] or "{}") or {}).get("candidate") or {}
    isrc = (cand.get("external_ids") or {}).get("isrc")
    if isrc:
        tids |= {r["id"] for r in q(conn, "SELECT id FROM track WHERE isrc=?", (isrc,))}
    before = review.snapshot(conn, tids, [item["id"]])
    song = q(conn, "SELECT coalesce(yt_title, title) t FROM track WHERE id=?", (item["track_id"],))
    summary = f"{(song[0]['t'] if song else '?')} ↔ {item['platform']}: {item['title']}"
    result = _apply_decision(conn, item, action)
    review.record(conn, REQUEST.actor, action, item["platform"], item["id"], item["track_id"], summary, before,
                  rejection=[item["id"], item["track_id"]] if action == "reject" and item["track_id"] else None)
    conn.commit()
    refresh_meta(conn, tids | {r[0] for r in conn.execute("SELECT track_id FROM platform_item WHERE id=?", (item["id"],)) if r[0]})
    return result


def _apply_decision(conn, item, action):
    if item["platform"] == "spotify":
        from . import songs
        detail = json.loads(item["match_detail"] or "{}")
        if action == "approve":
            cand = detail.pop("candidate", None)
            if not cand:
                raise ValueError("no stored Spotify candidate to approve")
            tid = songs.attach_spotify(conn, item["track_id"], cand, "auto", detail, config.load()["discovery"]["since"],
                                       db.now_iso())
            conn.execute("UPDATE platform_item SET review_state='approved' WHERE id=?", (item["id"],))
            songs.refresh_song_dates_and_status(conn)
        elif action == "reject":
            conn.execute("INSERT OR IGNORE INTO match_rejection VALUES (?,?,?)", (item["id"], item["track_id"], db.now_iso()))
            state = q(conn, "SELECT review_state FROM platform_item WHERE id=?", (item["id"],))[0]["review_state"]
            if state in ("auto", "approved"):  # undo the metadata that link put on the song
                conn.execute("""UPDATE track SET title=coalesce(yt_title, title), isrc=NULL, release_id=NULL
                                WHERE id=? AND source='youtube'""", (item["track_id"],))
                conn.execute("DELETE FROM track_artist WHERE track_id=? AND (SELECT source FROM track WHERE id=?)='youtube'",
                             (item["track_id"], item["track_id"]))
                conn.execute("UPDATE platform_item SET track_id=NULL WHERE platform='deezer' AND track_id=?", (item["track_id"],))
            conn.execute("UPDATE track SET release_status='not_on_spotify', spotify_checked_at=? WHERE id=?",
                         (db.now_iso(), item["track_id"]))
            conn.execute("UPDATE platform_item SET track_id=NULL, review_state='rejected' WHERE id=?", (item["id"],))
        else:
            raise ValueError("action must be approve or reject")
        conn.commit()
        return {"ok": True}
    if action == "approve":
        if not item["track_id"]:
            raise ValueError("nothing to approve")
        conn.execute("UPDATE platform_item SET review_state='approved' WHERE id=?", (item["id"],))
    elif action == "reject":
        if item["track_id"]:
            conn.execute("INSERT OR IGNORE INTO match_rejection VALUES (?,?,?)", (item["id"], item["track_id"], db.now_iso()))
        conn.execute("UPDATE platform_item SET track_id=NULL, review_state=NULL, match_score=NULL, match_detail=NULL, "
                     "match_method=NULL WHERE id=?", (item["id"],))
    else:
        raise ValueError("action must be approve or reject")
    conn.commit()
    return {"ok": True}


def post_match_link(conn, body):
    """Manually attach a collected YouTube video (URL or id) to a track."""
    ref = (body.get("video") or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", ref) or re.fullmatch(r"([\w-]{11})", ref)
    if not m:
        raise ValueError("paste a YouTube URL or 11-character video id")
    vid = m.group(1)
    item = q(conn, "SELECT id FROM platform_item WHERE platform='youtube' AND platform_id=?", (vid,))
    if not item:
        raise ValueError("that video isn't collected: it isn't from a configured label channel or is outside the window")
    track = q(conn, "SELECT id FROM track WHERE id=?", (int(body["track_id"]),))
    if not track:
        raise ValueError("track not found")
    prev = q(conn, "SELECT track_id, title FROM platform_item WHERE id=?", (item[0]["id"],))[0]
    before = review.snapshot(conn, {track[0]["id"], prev["track_id"]}, [item[0]["id"]])
    song = q(conn, "SELECT coalesce(yt_title, title) t FROM track WHERE id=?", (track[0]["id"],))[0]["t"]
    conn.execute("DELETE FROM match_rejection WHERE platform_item_id=? AND track_id=?", (item[0]["id"], track[0]["id"]))
    conn.execute("UPDATE platform_item SET track_id=?, review_state='approved', match_method='manual', match_score=NULL, "
                 "match_detail=NULL WHERE id=?", (track[0]["id"], item[0]["id"]))
    review.record(conn, REQUEST.actor, "link", "youtube", item[0]["id"], track[0]["id"], f"{song} ↔ youtube: {prev['title']}", before)
    conn.commit()
    return {"ok": True}


def api_match_candidates(conn, qs):
    """Best-scoring collected videos for one track (for manual linking), whatever the thresholds."""
    from . import match
    t = q(conn, """SELECT t.id, t.title, t.duration_ms, r.title release_title, r.release_date FROM track t
                   JOIN release r ON r.id=t.release_id WHERE t.id=?""", (int(qs["track_id"]),))
    if not t:
        raise ValueError("track not found")
    t = t[0]
    t["artists"] = [r["name"] for r in conn.execute("""SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
                                                       WHERE ta.track_id=? ORDER BY ta.position""", (t["id"],))]
    core, _, _ = match.parse_spotify_title(t["title"])
    keys = match.block_keys(core)
    if not keys:
        return {"candidates": []}
    like = " OR ".join(["lower(title) LIKE ?"] * len(keys))
    vids = q(conn, f"""SELECT id, platform_id, title, description, published_at, duration_ms, url, channel, asset_type, track_id,
                              (SELECT value FROM snapshot s WHERE s.platform_item_id=platform_item.id AND metric='views'
                               ORDER BY capture_date DESC LIMIT 1) views
                       FROM platform_item WHERE platform='youtube' AND asset_type NOT IN ('short','jukebox')
                         AND ({like})""", [f"%{k}%" for k in keys])
    out = []
    for v in vids:
        total, detail = match.score(t, {"title": v["title"], "description": v["description"],
                                        "published_at": v["published_at"],
                                        "duration_s": (v["duration_ms"] or 0) / 1000 or None})
        out.append({"item_id": v["id"], "video_id": v["platform_id"], "title": v["title"], "url": v["url"],
                    "channel": v["channel"], "published": (v["published_at"] or "")[:10], "views": v["views"],
                    "asset_type": v["asset_type"], "score": total, "detail": detail,
                    "linked_elsewhere": v["track_id"] not in (None, t["id"])})
    out.sort(key=lambda x: -x["score"])
    return {"candidates": out[:8]}


def api_decisions(conn, qs):
    rows = q(conn, """SELECT id, decided_at, decided_by, action, platform, item_id, track_id, summary, undone_at, undone_by
                      FROM review_decision ORDER BY id DESC LIMIT 200""")
    return {"decisions": rows}


def post_undo(conn, body):
    review.undo(conn, int(body["id"]), REQUEST.actor)
    d = conn.execute("SELECT before FROM review_decision WHERE id=?", (int(body["id"]),)).fetchone()
    refresh_meta(conn, {int(t) for t in json.loads(d["before"])["tracks"]} if d else set())
    return {"ok": True}


META_SORTS = {"songs", "new", "views", "views_delta", "likes", "comments", "videos", "avg_views", "value"}


def _meta_window(qs):
    since, until = config.window(config.load())
    return qs.get("since") or since, qs.get("until") or until


def api_meta_top(conn, qs):
    """Metrics per value of one metadata field (Explore page): songs, new releases, views, gains ..."""
    field = qs.get("field") or "singer"
    if field not in metadata.FIELDS:
        raise ValueError("unknown field")
    since, until = _meta_window(qs)
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?{PERIOD_ONLY}{focus(qs)}"
    args = tracks_args() + [since, until, field]
    extra = ""
    if qs.get("label"):
        extra += " AND m.track_id IN (SELECT track_id FROM song_meta WHERE field='label' AND value=?)"
        args.append(qs["label"])
    if qs.get("language") and field != "language":
        extra += " AND m.track_id IN (SELECT track_id FROM song_meta WHERE field='language' AND value=?)"
        args.append(qs["language"])
    if qs.get("q"):
        extra += " AND m.value LIKE ?"
        args.append(f"%{qs['q']}%")
    sort = qs.get("sort") if qs.get("sort") in META_SORTS else "views"
    order = "ASC" if qs.get("order") == "asc" else "DESC"
    base = f"""SELECT m.value_key key, max(m.value) value, count(DISTINCT s.id) songs, sum(s.release_status='new') new,
                      sum(s.yt_views) views, sum(s.yt_delta) views_delta, sum(s.yt_likes) likes,
                      sum(s.yt_comments) comments, sum(s.yt_videos) videos,
                      round(sum(s.yt_views) * 1.0 / count(DISTINCT s.id)) avg_views
               FROM song_meta m JOIN ({W}) s ON s.id=m.track_id WHERE m.field=?{extra} GROUP BY m.value_key"""
    total = conn.execute(f"SELECT count(*), sum(views) FROM ({base})", args).fetchone()
    lim, off = min(int(qs.get("limit", 50)), 500), int(qs.get("offset", 0))
    rows = q(conn, f"SELECT * FROM ({base}) ORDER BY {sort} {order} NULLS LAST, songs DESC LIMIT ? OFFSET ?", args + [lim, off])
    keys = [r["key"] for r in rows]
    if keys:  # each value's most viewed song
        top = {}
        for r in conn.execute(f"""SELECT m.value_key, s.id, s.title, s.yt_views FROM song_meta m JOIN ({W}) s ON s.id=m.track_id
                                  WHERE m.field=? AND m.value_key IN ({','.join('?' * len(keys))})
                                  ORDER BY s.yt_views DESC NULLS LAST""", tracks_args() + [since, until, field] + keys):
            top.setdefault(r[0], {"id": r[1], "title": r[2], "views": r[3]})
        for r in rows:
            r["top_song"] = top.get(r["key"])
    return {"field": field, "label": metadata.FIELDS[field], "total": total[0], "total_views": total[1],
            "since": since, "until": until, "rows": rows}


def api_meta_detail(conn, qs):
    """One metadata value (an artist, a film, a language ...): KPIs, daily views, its songs, collaborators."""
    field, key = qs.get("field"), qs.get("key")
    if field not in metadata.FIELDS or not key:
        raise ValueError("field and key are required")
    since, until = _meta_window(qs)
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?{PERIOD_ONLY}{focus(qs)}"
    a = tracks_args() + [since, until]
    ids_sql = "SELECT track_id FROM song_meta WHERE field=? AND value_key=?"
    songs_ = tag_labels(q(conn, f"{W} AND id IN ({ids_sql}) ORDER BY yt_views DESC NULLS LAST", a + [field, key]))
    ids = [s_["id"] for s_ in songs_]
    name = conn.execute("SELECT value FROM song_meta WHERE field=? AND value_key=? GROUP BY value ORDER BY count(*) DESC LIMIT 1",
                        (field, key)).fetchone()
    kpi = {"songs": len(songs_), "new": sum(1 for s_ in songs_ if s_["release_status"] == "new")}
    for k in ("yt_views", "yt_delta", "yt_likes", "yt_comments", "yt_videos") + tuple(f"yt_views_{t}" for t in
                                                                                     ("video", "lyric", "audio", "short", "teaser", "other")):
        kpi[k] = sum(s_[k] or 0 for s_ in songs_)
    series, collab = [], []
    if ids:
        ph = ",".join("?" * len(ids))
        # Views gained per day: each video's rise since its previous reading (a video's first reading adds
        # nothing), so videos collected later don't show up as a jump.
        series = q(conn, f"""SELECT day, sum(gain) views, count(*) videos FROM (
                               SELECT s.capture_date day, s.value - lag(s.value) OVER (PARTITION BY s.platform_item_id
                                      ORDER BY s.capture_date) gain
                               FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                               WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song') AND s.metric='views'
                                 AND p.track_id IN ({ph}))
                             WHERE gain IS NOT NULL GROUP BY day ORDER BY day""", ids)
        collab = q(conn, f"""SELECT field, max(value) value, value_key key, count(DISTINCT track_id) songs FROM song_meta
                             WHERE track_id IN ({ph}) AND NOT (field=? AND value_key=?)
                               AND field IN ('singer','music_director','lyricist','cast','film','language','category','label','occasion')
                             GROUP BY field, value_key ORDER BY songs DESC""", ids + [field, key])
    by_field = {}
    for c in collab:
        if len(by_field.setdefault(c["field"], [])) < 8:
            by_field[c["field"]].append(c)
    return {"field": field, "field_label": metadata.FIELDS[field], "key": key, "value": name[0] if name else key,
            "since": since, "until": until, "kpi": kpi, "series": series, "songs": songs_, "collaborators": by_field}


def api_insights(conn, qs):
    """Rule-based insight panels (no AI): emerging, cross_label, catalog, artists, anomalies."""
    return insights.panel(conn, qs.get("panel") or "emerging", field=qs.get("field"))


def api_insights_song(conn, qs):
    facts, meta = insights.song_facts(conn)
    return {"benchmark": insights.benchmark(facts, int(qs["id"])), "history": meta["history_note"]}


def api_ai_status(conn, _):
    return ai.status()


def api_ai_assist(conn, _):
    """The written AI-assist briefing (data/ai_assist.json). Live Claude calls are not enabled yet."""
    return insights.ai_assist() or {}


def post_ai_explain(conn, body):
    return ai.explain(conn, body.get("panel") or "emerging", field=body.get("field"), refresh=bool(body.get("refresh")))


def post_ai_ask(conn, body):
    q = (body.get("question") or "").strip()
    if not q:
        raise ValueError("ask a question")
    return ai.ask(conn, q[:2000], history=body.get("history") or [])


def api_coverage(conn, _):
    """How much of each kind of data the window's songs have (Sources -> Data coverage & gaps)."""
    since, until = config.window(config.load())
    W = "t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?"
    n = conn.execute(f"SELECT count(*) FROM track t WHERE {W}", (since, until)).fetchone()[0]

    def songs_with(cond):
        return conn.execute(f"SELECT count(*) FROM track t WHERE {W} AND {cond}", (since, until)).fetchone()[0]

    def meta(field):
        return songs_with(f"EXISTS (SELECT 1 FROM song_meta m WHERE m.track_id=t.id AND m.field='{field}')")

    def item(platform, extra=""):
        return songs_with(f"EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='{platform}' {extra})")
    rows = [
        ("YouTube views", "youtube", songs_with("EXISTS (SELECT 1 FROM platform_item p JOIN snapshot s ON s.platform_item_id=p.id "
                                                 "WHERE p.track_id=t.id AND p.platform='youtube' AND s.metric='views')")),
        ("Spotify match (metadata)", "spotify", item("spotify", "AND p.review_state IN ('auto','approved')")),
        ("Spotify lookup still pending", "spotify", songs_with("t.release_id IS NULL AND coalesce(t.release_status,'') NOT IN ('not_on_spotify','pending')")),
        ("Not on Spotify (searched)", "spotify", songs_with("t.release_status='not_on_spotify'")),
        ("Apple Music metadata", "apple", item("apple")),
        ("ISRC", "spotify / musicbrainz", songs_with("t.isrc IS NOT NULL")),
        ("Deezer rank", "deezer", item("deezer")),
        ("Release date", "spotify / apple", songs_with("(t.release_id IS NOT NULL OR EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='apple'))")),
        ("Singers", "credits", meta("singer")), ("Music director", "credits", meta("music_director")),
        ("Lyricist", "credits", meta("lyricist")), ("Film", "credits / spotify / apple", meta("film")),
        ("Cast", "credits", meta("cast")), ("Language", "rules", meta("language")), ("Genre", "apple", meta("genre")),
    ]
    vids = conn.execute(f"""SELECT count(*) total,
            sum(NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='likes')) no_likes,
            sum(NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='comments')) no_comments,
            sum(p.audio_language IS NULL OR p.audio_language='') no_lang
        FROM platform_item p JOIN track t ON t.id=p.track_id
        WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song') AND {W}""", (since, until)).fetchone()
    days = conn.execute("SELECT count(DISTINCT capture_date) FROM snapshot WHERE metric='views'").fetchone()[0]
    return {"since": since, "until": until, "songs": n,
            "rows": [{"item": a, "source": b, "songs": c, "pct": round(100 * c / n) if n else 0} for a, b, c in rows],
            "videos": dict(vids), "days_of_readings": days}


def api_merge_suggestions(conn, qs):
    cfg = config.load()
    groups = merge.suggestions(conn, cfg)
    counts = {}
    for g in groups:
        c = counts.setdefault(g["rule"], {"groups": 0, "songs": 0})
        c["groups"] += 1
        c["songs"] += len(g["songs"])
    rules = [dict(cfg["merge"]["rules"].get(r, {}), id=r, description=d, **counts.get(r, {"groups": 0, "songs": 0}))
             for r, d in merge.RULES.items()]
    if qs.get("rule"):
        groups = [g for g in groups if g["rule"] == qs["rule"]]
    if qs.get("q"):
        ql = qs["q"].lower()
        groups = [g for g in groups if any(ql in ((s["yt_title"] or "") + " " + (s["title"] or "")).lower() for s in g["songs"])]
    off, lim = int(qs.get("offset", 0)), min(int(qs.get("limit", 50)), 500)
    return {"rules": rules, "total": len(groups), "groups": groups[off:off + lim]}


def post_merge_apply(conn, body):
    """Merge chosen groups ({groups:[{keep, drop}]}), every group of one rule ({rule}), or hand-picked
    songs ({song_ids}, keep = the best one by the same preference as the rules)."""
    actor = REQUEST.actor
    if body.get("song_ids"):
        ids = [int(i) for i in body["song_ids"]]
        if len(ids) < 2:
            raise ValueError("pick at least two songs")
        rows = [r for r in merge._songs(conn, config.load()) if r["id"] in ids]
        missing = set(ids) - {r["id"] for r in rows}
        if missing:  # outside the window: still mergeable
            rows += [{"id": i, "release_id": conn.execute("SELECT release_id FROM track WHERE id=?", (i,)).fetchone()[0],
                      "videos": 0, "views": 0} for i in missing]
        keep = int(body["keep"]) if body.get("keep") else merge._keep(rows)["id"]
        n = merge.apply(conn, [{"keep": keep, "drop": [i for i in ids if i != keep]}], actor, "Merged selected songs")
        refresh_meta(conn, {keep})
        return {"merged": n, "keep": keep}
    if body.get("rule"):
        groups = merge.suggestions(conn, config.load(), only_rule=body["rule"])
        groups = [{"keep": g["keep"], "drop": [s["id"] for s in g["songs"] if s["id"] != g["keep"]]} for g in groups]
        summary = f"Merged all suggestions of rule {body['rule']}"
    else:
        groups = [{"keep": int(g["keep"]), "drop": [int(d) for d in g["drop"]]} for g in body.get("groups", [])]
        summary = "Merged chosen duplicate groups"
    n = merge.apply(conn, groups, actor, summary)
    refresh_meta(conn, {g["keep"] for g in groups})
    return {"merged": n, "groups": len(groups)}


def post_merge_reject(conn, body):
    ids = [int(i) for i in body.get("song_ids", [])]
    if len(ids) < 2:
        raise ValueError("need two or more songs")
    merge.reject(conn, merge.keys_of(conn, ids))
    return {"ok": True}


def post_youtube_label(conn, body):
    """Add a label, switch one on/off, or remove one (with its channels; collected data is kept)."""
    cfg = config.load()
    y = cfg["youtube"]
    name = re.sub(r"\s+", " ", (body.get("name") or "")).strip()
    if not name:
        raise ValueError("label name is empty")
    found = next((lb for lb in y["labels"] if lb["name"].lower() == name.lower()), None)
    if body.get("action") == "add":
        if found:
            raise ValueError("that label is already in the list")
        y["labels"].append({"name": name, "enabled": True})
    elif not found:
        raise ValueError("label not found")
    elif body.get("action") == "toggle":
        found["enabled"] = bool(body.get("enabled"))
    elif body.get("action") == "remove":
        y["labels"] = [lb for lb in y["labels"] if lb is not found]
        y["channels"] = [c for c in y["channels"] if c["label"] != found["name"]]
    else:
        raise ValueError("unknown action")
    return {"config": config.save(cfg)}


def post_youtube_channel(conn, body):
    """Add (by handle, URL or channel id) to a label, or remove a YouTube channel."""
    from .youtube import YouTube, YouTubeError
    cfg = config.load()
    chans = cfg["youtube"]["channels"]
    if body.get("remove"):
        cfg["youtube"]["channels"] = [c for c in chans if c["id"] != body["remove"]]
        return {"config": config.save(cfg)}
    label = body.get("label") or config.FIRST_LABEL
    if label not in [lb["name"] for lb in cfg["youtube"]["labels"]]:
        raise ValueError("unknown label")
    ref = (body.get("channel") or "").strip()
    key = config.read_secrets().get("YOUTUBE_API_KEY")
    if not key:
        raise ValueError("save a YouTube API key first")
    yt = YouTube(key)
    m = re.search(r"(UC[\w-]{22})", ref)
    h = re.search(r"@([\w.-]+)", ref)
    try:
        items = yt.channels(ids=[m.group(1)]) if m else yt.channels(handle="@" + (h.group(1) if h else ref.lstrip("@")))
    except YouTubeError as e:
        raise ValueError(str(e))
    if not items:
        raise ValueError("channel not found")
    c = items[0]
    have = next((x for x in chans if x["id"] == c["id"]), None)
    if have:
        raise ValueError(f"already in the list (under {have['label']})")
    chans.append({"id": c["id"], "title": c["snippet"]["title"], "label": label})
    return {"config": config.save(cfg), "added": {"title": c["snippet"]["title"],
            "subscribers": c["statistics"].get("subscriberCount"), "videos": c["statistics"].get("videoCount")}}


def api_youtube_status(conn, _):
    last = q(conn, "SELECT id, started_at, status, stats FROM run_log WHERE job IN ('youtube_sync','pipeline') "
                   "ORDER BY id DESC LIMIT 1")
    from .youtube import DAILY_UNITS, key_status, next_reset, parse_keys
    keys = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
    # The quota day starts at midnight Pacific time; run_log times are UTC ISO strings.
    day_start = datetime.fromtimestamp(next_reset() - 86400, timezone.utc).isoformat(timespec="seconds")
    units, by_key = 0, {}
    for r in conn.execute("SELECT stats FROM run_log WHERE started_at >= ?", (day_start,)):  # every job using YouTube: sync and YouTube Music
        st = json.loads(r["stats"] or "{}")
        units += (st.get("quota_units") or 0) + (st.get("ytmusic_quota_units") or 0)
        for h, n in (st.get("quota_units_by_key") or {}).items():
            by_key[h] = by_key.get(h, 0) + n
    if last:
        last[0]["stats"] = {k: v for k, v in json.loads(last[0]["stats"] or "{}").items() if k != "log_tail"}
    ks = key_status(keys)
    for k in ks:
        k["units_today"] = by_key.get(k["hint"], 0)
    return {"quota_day_start": day_start, "quota_resets": datetime.fromtimestamp(next_reset(), timezone.utc).isoformat(timespec="minutes"),
            "last_run": last[0] if last else None, "units_today": units,
            "units_limit": DAILY_UNITS * max(len(keys), 1), "keys": ks,
            "channels": config.load()["youtube"]["channels"]}


# ---------- Search: one box, every detail of a song ----------
LINK_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/track/|/song/[^/?#]+/|/album/[^/?#]+/|/watch/|\?i=)([A-Za-z0-9_-]{5,})")


def api_search(conn, qs):
    """Songs matching `q` in any detail: name, any credit or metadata value (singer, music director, lyricist, film,
    cast, language, label, channel, album, genre...), ISRC, song number, or a platform link / ID (YouTube, Spotify,
    JioSaavn, Apple, Deezer). Older songs hidden unless focus=all, as everywhere. Best matches first, then the biggest."""
    raw = (qs.get("q") or "").strip()
    if len(raw) < 2:
        return {"q": raw, "total": 0, "results": []}
    since, until = config.window(config.load())
    W = f"t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?{focus(qs)}"
    low, hits = raw.lower(), {}

    def add(rows, how):
        for r in rows:
            tid, rank, field, value = r
            if tid not in hits or rank < hits[tid][0]:
                hits[tid] = (rank, how(field), value)
    ids = set(LINK_ID.findall(raw)) | {raw}
    marks = ",".join("?" * len(ids))
    add(conn.execute(f"""SELECT t.id, 0, p.platform, p.platform_id FROM platform_item p JOIN track t ON t.id=p.track_id
                         WHERE (p.platform_id IN ({marks}) OR p.url=?) AND {W} LIMIT 50""", (*ids, raw, since, until)),
        lambda f: f"{PLATFORM_NAME.get(f, f)} link")
    add(conn.execute(f"SELECT t.id, 0, 'isrc', t.isrc FROM track t WHERE upper(t.isrc)=upper(?) AND {W}", (raw, since, until)), lambda f: "ISRC")
    if raw.lstrip("#").isdigit():
        add(conn.execute(f"SELECT t.id, 0, 'id', t.id FROM track t WHERE t.id=? AND {W}", (int(raw.lstrip("#")), since, until)), lambda f: "Song no.")
    add(conn.execute(f"""SELECT t.id, CASE WHEN lower(coalesce(t.yt_title, t.title))=? THEN 1 WHEN lower(coalesce(t.yt_title, t.title)) LIKE ? THEN 2 ELSE 3 END,
                                'song', coalesce(t.yt_title, t.title) FROM track t
                         WHERE (lower(coalesce(t.yt_title, t.title)) LIKE ? OR lower(t.title) LIKE ?) AND {W} LIMIT 500""",
                     (low, low + "%", f"%{low}%", f"%{low}%", since, until)), lambda f: "Song")
    add(conn.execute(f"""SELECT t.id, CASE WHEN lower(m.value)=? THEN 2 WHEN lower(m.value) LIKE ? THEN 3 ELSE 4 END, m.field, m.value
                         FROM song_meta m JOIN track t ON t.id=m.track_id WHERE lower(m.value) LIKE ? AND {W} LIMIT 3000""",
                     (low, low + "%", f"%{low}%", since, until)), lambda f: META_NAME.get(f, f.replace("_", " ").capitalize()))
    if not hits:
        return {"q": raw, "total": 0, "results": []}
    song_daily.ensure(conn)
    tids = list(hits)
    stats = {}
    for i in range(0, len(tids), 500):
        part = tids[i:i + 500]
        for r in conn.execute(f"""SELECT song_id, yt_views, sp_plays, js_plays, ytm_plays FROM song_daily d
                                  WHERE song_id IN ({",".join("?" * len(part))}) AND day=(SELECT max(day) FROM song_daily x WHERE x.song_id=d.song_id)""", part):
            stats[r[0]] = dict(r)
    order = sorted(tids, key=lambda t: (hits[t][0], -((stats.get(t) or {}).get("yt_views") or 0)))
    top = order[:int(qs.get("limit") or 30)]
    rows = {r["id"]: r for r in q(conn, f"""SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) first_upload, t.release_status,
                 (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) yt_channel,
                 (SELECT platform_id FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' AND p.review_state IN ('auto','approved','song') ORDER BY published_at LIMIT 1) yt_video_id
               FROM track t WHERE t.id IN ({",".join("?" * len(top))})""", top)}
    out = []
    for t in top:
        r = rows[t]
        r.update(stats.get(t) or {}, matched_on=hits[t][1], matched=str(hits[t][2]))
        out.append(r)
    by_field = {}
    for rank, how, _ in hits.values():
        by_field[how] = by_field.get(how, 0) + 1
    return {"q": raw, "total": len(hits), "by_field": dict(sorted(by_field.items(), key=lambda x: -x[1])), "results": tag_labels(out)}


PLATFORM_NAME = {"youtube": "YouTube", "ytmusic": "YouTube Music", "spotify": "Spotify", "jiosaavn": "JioSaavn", "apple": "Apple Music",
                 "deezer": "Deezer", "musicbrainz": "MusicBrainz"}
META_NAME = {"singer": "Singer", "music_director": "Music director", "lyricist": "Lyricist", "film": "Film", "cast": "Cast", "language": "Language",
             "category": "Category", "label": "Label", "music_label": "Label", "channel": "Channel", "album": "Album", "spotify_album": "Spotify album",
             "apple_album": "Apple album", "spotify_artist": "Spotify artist", "apple_artist": "Apple artist", "genre": "Genre", "director": "Director",
             "producer": "Producer", "song_title": "Song", "occasion": "Occasion"}


# ---------- Dashboard (client home): reach and growth of the period's songs, in plain terms ----------
REACH = (("youtube", "YouTube views", "yt_views"), ("ytmusic", "YouTube Music plays", "ytm_plays"),
         ("spotify", "Spotify plays", "sp_plays"), ("jiosaavn", "JioSaavn plays", "js_plays"))


def _song_set(qs):
    since, until = config.window(config.load())
    return since, until, (f"SELECT id FROM track WHERE source='youtube' AND first_published >= ? AND old_release IS NULL "
                          f"AND substr(first_published,1,10) <= ?{focus(qs)}")


AUDIO = ("ytm_plays", "sp_plays", "js_plays")  # audio streams: kept apart from YouTube views (never one "reach" number)


def api_client(conn, qs):
    """Dashboard: each song's total and day-on-day gain per platform on the latest completed reporting day (the last
    finished server day: today is still being read), YouTube views and audio streams kept apart; totals, top songs,
    risers, labels and a 30-day trend (YouTube on its own axis)."""
    since, until, S = _song_set(qs)
    song_daily.ensure(conn)
    today = date.today().isoformat()
    last = conn.execute(f"SELECT max(day) FROM song_daily WHERE day < ? AND song_id IN ({S})", (today, since, until)).fetchone()[0] \
        or conn.execute(f"SELECT max(day) FROM song_daily WHERE song_id IN ({S})", (since, until)).fetchone()[0]
    songs = {r["id"]: dict(r, **{f: None for _, _, c in REACH for f in (c, c + "_gain")}) for r in conn.execute(
        f"""SELECT t.id, coalesce(t.yt_title, t.title) title, t.release_status, substr(t.first_published,1,10) first_upload,
                   (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) channel,
                   (SELECT platform_id FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
                      AND p.review_state IN ('auto','approved','song') ORDER BY published_at LIMIT 1) yt_video_id
            FROM track t WHERE t.id IN ({S})""", (since, until))}
    cols = [c for _, _, c in REACH]
    if last:
        # totals: each song's value on the reporting day, or its newest of the 3 days before (a platform not read that day);
        # gains: the reporting day's own gain only
        for r in conn.execute(f"""SELECT song_id, day, {', '.join(cols + [c + '_gain' for c in cols])} FROM song_daily
                                  WHERE day > date(?, '-3 day') AND day <= ? AND song_id IN ({S}) ORDER BY day DESC""", (last, last, since, until)):
            s = songs.get(r["song_id"])
            if not s:
                continue
            for c in cols:
                if s[c] is None and r[c] is not None:
                    s[c] = r[c]
                if r["day"] == last and r[c + "_gain"] is not None:
                    s[c + "_gain"] = r[c + "_gain"]
    labels_of = config.channel_labels(config.load())
    on = {r[0]: r[1] for r in conn.execute(f"""SELECT platform, count(DISTINCT track_id) FROM platform_item
                                               WHERE track_id IN ({S}) AND review_state IN ('auto','approved','song')
                                               GROUP BY platform""", (since, until))}
    labels, rows = {}, list(songs.values())
    for s in rows:
        s["label"] = labels_of.get(s["channel"], config.FIRST_LABEL) if s["channel"] else ""
        s["audio"] = sum(s[c] or 0 for c in AUDIO)
        s["audio_gain"] = sum(s[c + "_gain"] or 0 for c in AUDIO)
        s["yt_gain"] = s["yt_views_gain"] or 0
        s["hit"] = (s["yt_views"] or 0) >= 1_000_000 or s["audio"] >= 1_000_000
        t = labels.setdefault(s["label"] or "Other", {"label": s["label"] or "Other", "songs": 0, "new": 0, "yt_views": 0, "audio": 0,
                                                      "yt_gain": 0, "audio_gain": 0, "hits": 0})
        t["songs"] += 1
        t["new"] += s["release_status"] == "new"
        t["yt_views"] += s["yt_views"] or 0
        t["audio"] += s["audio"]
        t["yt_gain"] += s["yt_gain"]
        t["audio_gain"] += s["audio_gain"]
        t["hits"] += s["hit"]
    total = {c: sum(s[c] or 0 for s in rows) for c in cols}
    gain = {c: sum(s[c + "_gain"] or 0 for s in rows) for c in cols}
    keep = ("id", "title", "label", "first_upload", "release_status", "yt_video_id", "audio", "audio_gain", "yt_gain") + tuple(cols)
    pick = lambda xs: [{k: s[k] for k in keep} for s in xs]
    trend = []
    if last:
        days = q(conn, f"""SELECT day, {', '.join(f'sum({c}_gain) {c}' for c in cols)} FROM song_daily
                           WHERE day > date(?, '-30 day') AND day <= ? AND song_id IN ({S}) GROUP BY day ORDER BY day""", (last, last, since, until))
        trend = [{"platform": p, "name": n, "axis": "left" if p == "youtube" else "right",
                  "points": [{"x": d["day"], "y": d[c]} for d in days if d[c] is not None]} for p, n, c in REACH]
    from .market_share import data_started
    return {"since": since, "until": until, "as_of": last, "today": today, "songs": len(rows), "started": data_started(conn),
            "new": sum(s["release_status"] == "new" for s in rows), "hits": sum(s["hit"] for s in rows),
            "hits_yt": sum((s["yt_views"] or 0) >= 1_000_000 for s in rows), "hits_audio": sum(s["audio"] >= 1_000_000 for s in rows),
            "yt_views": total["yt_views"], "yt_gain": gain["yt_views"],
            "audio": sum(total[c] for c in AUDIO), "audio_gain": sum(gain[c] for c in AUDIO),
            "growing": sum(s["yt_gain"] > 0 or s["audio_gain"] > 0 for s in rows),
            "platforms": [{"platform": p, "name": n, "total": total[c], "gain": gain[c], "songs": on.get(p, 0)} for p, n, c in REACH]
                         + [{"platform": p, "name": n, "songs": on.get(p, 0)} for p, n in (("apple", "Apple Music"), ("deezer", "Deezer"))],
            "top": pick(sorted(rows, key=lambda s: -(s["yt_views"] or 0))[:15]),
            "top_audio": pick(sorted((s for s in rows if s["audio"]), key=lambda s: -s["audio"])[:15]),
            "risers": pick(sorted((s for s in rows if s["yt_gain"] > 0), key=lambda s: -s["yt_gain"])[:10]),
            "risers_audio": pick(sorted((s for s in rows if s["audio_gain"] > 0), key=lambda s: -s["audio_gain"])[:10]),
            "labels": sorted(labels.values(), key=lambda t: -t["yt_views"]), "trend": trend, "shorts": shorts_summary(conn, S, (since, until), last),
            "reels": shorts_summary(conn, S, (since, until), last, "ig_reels")}


def shorts_summary(conn, S, args, last, col="yt_shorts"):
    """Shorts (or Instagram Reels) made with the songs' official audio: total, gained, and the most-used songs (kept
    apart from reach, which adds plays)."""
    if not last:
        return None
    rows = q(conn, f"""SELECT d.song_id id, coalesce(t.yt_title, t.title) title, d.{col} shorts, d.{col}_gain gain
                       FROM song_daily d JOIN track t ON t.id=d.song_id
                       WHERE d.day=(SELECT max(day) FROM song_daily x WHERE x.song_id=d.song_id AND x.{col} IS NOT NULL AND x.day > date(?, '-3 day'))
                         AND d.song_id IN ({S}) AND d.{col} IS NOT NULL""", (last, *args))
    if not rows:
        return None
    return {"total": sum(r["shorts"] for r in rows), "gain": sum(r["gain"] or 0 for r in rows), "songs": sum(r["shorts"] > 0 for r in rows),
            "read": len(rows), "top": sorted((r for r in rows if r["shorts"]), key=lambda r: -r["shorts"])[:10]}


# ---------- Shorts made with each song's official audio (label-owned sounds) ----------
SHORTS_PERIODS = {"day": None, "week": "song_weekly", "month": "song_monthly", "year": "song_yearly"}


def api_shorts(conn, qs):
    """?song=ID: the song's official sounds (latest count each), its Shorts by day / week / month / year and the most-viewed
    Shorts seen. Without a song: songs ranked by Shorts made, for the chosen period."""
    song_daily.ensure(conn)
    period = qs.get("period") if qs.get("period") in SHORTS_PERIODS else "day"
    view = SHORTS_PERIODS[period]
    if qs.get("song"):
        sid = int(qs["song"])
        sounds = q(conn, """SELECT u.platform_item_id, u.platform, p.platform_id video_id, p.asset_type, p.title upload, u.sound_title, u.sound_owner,
                                   u.shorts, u.count_text, u.official, u.listed, u.listed_views, u.day
                            FROM sound_use u JOIN platform_item p ON p.id=u.platform_item_id
                            WHERE p.track_id=? AND u.day=(SELECT max(day) FROM sound_use x WHERE x.platform_item_id=u.platform_item_id)
                            ORDER BY u.platform DESC, u.official DESC, u.shorts DESC""", (sid,))
        # Instagram audios linked but not read yet (they are read by the browser job)
        sounds += q(conn, """SELECT id platform_item_id, 'instagram' platform, platform_id video_id, NULL asset_type, title upload, NULL sound_title,
                                    NULL sound_owner, NULL shorts, NULL count_text, NULL official, NULL listed, NULL listed_views, NULL day
                             FROM platform_item p WHERE platform='instagram' AND track_id=? AND review_state IN ('auto','approved')
                               AND NOT EXISTS (SELECT 1 FROM sound_use u WHERE u.platform_item_id=p.id)""", (sid,))
        if view:
            series = q(conn, f"""SELECT period, last_day, yt_shorts total, yt_shorts_gain gain, ig_reels reels, ig_reels_gain reels_gain FROM {view}
                                 WHERE song_id=? AND coalesce(yt_shorts, ig_reels) IS NOT NULL ORDER BY period DESC""", (sid,))
        else:
            series = q(conn, """SELECT day period, day last_day, yt_shorts total, yt_shorts_gain gain, ig_reels reels, ig_reels_gain reels_gain FROM song_daily
                                WHERE song_id=? AND coalesce(yt_shorts, ig_reels) IS NOT NULL ORDER BY day DESC""", (sid,))
        top = q(conn, """SELECT s.video_id, coalesce(s.platform, 'youtube') platform, s.title, s.views, s.likes, s.comments, s.first_seen, s.last_seen
                         FROM sound_short s JOIN sound_use u ON u.platform_item_id=s.platform_item_id AND u.official=1
                         WHERE s.song_id=? GROUP BY s.video_id ORDER BY s.views DESC LIMIT 40""", (sid,))
        return {"song": sid, "period": period, "sounds": sounds, "series": series, "top": top}
    since, until = config.window(config.load())
    src = view or "song_daily"
    per = "period" if view else "day"
    latest = conn.execute(f"SELECT max({per}) FROM {src} WHERE yt_shorts IS NOT NULL").fetchone()[0]
    rows = tag_labels(q(conn, f"""SELECT d.song_id id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) song_date, d.yt_shorts shorts,
                                      d.yt_shorts_gain gain,
                                      (SELECT channel FROM platform_item x WHERE x.track_id=t.id AND x.platform='youtube' ORDER BY published_at LIMIT 1) yt_channel
                                  FROM {src} d JOIN track t ON t.id=d.song_id
                                  WHERE d.{per}=? AND d.yt_shorts IS NOT NULL AND t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?
                                  ORDER BY d.yt_shorts DESC LIMIT ?""", (latest, since, until, int(qs.get("limit") or 50))))
    return {"period": period, "latest": latest, "rows": rows}


# ---------- Crawl stats (Developer): runs, items read and failures by day, week, month or year ----------
CRAWL_PERIODS = {"day": ("day", 30), "week": ("strftime('%Y-W%W', day)", 16), "month": ("substr(day,1,7)", 12), "year": ("substr(day,1,4)", 5)}
CRAWL_NAMES = {"youtube": "YouTube", "ytmusic": "YouTube Music", "spotify_plays": "Spotify plays (pages)", "spotify_search": "Spotify search (API)",
               "spotify_playlists": "Spotify playlists + film albums", "spotify_web": "Spotify links: web search", "spotify_mb_links": "Spotify links: MusicBrainz", "description_links": "Links in label descriptions",
               "odesli": "Odesli", "jiosaavn": "JioSaavn", "deezer": "Deezer", "apple": "Apple Music (iTunes)",
               "musicbrainz": "MusicBrainz", "youtube_shorts": "YouTube Shorts (sound pages)", "shorts": "Shorts job", "instagram": "Instagram Reels (audio pages)", "songs": "Songs job", "daily": "Daily totals", "spotify": "Spotify label crawl"}


def api_crawl(conn, qs):
    crawl_stats.sync(conn)
    period = qs.get("period") if qs.get("period") in CRAWL_PERIODS else "day"
    expr, keep = CRAWL_PERIODS[period]
    src = qs.get("source") or ""
    group = "api" if qs.get("group") == "api" else "crawl"  # two pages: APIs, and crawling (public pages, web search, internal jobs)
    is_api = lambda s: crawl_stats.METRICS.get(s, {}).get("access") in crawl_stats.API_ACCESS
    scope = [r[0] for r in conn.execute("SELECT DISTINCT source FROM crawl_stat") if is_api(r[0]) == (group == "api")]
    in_scope = "source IN (" + ",".join("'" + s.replace("'", "") + "'" for s in scope or ["-"]) + ")"
    where, args = (" AND source=?", [src]) if src and src in scope else (f" AND {in_scope}", [])
    if src not in scope:
        src = ""
    buckets = [r[0] for r in conn.execute(f"SELECT DISTINCT {expr} b FROM crawl_stat ORDER BY b DESC LIMIT ?", (keep,))]
    if not buckets:
        return {"period": period, "buckets": [], "sources": [], "rows": [], "failures": [], "kinds": [], "kpi": {}, "series": [], "coverage": [], "names": CRAWL_NAMES}
    marks = ",".join("?" * len(buckets))
    in_b = f"{expr} IN ({marks})"
    probs = "('failed','partial','quota_exceeded','interrupted')"
    # one row per period
    rows = q(conn, f"""SELECT {expr} bucket, count(DISTINCT run_id) runs,
                              count(DISTINCT CASE WHEN is_step=1 AND status='ok' THEN run_id END) ok_runs,
                              sum(is_step=1 AND status IN {probs}) problems, sum(is_step=1 AND status='cancelled') cancelled,
                              sum(items_read) items_read, sum(found) found, sum(missing) missing, sum(quota_units) quota_units, sum(api_calls) api_calls,
                              round(sum(coalesce(seconds, 0)) / 3600.0, 1) busy_hours
                       FROM crawl_stat WHERE {in_b}{where} GROUP BY 1 ORDER BY 1 DESC""", (*buckets, *args))
    # time spent working, from the whole run (older multi-step runs have no per-step timing)
    busy = {b: h for b, h in conn.execute(f"""SELECT {expr}, round(sum((julianday(finished_at) - julianday(started_at)) * 24), 1) FROM
        (SELECT substr(started_at,1,10) day, started_at, finished_at FROM run_log WHERE finished_at IS NOT NULL) WHERE {in_b} GROUP BY 1""", buckets)}
    for r in rows:
        r["busy_hours"] = busy.get(r["bucket"], 0)
    # one row per source over the whole selection
    sources = q(conn, f"""SELECT source, count(DISTINCT run_id) runs, sum(status='ok') ok, sum(status IN {probs}) problems,
                                 sum(items_read) items_read, sum(found) found, sum(missing) missing, sum(quota_units) quota_units, sum(api_calls) api_calls,
                                 round(avg(seconds)) avg_seconds, max(CASE WHEN status='ok' THEN started_at END) last_ok,
                                 max(CASE WHEN status IN {probs} THEN started_at END) last_problem
                          FROM crawl_stat WHERE {in_b} AND {in_scope} GROUP BY 1 ORDER BY items_read DESC""", buckets)
    for r in sources:
        r["name"] = CRAWL_NAMES.get(r["source"], r["source"])
        r["access"] = crawl_stats.METRICS.get(r["source"], {}).get("access", "Internal")
        r["yield"] = round(100 * (r["found"] or 0) / r["items_read"], 1) if r["items_read"] else None  # found per item tried
        r["success"] = round(100 * r["ok"] / max(r["ok"] + r["problems"], 1), 1)
    # chart series, oldest first
    order = sorted(buckets)
    by = {}
    for b, s, n in conn.execute(f"SELECT {expr}, source, sum(items_read) FROM crawl_stat WHERE {in_b} AND {in_scope} AND source NOT IN ('daily','songs') GROUP BY 1, 2", buckets):
        by.setdefault(s, {})[b] = n
    series = [{"source": s, "name": CRAWL_NAMES.get(s, s), "points": [{"x": b, "y": v.get(b, 0)} for b in order]} for s, v in by.items() if any(v.values())]
    health = {r["bucket"]: r for r in rows}
    # readings captured, % of tracked items, per platform and period
    coverage = q(conn, f"""SELECT {expr} bucket, platform, sum(readings) readings, sum(items) items FROM crawl_readings
                           WHERE {in_b} GROUP BY 1, 2 ORDER BY 1 DESC""", buckets)
    for r in coverage:
        r["pct"] = min(100.0, round(100 * r["readings"] / r["items"], 1)) if r["items"] else None
    # failures: groups by type, then the latest list
    fw = f"is_step=1 AND status != 'ok' AND {in_b}{where}"
    kinds = q(conn, f"""SELECT coalesce(kind, status) kind, count(*) n, max(started_at) last_seen, count(DISTINCT source) sources,
                               (SELECT message FROM crawl_stat x WHERE coalesce(x.kind, x.status)=coalesce(crawl_stat.kind, crawl_stat.status) AND x.message != '' ORDER BY x.started_at DESC LIMIT 1) sample
                        FROM crawl_stat WHERE {fw} GROUP BY 1 ORDER BY n DESC""", (*buckets, *args))
    fk = qs.get("kind") or ""
    failures = q(conn, f"""SELECT run_id, job, source, started_at, status, coalesce(kind, status) kind, message, seconds FROM crawl_stat
                           WHERE {fw}{" AND coalesce(kind, status)=?" if fk else ""} ORDER BY started_at DESC LIMIT 200""",
                 (*buckets, *args, *([fk] if fk else [])))
    for f in failures:
        f["name"] = CRAWL_NAMES.get(f["source"], f["source"])
    # API health checks (e.g. Spotify blocked) over the same days
    first = conn.execute(f"SELECT min(day) FROM crawl_stat WHERE {in_b}", buckets).fetchone()[0]
    health_checks = q(conn, """SELECT source, count(*) checks, sum(ok=0) blocked, max(CASE WHEN ok=0 THEN checked_at END) last_blocked,
                                      (SELECT ok FROM api_check x WHERE x.source=a.source ORDER BY id DESC LIMIT 1) ok_now,
                                      (SELECT retry_at FROM api_check x WHERE x.source=a.source ORDER BY id DESC LIMIT 1) retry_at,
                                      (SELECT message FROM api_check x WHERE x.source=a.source AND x.ok=0 ORDER BY id DESC LIMIT 1) message
                               FROM api_check a WHERE substr(checked_at,1,10) >= ? GROUP BY source""", (first,))
    t = {k: sum(r[k] or 0 for r in rows) for k in ("runs", "ok_runs", "problems", "cancelled", "items_read", "found", "missing", "quota_units", "api_calls", "busy_hours")}
    t["success"] = round(100 * (1 - t["problems"] / max(t["problems"] + t["ok_runs"], 1)), 1)
    keys = None
    if group == "api":  # keys and quota left today
        from .youtube import DAILY_UNITS, key_status, next_reset, parse_keys
        yk = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
        start = datetime.fromtimestamp(next_reset() - 86400, timezone.utc).isoformat(timespec="seconds")
        used = conn.execute("SELECT coalesce(sum(quota_units), 0) FROM crawl_stat WHERE started_at >= ?", (start,)).fetchone()[0]
        keys = {"units_today": used, "units_limit": DAILY_UNITS * max(len(yk), 1), "keys": key_status(yk),
                "resets": datetime.fromtimestamp(next_reset(), timezone.utc).isoformat(timespec="minutes"),
                "spotify_searches_today": conn.execute("SELECT coalesce(sum(api_calls), 0) FROM crawl_stat WHERE source='spotify_search' AND day=?", (date.today().isoformat(),)).fetchone()[0]}
    return {"period": period, "group": group, "keys": keys, "source": src, "buckets": buckets, "kpi": t, "rows": rows, "sources": sources, "series": series, "order": order, "coverage": coverage,
            "kinds": kinds, "failures": failures, "names": CRAWL_NAMES, "health": health_checks,
            "all_sources": sorted(scope)}


# ---------- Developer dashboard: is the system healthy and on target? ----------
ID_PLATFORMS = (("spotify", "Spotify"), ("ytmusic", "YouTube Music"), ("jiosaavn", "JioSaavn"), ("apple", "Apple Music"),
                ("deezer", "Deezer"), ("musicbrainz", "MusicBrainz"), ("instagram", "Instagram audio"))


def song_ids(conn, qs, today):
    """Developer dashboard: per platform, how many of the period's songs have an ID there, how many were found on each of
    the last 7 days and by which route today, and how many are still without one."""
    since, until, S = _song_set(qs)
    args = (since, until)
    songs = conn.execute(f"SELECT count(*) FROM ({S})", args).fetchone()[0]
    ok = "p.review_state IN ('auto','approved') AND p.track_id IN (" + S + ")"
    have = dict(conn.execute(f"SELECT p.platform, count(DISTINCT p.track_id) FROM platform_item p WHERE {ok} GROUP BY 1", args).fetchall())
    days = [r[0] for r in conn.execute("SELECT date(?, '-' || n || ' day') FROM (SELECT 6 n UNION ALL SELECT 5 UNION ALL SELECT 4 UNION ALL "
                                       "SELECT 3 UNION ALL SELECT 2 UNION ALL SELECT 1 UNION ALL SELECT 0)", (today,))]
    per_day = {}
    for plat, d, n in conn.execute(f"""SELECT p.platform, substr(p.first_seen_at,1,10), count(DISTINCT p.track_id) FROM platform_item p
                                      WHERE {ok} AND p.first_seen_at >= ? GROUP BY 1, 2""", args + (days[0],)):
        per_day.setdefault(plat, {})[d] = n
    routes = {}
    for plat, m, n in conn.execute(f"""SELECT p.platform, coalesce(p.match_method,'other'), count(DISTINCT p.track_id) FROM platform_item p
                                      WHERE {ok} AND p.first_seen_at >= ? GROUP BY 1, 2 ORDER BY 3 DESC""", args + (today,)):
        routes.setdefault(plat, []).append({"route": m, "songs": n})
    rows = []
    for key, name in ID_PLATFORMS:
        h = have.get(key, 0)
        rows.append({"platform": key, "name": name, "with_id": h, "without_id": songs - h, "share": round(100 * h / songs, 1) if songs else None,
                     "days": [per_day.get(key, {}).get(d, 0) for d in days], "today": per_day.get(key, {}).get(today, 0),
                     "routes_today": routes.get(key, [])})
    return {"songs": songs, "days": days, "platforms": rows}


def spotify_quota(conn):
    """Developer dashboard: Spotify publishes no quota number (it answers 429 "blocked for N h" when the day's share is
    used), so this shows the API state, calls made today and in the last 24 h, the calls made in the 24 h before the
    last block (the best measure of the real daily limit) and the blocks of the last 7 days."""
    def calls(frm, to="9999"):
        n = 0
        for (st,) in conn.execute("SELECT stats FROM run_log WHERE started_at >= ? AND started_at < ?", (frm, to)):
            x = json.loads(st or "{}")
            n += sum(int(x.get(k) or 0) for k in ("spotify_searches", "spotify_artist_calls", "api_calls") if isinstance(x.get(k), (int, float)))
        return n
    now = datetime.now(timezone.utc)
    last = conn.execute("SELECT checked_at, ok, status, message, retry_at FROM api_check WHERE source='spotify' ORDER BY id DESC LIMIT 1").fetchone()
    week = (now - timedelta(days=7)).isoformat(timespec="seconds")
    blocks = [dict(r) for r in conn.execute("""SELECT checked_at, retry_at, message FROM api_check WHERE source='spotify' AND ok=0 AND status=429
                                              AND checked_at >= ? ORDER BY id DESC""", (week,))]
    # one block can be seen by several checks: keep the first sighting of each retry time
    seen, uniq = set(), []
    for b in reversed(blocks):
        k = (b["retry_at"] or "")[:13]
        if k not in seen:
            seen.add(k)
            uniq.append(b)
    uniq.reverse()
    before = None
    if uniq:
        at = datetime.fromisoformat(uniq[0]["checked_at"])
        before = calls((at - timedelta(hours=24)).isoformat(timespec="seconds"), uniq[0]["checked_at"])
    blocked = bool(last and not last["ok"] and last["retry_at"] and last["retry_at"] > now.isoformat())
    return {"ok": bool(last and last["ok"]) and not blocked, "blocked": blocked, "checked_at": last and last["checked_at"],
            "message": last and last["message"], "retry_at": last and last["retry_at"] if blocked else None,
            "calls_today": calls(now.date().isoformat()), "calls_24h": calls((now - timedelta(hours=24)).isoformat(timespec="seconds")),
            "calls_before_last_block": before, "blocks_7d": [{"at": b["checked_at"], "until": b["retry_at"]} for b in uniq],
            # our own limit per account, rolling 24 h (counted on every call, in data/spotify_calls.db)
            "accounts": {a: {"used": spotify.calls_last_24h(a), "limit": spotify.daily_limit(a)} for a in ("primary", "second")}}


def api_dev(conn, qs):
    import shutil
    from . import spotify_plays
    from .youtube import DAILY_UNITS, key_status, next_reset, parse_keys
    cfg = config.load()
    song_daily.ensure(conn)
    since, until, S = _song_set({"focus": "all"})  # backlogs and readings cover every song the jobs work on
    F = _song_set({})[2]  # targets: the period's releases (older songs hidden, as on every page)
    today = date.today().isoformat()
    yday = conn.execute("SELECT date(?, '-1 day')", (today,)).fetchone()[0]
    one = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]
    n_songs = one(f"SELECT count(*) FROM ({F})", (since, until))

    # Phase 0 targets. Discovery counts uploads published after their channel was first synced (a channel added later
    # brings its back catalogue in at once, which is not a late discovery).
    disc = conn.execute("""WITH ch AS (SELECT channel, min(first_seen_at) since FROM platform_item WHERE platform='youtube' GROUP BY channel)
                           SELECT count(*), sum(julianday(p.first_seen_at) - julianday(p.published_at) <= 1)
                           FROM platform_item p JOIN ch ON ch.channel=p.channel
                           WHERE p.platform='youtube' AND p.published_at >= date('now', '-14 day') AND p.published_at > ch.since""").fetchone()
    matched = one(f"""SELECT count(DISTINCT track_id) FROM platform_item WHERE track_id IN ({F}) AND platform != 'youtube'
                      AND review_state IN ('auto','approved')""", (since, until))
    pending = one(f"SELECT count(*) FROM platform_item WHERE review_state='pending' AND (track_id IS NULL OR track_id IN ({F}))", (since, until))
    readings = []
    for p, metric, ok in (("youtube", "views", "('auto','approved','song')"), ("spotify", "plays", "('auto','approved')"),
                          ("ytmusic", "plays", "('auto')"), ("jiosaavn", "plays", "('auto','approved')"), ("deezer", "rank", None)):
        base = f"FROM platform_item p WHERE p.platform='{p}' AND p.track_id IN ({S})" + (f" AND p.review_state IN {ok}" if ok else "")
        items = one(f"SELECT count(*) {base}", (since, until))
        got = {d: one(f"""SELECT count(*) {base} AND EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id
                             AND s.metric='{metric}' AND s.capture_date=?)""", (since, until, d)) for d in (yday, today)}
        readings.append({"platform": p, "items": items, "yesterday": got[yday], "today": got[today]})
    yt = readings[0]
    pct = lambda a, b: round(100 * a / b, 1) if b else None
    targets = [
        {"name": "Releases discovered within 24 h", "target": 95, "value": pct(disc[1] or 0, disc[0]),
         "detail": f"{fmt_n(disc[1] or 0)} of {fmt_n(disc[0])} new uploads of the last 14 days"},
        {"name": "Songs matched to another platform", "target": 90, "value": pct(matched, n_songs),
         "detail": f"{fmt_n(matched)} of {fmt_n(n_songs)} releases of the period"},
        {"name": "Daily readings captured (YouTube, yesterday)", "target": 99, "value": pct(yt["yesterday"], yt["items"]),
         "detail": f"{fmt_n(yt['yesterday'])} of {fmt_n(yt['items'])} uploads"},
        {"name": "Matches needing manual review", "target": 10, "value": pct(pending, n_songs), "lower": True,
         "detail": f"{fmt_n(pending)} items in Review"},
    ]

    # Jobs: what runs now, what is next, how the last 24 hours went.
    runs = q(conn, "SELECT id, job, started_at, finished_at, status, stats FROM run_log WHERE started_at >= datetime('now', '-1 day') ORDER BY id DESC")
    by_job = {}
    for r in runs:
        st = json.loads(r.pop("stats") or "{}")
        r["note"] = (st.get("error") or st.get("stop_reason") or "")[:240]
        r["trigger"] = st.get("trigger")
        j = by_job.setdefault(r["job"], {"job": r["job"], "runs": 0, "ok": 0, "problems": 0, "last": r})
        j["runs"] += 1
        j["ok"] += r["status"] == "ok"
        j["problems"] += r["status"] in ("failed", "interrupted")
    failures = [r for r in runs if r["status"] in ("failed", "interrupted")][:5]
    try:
        nxt = jobs._due_work(cfg, {}, paused=pause.get(conn))
    except Exception as e:
        nxt = ("?", repr(e))

    # Backlogs.
    recheck = conn.execute("SELECT date(?, '-7 day')", (today,)).fetchone()[0]
    todo = lambda plat, key: one(f"""SELECT count(*) FROM track t WHERE t.id IN ({S})
                                     AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='{plat}')
                                     AND coalesce((SELECT value FROM state WHERE key='{key}:' || t.id), '') < ?""", (since, until, recheck))
    try:
        sp_due = len(spotify_plays.due_ids(conn))
    except Exception:
        sp_due = None
    backlog = [
        {"name": "YouTube Music: songs to search", "count": todo("ytmusic", "ytm_lookup"), "note": f"up to {cfg['songs'].get('ytmusic_searches_per_run', 40)} a run, while search quota lasts"},
        {"name": "JioSaavn: songs to search", "count": todo("jiosaavn", "js_lookup"), "note": "no quota; paced 1 request a second"},
        {"name": "Spotify: songs awaiting lookup", "count": one(f"SELECT count(*) FROM track WHERE id IN ({S}) AND (release_status IS NULL OR release_status='unchecked')", (since, until)), "note": "Spotify search, ~600 a day"},
        {"name": "Spotify: play counts still to read today", "count": sp_due, "note": "public track pages, headless browser"},
        {"name": "Release year: no evidence yet", "count": one(f"SELECT count(*) FROM track WHERE id IN ({S}) AND coalesce(release_check,'unknown')='unknown'", (since, until)), "note": "iTunes / MusicBrainz / Deezer / JioSaavn / Spotify"},
        {"name": "Review queue", "count": pending, "note": "Review page"},
    ]

    # Quotas.
    keys = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
    day_start = datetime.fromtimestamp(next_reset() - 86400, timezone.utc).isoformat(timespec="seconds")
    units = 0
    for (s,) in conn.execute("SELECT stats FROM run_log WHERE started_at >= ?", (day_start,)):
        st = json.loads(s or "{}")
        units += (st.get("quota_units") or 0) + (st.get("ytmusic_quota_units") or 0)
    sp = health.last_check(conn)

    # Storage and volume.
    dbf = config.DB_FILE
    size = sum(f.stat().st_size for f in dbf.parent.glob(dbf.name + "*") if f.is_file())
    du = shutil.disk_usage(dbf.parent)
    backups = sorted((f for f in (dbf.parent / "backups").glob("lens_*") if not f.name.endswith(".part")), key=lambda f: f.stat().st_mtime)
    volume = q(conn, """SELECT s.capture_date day, p.platform, count(*) n FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                        WHERE s.capture_date >= date(?, '-13 day') AND s.metric IN ('views','plays','rank') GROUP BY 1, 2 ORDER BY 1""", (today,))
    lines = [{"platform": p, "name": n, "points": [{"x": v["day"], "y": v["n"]} for v in volume if v["platform"] == p]}
             for p, n in (("youtube", "YouTube"), ("spotify", "Spotify"), ("jiosaavn", "JioSaavn"), ("ytmusic", "YouTube Music"), ("deezer", "Deezer"))]
    return {"today": today, "yesterday": yday, "since": since, "until": until, "songs": n_songs, "targets": targets,
            "job": MANAGER.state(10**9), "browser_job": BROWSER.state(10**9), "worker": cfg.get("worker", {}), "next": {"job": nxt[0], "why": nxt[1]} if nxt else None,
            "jobs": sorted(by_job.values(), key=lambda j: j["last"]["started_at"], reverse=True), "failures": failures,
            "freshness": alerts.freshness(conn, today),
            "readings": readings, "backlog": backlog, "ids": song_ids(conn, qs, today),
            "youtube": {"units_today": units, "units_limit": DAILY_UNITS * max(len(keys), 1), "keys": key_status(keys),
                        "resets": datetime.fromtimestamp(next_reset(), timezone.utc).isoformat(timespec="minutes")},
            "spotify": {"ok": sp and sp["ok"], "checked_at": sp and sp["checked_at"], "retry_at": sp and sp.get("retry_at")},
            "spotify_quota": spotify_quota(conn),
            "pause": pause.get(conn), "pause_log": pause.history(conn, 8),
            "job_names": {k: v for k, v in jobs.JOBS.items()},
            "storage": {"db_bytes": size, "disk_free": du.free, "disk_total": du.total, "snapshot_rows": one("SELECT max(rowid) FROM snapshot"),
                        "song_daily_rows": one("SELECT count(*) FROM song_daily"), "first_day": one("SELECT min(capture_date) FROM snapshot"),
                        "backup": {"name": backups[-1].name, "at": datetime.fromtimestamp(backups[-1].stat().st_mtime).isoformat(timespec="minutes")} if backups else None},
            "volume": lines}


def fmt_n(n):
    return f"{n or 0:,}"


GET = {"/api/coverage": api_coverage, "/api/insights": api_insights, "/api/insights/song": api_insights_song, "/api/ai/status": api_ai_status, "/api/ai/assist": api_ai_assist,
       "/api/meta/top": api_meta_top, "/api/meta/detail": api_meta_detail,
       "/api/cast": api_cast, "/api/cast/detail": api_cast_detail,
       "/api/merge/suggestions": api_merge_suggestions, "/api/status": api_status, "/api/overview": api_overview, "/api/crawl": api_crawl, "/api/client": api_client, "/api/search": api_search, "/api/dev": api_dev, "/api/tracks": api_tracks,
       "/api/track": api_track, "/api/platform": api_platform, "/api/report": api_report, "/api/daily": api_daily, "/api/shorts": api_shorts, "/api/system": lambda conn, qs: sysstats.api(conn), "/api/alerts": api_alerts, "/api/live": api_live, "/api/management": api_management, "/api/market_share": api_market_share, "/api/labels/report": api_label_report, "/api/freshness": api_freshness, "/api/additions": api_additions, "/api/heal": api_heal, "/api/shield": api_shield, "/api/charts": lambda conn, qs: alerts.charts_status(conn),
       "/api/collection": lambda conn, qs: collection.estimate(conn, overrides={k: qs[k] for k in collection.CHOICES if qs.get(k)}), "/api/items": api_items, "/api/song/compare": api_song_compare, "/api/items/history": api_item_history, "/api/runs": api_runs, "/api/seeds": api_seeds,
       "/api/artist_search": api_artist_search, "/api/health": api_health, "/api/datasets": api_datasets,
       "/api/datasets/view": api_dataset_view, "/api/raw": api_raw, "/api/raw/item": api_raw_item,
       "/api/matching": api_matching, "/api/matching/candidates": api_match_candidates,
       "/api/youtube/status": api_youtube_status, "/api/review/decisions": api_decisions}
POST = {"/api/config": post_config, "/api/secrets": post_secrets, "/api/test/spotify": post_test_spotify,
        "/api/seeds": post_seeds, "/api/run": post_run, "/api/cancel": post_cancel, "/api/instagram/link": post_instagram_link, "/api/alerts/snooze": post_alert_snooze, "/api/charts/token": post_charts_token,
        "/api/collection": lambda conn, body: (collection.save(body), collection.estimate(conn))[1],
        "/api/matching/decide": post_match_decide, "/api/matching/link": post_match_link,
        "/api/youtube/channel": post_youtube_channel, "/api/youtube/label": post_youtube_label,
        # Live Claude endpoints (lens/ai.py) are switched off until the API integration: add
        # "/api/ai/explain": post_ai_explain, "/api/ai/ask": post_ai_ask here to enable them.
        "/api/merge/apply": post_merge_apply, "/api/merge/reject": post_merge_reject, "/api/review/undo": post_undo,
        "/api/jobs/pause": post_jobs_pause, "/api/freshness/retry": post_freshness_retry, "/api/market_share/settings": post_market_share_settings, "/api/freshness/mark": post_freshness_mark, "/api/jobs/resume": post_jobs_resume}


# Keep the Console out of search engines: robots.txt (no crawling), plus a noindex header on every response.
ROBOTS_TXT = b"User-agent: *\nDisallow: /\n"
ROBOTS_TAG = "noindex, nofollow, noarchive, nosnippet, noimageindex"

LOGOUT_PAGE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Lens</title><meta name="robots" content="noindex, nofollow"><style>body{font:15px system-ui,sans-serif;background:#141414;color:#eee;display:grid;place-items:center;height:100vh;margin:0}
a{color:#6aa8ff}</style></head><body><div id="m">Signing out...</div><script>
// The browser keeps a Basic-auth login until it is closed. Logging in once with a throwaway user that the server accepts
// only on this request replaces the saved login, so the next visit asks for a username and password again.
var x = new XMLHttpRequest();
x.open("GET", "/?signout=" + Date.now(), true, "signout", "signout");
x.onloadend = function () { document.getElementById("m").innerHTML = 'You are logged out. <a href="/">Sign in again</a>'; };
x.send();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketLens"

    def log_message(self, fmt, *args):
        pass

    def _role(self):
        """'developer', 'client', or None after sending 401. Without STUDIO_PASSWORD (local use) everyone is developer."""
        if not PASSWORD:
            return "developer"
        h = self.headers.get("Authorization", "")
        ip = shield.client_ip(self.headers, self.client_address[0])
        if h.startswith("Basic "):
            user = ""
            try:
                user, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                dev_user, dev_pw = DEV_LOGIN
                if dev_user and dev_pw and hmac.compare_digest(user, dev_user) and hmac.compare_digest(pw, dev_pw):
                    return "developer"  # checked first: a lockout caused by others on the same IP never stops the developer
                if shield.login_blocked(ip, user):  # too many wrong passwords: locked out for a while (no guessing)
                    self._send(429, {"error": "Too many failed sign-ins. Try again in 15 minutes."})
                    return None
                if logins.verify(user, pw):  # a named client account (lens/logins.py): client pages, read-only
                    return "client"
                if hmac.compare_digest(pw, PASSWORD):
                    return "client"
            except Exception:
                pass
            if user != "signout":
                shield.login_failed(ip, user)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Market Lens"')
        self.send_header("X-Robots-Tag", ROBOTS_TAG)
        self.end_headers()
        return None

    def _send(self, code, body, ctype="application/json", extra=None):
        if getattr(self, "_client", False):  # no Instagram for client logins
            if not isinstance(body, bytes) and ctype.startswith("application/json"):
                body = no_instagram(body)
                body = {} if body is _DROP else body
                if getattr(self, "_enc_key", None):  # encoded for the page only: a saved network log shows no readable data
                    body = shield.encode(json.dumps(body, default=str).encode(), self._enc_key)
                    ctype, extra = "text/plain; charset=utf-8", dict(extra or {}, **{"X-ML-Enc": "1"})
            elif ctype.startswith("text/csv"):
                body = no_instagram_csv(body)
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", ROBOTS_TAG)  # never indexed or followed by search engines
        self.send_header("X-Frame-Options", "DENY")  # no other site may embed the Console (and copy from it)
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _login_user(self):
        h = self.headers.get("Authorization", "")
        try:
            return base64.b64decode(h[6:]).decode().split(":", 1)[0] if h.startswith("Basic ") else ""
        except Exception:
            return ""

    def _actor(self):
        user = ""
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user = base64.b64decode(h[6:]).decode().split(":", 1)[0]
            except Exception:
                pass
        ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]
        return f"{user or 'anonymous'} @ {ip}"

    def _dispatch(self, table, arg, role="developer"):
        REQUEST.actor = self._actor()
        REQUEST.role = role
        path = urllib.parse.urlparse(self.path).path
        fn = table.get(path)
        if not fn:
            return self._send(404, {"error": "not found"})
        conn = db.connect(str(config.DB_FILE))
        try:
            self._send(200, fn(conn, arg))
        except (ValueError, RuntimeError, re.error, SpotifyError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"server error: {e!r}"})
        finally:
            conn.close()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/robots.txt":  # no login needed: crawlers must be able to read that they may not crawl
            return self._send(200, ROBOTS_TXT, "text/plain; charset=utf-8")
        if url.path == "/logout":  # no login needed: the page itself signs the browser out
            return self._send(200, LOGOUT_PAGE, "text/html; charset=utf-8")
        if "signout=" in url.query and self.headers.get("Authorization", "") == "Basic " + base64.b64encode(b"signout:signout").decode():
            return self._send(200, b"signed out", "text/plain")
        role = self._role()
        if not role:
            return
        self._client = role == "client"
        self._enc_key = None
        url = urllib.parse.urlparse(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        if role == "client":
            qs.pop("focus", None)  # "Include older songs" is developer only: client logins always see the period's songs
        if role == "client":  # anti-scraping shield (lens/shield.py): no bots or scripts, browser session, rate and volume limits
            ua, ip = self.headers.get("User-Agent", ""), shield.client_ip(self.headers, self.client_address[0])
            cuser = self._login_user()
            if shield.is_bot(ua):
                shield.log_event("bot", cuser, ip, url.path, ua[:120])
                return self._send(403, {"error": "automated access is not allowed"})
            if url.path.startswith("/api/"):
                self._enc_key = shield.session_ok(self.headers.get("Cookie", ""), cuser, ua)
                if self._enc_key == shield.ENDED:  # signed in on another browser since (one browser per login)
                    self._enc_key = None
                    return self._send(403, {"error": "in use elsewhere", "elsewhere": True})
                if not self._enc_key:
                    if self.headers.get("X-ML-App") == "1":  # the Console itself with an expired session: it reloads
                        return self._send(403, {"error": "session expired: reload the page"})
                    shield.log_event("session", cuser, ip, url.path, "no browser session")
                    return self._send(403, {"error": "open the Console to see this data"})
                verdict = shield.allow(cuser, ip, url.path, qs)
                if verdict:
                    return self._send(verdict[0], {"error": verdict[1]})
        if role == "client" and url.path.startswith("/api/") and (self.headers.get("X-ML-App") != "1" or self.headers.get("Sec-Fetch-Dest") == "document"):
            # client data feeds only for the Console itself, not opened directly in the browser (no copying of raw data)
            return self._send(403, {"error": "open the Console to see this data"})
        if role == "client" and (url.path.endswith((".csv", ".csv.gz")) or "download" in url.path or "/archive/" in url.path):
            return self._send(403, {"error": "downloads are not available for this login"})
        if role == "client" and ((url.path.startswith("/api/") and url.path not in CLIENT_GET) or url.path.startswith("/docs/")):
            return self._send(403, {"error": "not available for this login"})
        if url.path == "/api/job":
            st = MANAGER.state(int(qs.get("from", 0)))
            st["browser"] = {k: v for k, v in BROWSER.state(10**9).items() if k != "log"}
            c = db.connect(str(config.DB_FILE))
            try:
                st["pause"] = pause.get(c)
            finally:
                c.close()
            return self._send(200, st)
        if url.path == "/api/export.csv":
            from . import songs
            conn = db.connect(str(config.DB_FILE))
            rows = songs.export_rows(conn, *config.window(config.load()))
            conn.close()
            buf = io.StringIO()
            songs.write_csv(rows, buf)
            return self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="market_lens_songs_{date.today()}.csv"'})
        if url.path == "/api/items.csv":
            conn = db.connect(str(config.DB_FILE))
            text, name = items_csv(conn, qs)
            conn.close()
            return self._send(200, text.encode("utf-8-sig"), "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{name}"'})
        if url.path == "/api/daily.csv":
            conn = db.connect(str(config.DB_FILE))
            text, name = daily_csv(conn, qs)
            conn.close()
            return self._send(200, text.encode("utf-8-sig"), "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{name}"'})
        if url.path == "/api/report.csv":
            conn = db.connect(str(config.DB_FILE))
            text, frm, to = report_csv(conn, qs)
            conn.close()
            return self._send(200, text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="market_lens_report_{frm}_to_{to}.csv"'})
        if url.path == "/api/labels/report.csv":
            from . import label_report
            conn = db.connect(str(config.DB_FILE))
            try:
                text, name = label_report.csv_text(conn, qs.get("view") or "daily", qs.get("month") or None, qs.get("by") or "yt")
            finally:
                conn.close()
            return self._send(200, text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="market_lens_{name}.csv"'})
        if url.path in ("/api/datasets/download", "/api/datasets/archived"):  # CSV built in memory on request; nothing kept on disk
            conn = db.connect(str(config.DB_FILE))
            try:
                body, name = exports.download(conn, qs) if url.path.endswith("download") else exports.archived(qs.get("name"))
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            finally:
                conn.close()
            return self._send(200, body, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{name}"'})
        if url.path.startswith("/api/"):
            return self._dispatch(GET, qs, role)
        if url.path.startswith("/docs/"):
            f = (DOCS / url.path[len("/docs/"):]).resolve()
            if DOCS not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = "text/html; charset=utf-8" if f.suffix == ".html" else "text/plain; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype)
        name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
        f = (STATIC / name).resolve()
        if STATIC not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        extra, data = None, f.read_bytes()
        if role == "client" and name == "index.html":  # the Console page hands out the browser session its data feeds need
            https = self.headers.get("X-Forwarded-Proto", "") == "https"
            ip, cuser, ua = shield.client_ip(self.headers, self.client_address[0]), self._login_user(), self.headers.get("User-Agent", "")
            key = shield.session_ok(self.headers.get("Cookie", ""), cuser, ua)  # this browser's session is still valid: kept
            if not key or key == shield.ENDED:  # (an ended one: opening the page again takes the login over here)  # (a reload must not count as another browser and sign out the login's other devices)
                cookie, key = shield.make_cookie(cuser, ua, https, ip)
                extra = {"Set-Cookie": cookie}
            data = data.replace(b"</head>", f'<meta name="ml-k" content="{key}"></head>'.encode(), 1)
        self._send(200, data, mimetypes.guess_type(f.name)[0] or "application/octet-stream", extra)

    def do_POST(self):
        role = self._role()
        if not role:
            return
        if role != "developer":
            return self._send(403, {"error": "read-only login"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        self._dispatch(POST, body)


def main():
    global PASSWORD, DEV_LOGIN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3000)
    args = ap.parse_args()

    PASSWORD = os.environ.get("STUDIO_PASSWORD") or config.read_secrets().get("STUDIO_PASSWORD")
    DEV_LOGIN = (config.read_secrets().get("DEVELOPER_USERNAME"), config.read_secrets().get("DEVELOPER_PASSWORD"))
    if args.host not in ("127.0.0.1", "localhost", "::1") and not PASSWORD:
        sys.exit("Refusing to listen on a non-loopback address without STUDIO_PASSWORD set.")

    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db.connect(str(config.DB_FILE)).close()
    # Runs left 'running' by a previous process can never finish.
    c = db.connect(str(config.DB_FILE))
    c.execute("UPDATE run_log SET status='interrupted', finished_at=? WHERE status='running'", (db.now_iso(),))
    c.commit()
    c.close()

    jobs.start_scheduler(MANAGER)
    jobs.start_health_watcher(MANAGER)
    jobs.start_worker(MANAGER, BROWSER)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Market Lens Console on http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
