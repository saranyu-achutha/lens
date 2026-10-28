"""Market Lens Console: web UI to configure, run and browse the pipeline.

Usage:
    python3 -m lens.studio [--host 127.0.0.1] [--port 3000]

Binding to a non-loopback host requires STUDIO_PASSWORD (env or .env); the UI then
asks for it via HTTP basic auth (any username).
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
import sys
import urllib.parse
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import threading

from . import ai, config, db, health, insights, jobs, merge, metadata, review, song_daily, songs, spotify_charts
from .discover_spotify import EXPORT_SQL
from .spotify import Spotify, SpotifyError

STATIC = Path(__file__).resolve().parent / "static"
DOCS = Path(__file__).resolve().parent.parent / "docs"
REQUEST = threading.local()  # who is making the current request (for the decision log)
MANAGER = jobs.JobManager()
PASSWORD = None

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
    out = {k: {"set": bool(s.get(k)), "hint": mask(s.get(k, "")) if k != "SPOTIFY_CLIENT_SECRET"
               else ("set" if s.get(k) else "")} for k in config.SECRET_KEYS}
    from .youtube import parse_keys
    out["YOUTUBE_API_KEY"]["count"] = len(parse_keys(s.get("YOUTUBE_API_KEY")))
    return out


def api_status(conn, _):
    cfg = config.load()
    nxt = jobs.next_scheduled(cfg)
    counts = q(conn, """SELECT (SELECT count(*) FROM release) releases, (SELECT count(*) FROM track) tracks,
                              (SELECT count(*) FROM artist) artists, (SELECT count(*) FROM snapshot) snapshots,
                              (SELECT max(capture_date) FROM snapshot) last_capture,
                              (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='pending') yt_pending""")[0]
    last_ok = q(conn, "SELECT finished_at FROM run_log WHERE status='ok' ORDER BY id DESC LIMIT 1")
    return {"meta_fields": metadata.FIELDS, "explore_fields": metadata.EXPLORE, "config": cfg, "secrets": secret_status(), "job": MANAGER.state(10**9), "jobs": jobs.JOBS,
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None, "counts": counts,
            "last_ok": last_ok[0]["finished_at"] if last_ok else None, "today": date.today().isoformat()}


def tag_labels(rows, cfg=None):
    """Add music_label (the tracked label owning the song's YouTube channel) to song rows."""
    lab = config.channel_labels(cfg or config.load())
    for r in rows:
        r["music_label"] = lab.get(r.get("yt_channel"), config.FIRST_LABEL) if r.get("yt_channel") else ""
    return rows


def api_overview(conn, _):
    since, until = config.window(config.load())
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?"
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
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?"
    a = tracks_args() + [since, until]
    songs = q(conn, f"SELECT count(*) n FROM ({W})", a)[0]["n"] or 0
    pct = lambda n: f"{round(100 * (n or 0) / songs) if songs else 0}% of {songs:,} songs"
    out = {"platform": p, "since": since, "until": until, "songs": songs}
    if p == "youtube":
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


# Daily data page: song_daily rows, by day (all songs) or by song (all days), with the change from the previous day.
DAILY_COLS = [("yt_views", "YouTube views"), ("yt_likes", "YouTube likes"), ("yt_comments", "YouTube comments"),
              ("ytm_plays", "YouTube Music plays"), ("sp_plays", "Spotify plays"), ("js_plays", "JioSaavn plays"), ("dz_rank", "Deezer rank"),
              ("chart_rank", "Spotify India chart")]


GAIN = {"yt_views", "yt_likes", "yt_comments", "ytm_plays", "sp_plays", "js_plays"}


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
        t = q(conn, "SELECT id, title FROM track WHERE id=?", (int(qs["song"]),))
        return {"mode": "song", "song": t[0] if t else None, "days": days, "rows": rows}
    day = qs.get("day") or (days[-1] if days else None)
    prev = max((d for d in days if day and d < day), default=None)
    rows = tag_labels(q(conn, f"""SELECT d.song_id id, t.title, substr(t.first_published,1,10) song_date, d.yt_uploads_read, {cols},
                                    (SELECT channel FROM platform_item x WHERE x.track_id=t.id AND x.platform='youtube' ORDER BY published_at LIMIT 1) yt_channel
                                  FROM song_daily d JOIN track t ON t.id=d.song_id
                                  LEFT JOIN song_daily p ON p.song_id=d.song_id AND p.day=?
                                  WHERE d.day=?""", (prev, day)))
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
    return d


def daily_csv(conn, qs):
    d = daily_rows(conn, qs)
    buf = io.StringIO()
    w = csv.writer(buf)
    metrics = [k for k, _ in DAILY_COLS]
    if d["mode"] == "song":
        w.writerow(["day"] + [x for k in metrics for x in (k, k + "_change")] + ["yt_uploads_read"])
        for r in d["rows"]:
            w.writerow([r["day"]] + [x for k in metrics for x in (r[k], daily_change(r, k))] + [r["yt_uploads_read"]])
        name = f"market_lens_song_{qs['song']}_daily.csv"
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
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?"
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
    w = csv.writer(buf)
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
    return {"meta": metadata.for_song(conn, tid), "track": tag_labels(t)[0], "items": items, "history": history,
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
    return Spotify(s.get("SPOTIFY_CLIENT_ID"), s.get("SPOTIFY_CLIENT_SECRET"), market=config.load()["discovery"]["market"])


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
    for section in ("discovery", "schedule", "health", "youtube"):
        cfg[section].update(body.get(section, {}))
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


OUTPUT_RE = re.compile(r"[\w.-]+\.csv")


def _output_path(name):
    if not name or not OUTPUT_RE.fullmatch(name):
        raise ValueError("bad file name")
    f = config.OUT_DIR / name
    if not f.is_file():
        raise ValueError("file not found")
    return f


def api_outputs(conn, _):
    runs = {r["id"]: r for r in q(conn, "SELECT id, started_at, status FROM run_log")}
    files = []
    for f in sorted(config.OUT_DIR.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True):
        m = re.search(r"_run(\d+)\.csv$", f.name)
        run_id = int(m.group(1)) if m else None
        with f.open(encoding="utf-8", errors="replace") as fh:
            rows = max(sum(1 for _ in fh) - 1, 0)
        files.append({"name": f.name, "size": f.stat().st_size, "rows": rows, "run_id": run_id,
                      "run": runs.get(run_id),
                      "modified": datetime.fromtimestamp(f.stat().st_mtime).astimezone().isoformat(timespec="seconds")})
    return {"files": files}


def api_output_preview(conn, qs):
    f = _output_path(qs.get("name"))
    with f.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        rows = list(reader)
    if qs.get("q"):
        needle = qs["q"].lower()
        rows = [r for r in rows if any(needle in c.lower() for c in r)]
    offset, limit = int(qs.get("offset", 0)), min(int(qs.get("limit", 100)), 1000)
    return {"name": f.name, "header": header, "total": len(rows), "rows": rows[offset:offset + limit]}


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
    run_id = MANAGER.start(body.get("job", "spotify_discovery"), since=body.get("since"), until=body.get("until"))
    return {"run_id": run_id}


def post_cancel(conn, body):
    return {"cancelled": MANAGER.cancel()}


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
                  WHERE sp.platform='spotify' AND sp.review_state IN ({states}) AND t.first_published >= ?
                    AND (sp.title LIKE ? OR t.yt_title LIKE ?)""".format(
            states="'pending'" if state == "spotify_pending" else "'auto','approved'")
        args = [since, like, like]
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
                           (SELECT count(*) FROM platform_item WHERE platform='spotify' AND review_state='pending') spotify_pending,
                           (SELECT count(*) FROM platform_item sp JOIN track t ON t.id=sp.track_id WHERE sp.platform='spotify'
                              AND sp.review_state IN ('auto','approved') AND t.first_published >= ?) spotify_linked
                    FROM ({TRACKS_SQL}) WHERE song_date >= ?""", [since, since] + tracks_args() + [since])[0]
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
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?"
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
    W = f"SELECT * FROM ({TRACKS_SQL}) WHERE song_date >= ? AND song_date <= ?"
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
    W = "t.first_published >= ? AND substr(t.first_published,1,10) <= ?"
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
    for r in conn.execute("SELECT stats FROM run_log WHERE job IN ('youtube_sync','pipeline') AND started_at >= ?", (day_start,)):
        st = json.loads(r["stats"] or "{}")
        units += st.get("quota_units") or 0
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


GET = {"/api/coverage": api_coverage, "/api/insights": api_insights, "/api/insights/song": api_insights_song, "/api/ai/status": api_ai_status, "/api/ai/assist": api_ai_assist,
       "/api/meta/top": api_meta_top, "/api/meta/detail": api_meta_detail,
       "/api/merge/suggestions": api_merge_suggestions, "/api/status": api_status, "/api/overview": api_overview, "/api/tracks": api_tracks,
       "/api/track": api_track, "/api/platform": api_platform, "/api/report": api_report, "/api/daily": api_daily, "/api/runs": api_runs, "/api/seeds": api_seeds,
       "/api/artist_search": api_artist_search, "/api/health": api_health, "/api/outputs": api_outputs,
       "/api/outputs/preview": api_output_preview, "/api/raw": api_raw, "/api/raw/item": api_raw_item,
       "/api/matching": api_matching, "/api/matching/candidates": api_match_candidates,
       "/api/youtube/status": api_youtube_status, "/api/review/decisions": api_decisions}
POST = {"/api/config": post_config, "/api/secrets": post_secrets, "/api/test/spotify": post_test_spotify,
        "/api/seeds": post_seeds, "/api/run": post_run, "/api/cancel": post_cancel,
        "/api/matching/decide": post_match_decide, "/api/matching/link": post_match_link,
        "/api/youtube/channel": post_youtube_channel, "/api/youtube/label": post_youtube_label,
        # Live Claude endpoints (lens/ai.py) are switched off until the API integration: add
        # "/api/ai/explain": post_ai_explain, "/api/ai/ask": post_ai_ask here to enable them.
        "/api/merge/apply": post_merge_apply, "/api/merge/reject": post_merge_reject, "/api/review/undo": post_undo}


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketLens"

    def log_message(self, fmt, *args):
        pass

    def _auth_ok(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode().split(":", 1)[1]
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Market Lens"')
        self.end_headers()
        return False

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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

    def _dispatch(self, table, arg):
        REQUEST.actor = self._actor()
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
        if not self._auth_ok():
            return
        url = urllib.parse.urlparse(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/api/job":
            return self._send(200, MANAGER.state(int(qs.get("from", 0))))
        if url.path == "/api/export.csv":
            from . import songs
            conn = db.connect(str(config.DB_FILE))
            rows = songs.export_rows(conn, *config.window(config.load()))
            conn.close()
            buf = io.StringIO()
            songs.write_csv(rows, buf)
            return self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="market_lens_songs_{date.today()}.csv"'})
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
        if url.path == "/api/outputs/download":
            try:
                f = _output_path(qs.get("name"))
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            return self._send(200, f.read_bytes(), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{f.name}"'})
        if url.path.startswith("/api/"):
            return self._dispatch(GET, qs)
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
        self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")

    def do_POST(self):
        if not self._auth_ok():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        self._dispatch(POST, body)


def main():
    global PASSWORD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3000)
    args = ap.parse_args()

    PASSWORD = os.environ.get("STUDIO_PASSWORD") or config.read_secrets().get("STUDIO_PASSWORD")
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
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Market Lens Console on http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
