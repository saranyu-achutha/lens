"""YouTube sync: collect uploads of the labels' channels, read view/like/comment counts,
and match videos to Spotify tracks with fixed rules (lens.match).

Readings: every run for videos published in the last `fresh_days` (the studio runs this every 6 h),
otherwise once a day. Quota: playlistItems and videos cost 1 unit per 50 items, so a run uses a few
hundred of the 10,000 daily units.
"""
import csv
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from . import config, db, match
from .youtube import YouTube, YouTubeError, iso_duration, parse_keys

MATCHABLE = ("video", "lyric", "audio", "other")
READ_WORKERS = 8  # parallel videos.list calls when reading counts (quota is per call, not per second)


class Cancelled(Exception):
    pass


def _state(conn, key, value=None):
    if value is None:
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (key, value))


def collect_uploads(conn, yt, channels, pool_start, ts, log, check):
    """Store every upload published on/after pool_start. Returns number of new videos."""
    new = 0
    for ch in channels:
        scan_key = f"yt_scan:{ch['id']}:{pool_start}"
        scanned = _state(conn, scan_key) == "done"
        token, found, pages = None, 0, 0
        while True:
            check()
            page = yt.uploads(ch["id"], token)
            pages += 1
            items = page.get("items", [])
            all_old, all_known = True, True
            for it in items:
                vid = it["contentDetails"]["videoId"]
                pub = it["contentDetails"].get("videoPublishedAt")
                if not pub:  # private or deleted
                    continue
                if pub[:10] < pool_start:
                    continue
                all_old = False
                sn = it["snippet"]
                exists = conn.execute("SELECT 1 FROM platform_item WHERE platform='youtube' AND platform_id=?",
                                      (vid,)).fetchone()
                if not exists:
                    all_known = False
                    new += 1
                conn.execute(
                    """INSERT INTO platform_item (platform, platform_id, title, url, published_at, channel,
                           description, first_seen_at)
                       VALUES ('youtube', ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(platform, platform_id) DO UPDATE SET title=excluded.title,
                           description=excluded.description""",
                    (vid, sn["title"], f"https://www.youtube.com/watch?v={vid}", pub, ch["title"],
                     (sn.get("description") or "")[:2000], ts))
                found += 1
            token = page.get("nextPageToken")
            # Uploads are newest-first: stop once a page is entirely older than the window, or (after
            # one complete scan) once a page holds nothing new.
            if not token or all_old or (scanned and all_known):
                break
        if not token or all_old:
            _state(conn, scan_key, "done")
        conn.commit()
        log(f"channel {ch['title']} ({ch['label']}): {found} videos in window ({pages} pages)")
    return new


def take_readings(conn, yt, ycfg, channels, pool_start, ts, log, check, flush_raw=lambda: None):
    """Read views/likes/comments for every video that is due, on the given (switched-on) channels.
    Returns number of videos read."""
    today = ts[:10]
    titles = [c["title"] for c in channels]
    fresh_from = (datetime.now(timezone.utc) - timedelta(days=ycfg["fresh_days"])).isoformat()
    six_h_ago = (datetime.now(timezone.utc) - timedelta(hours=5, minutes=30)).isoformat()
    due = [r["platform_id"] for r in conn.execute(
        """SELECT pi.platform_id FROM platform_item pi
           WHERE pi.platform='youtube' AND pi.published_at >= ?  -- only uploads from the period start on, linked or not
             AND pi.channel IN (SELECT value FROM json_each(?))
             AND (pi.snippet_at IS NULL
                  OR NOT EXISTS (SELECT 1 FROM reading r WHERE r.platform_item_id=pi.id AND r.metric='views'
                                 AND r.captured_at >= CASE WHEN pi.published_at >= ? THEN ? ELSE ? END))""",
        (pool_start, json.dumps(titles), fresh_from, six_h_ago, today))]
    if not due:
        return 0
    log(f"reading view counts for {len(due)} videos")
    n = 0
    batches = [due[i:i + 50] for i in range(0, len(due), 50)]
    with ThreadPoolExecutor(READ_WORKERS) as pool:  # API calls in parallel; database writes stay on this thread
        for c in range(0, len(batches), READ_WORKERS * 2):
            check()
            for v in (v for vids in pool.map(yt.videos, batches[c:c + READ_WORKERS * 2]) for v in vids):
                item = conn.execute("SELECT id, title FROM platform_item WHERE platform='youtube' AND platform_id=?",
                                    (v["id"],)).fetchone()
                if not item:
                    continue
                dur = iso_duration(v.get("contentDetails", {}).get("duration"))
                sn = v.get("snippet") or {}
                conn.execute("""UPDATE platform_item SET duration_ms=?, asset_type=?, yt_tags=?, audio_language=?,
                                    yt_category=?, snippet_at=?, description=COALESCE(?, description) WHERE id=?""",
                             (dur * 1000 if dur is not None else None, match.asset_type(item["title"], dur),
                              json.dumps(sn.get("tags") or []), sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                              sn.get("categoryId"), ts, (sn.get("description") or "")[:5000] or None, item["id"]))
                st = v.get("statistics", {})
                for metric, key in (("views", "viewCount"), ("likes", "likeCount"), ("comments", "commentCount")):
                    if key in st:
                        val = int(st[key])
                        conn.execute("INSERT INTO reading (platform_item_id, metric, value, captured_at) VALUES (?,?,?,?)",
                                     (item["id"], metric, val, ts))
                        conn.execute("""INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at)
                                        VALUES (?,?,?,?,?)
                                        ON CONFLICT(platform_item_id, metric, capture_date)
                                        DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                                     (item["id"], metric, val, today, ts))
                n += 1
            flush_raw()
            conn.commit()
    return n


def run_matching(conn, since, log):
    """Score unmatched/pending videos against tracks released in the window."""
    tracks = {}
    for r in conn.execute(
            """SELECT t.id, t.title, t.duration_ms, r.title release_title, r.release_date
               FROM track t JOIN release r ON r.id=t.release_id WHERE r.release_date >= ?""", (since,)):
        tracks[r["id"]] = dict(r, artists=[])
    for r in conn.execute("""SELECT ta.track_id, a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
                             ORDER BY ta.position"""):
        if r["track_id"] in tracks:
            tracks[r["track_id"]]["artists"].append(r["name"])
    index = {}
    for tid, t in tracks.items():
        core, film, _ = match.parse_spotify_title(t["title"])
        for k in match.block_keys(core):
            index.setdefault(k, set()).add(tid)
    rejected = {(r[0], r[1]) for r in conn.execute("SELECT platform_item_id, track_id FROM match_rejection")}

    videos = conn.execute(
        f"""SELECT id, title, description, published_at, duration_ms, review_state FROM platform_item
            WHERE platform='youtube' AND asset_type IN ({','.join('?' * len(MATCHABLE))})
              AND (track_id IS NULL OR review_state IN ('pending','auto'))""", MATCHABLE).fetchall()
    auto = pending = 0
    for v in videos:
        keys = set()
        for part in match.title_parts(v["title"])[:4]:
            keys |= match.block_keys(part)
        cands = set().union(*(index.get(k, set()) for k in keys)) if keys else set()
        best = None
        vd = {"title": v["title"], "description": v["description"], "published_at": v["published_at"],
              "duration_s": (v["duration_ms"] or 0) / 1000 or None}
        for tid in cands:
            if (v["id"], tid) in rejected:
                continue
            total, detail = match.score(tracks[tid], vd)
            if best is None or total > best[0]:
                best = (total, detail, tid)
        decision = match.decide(best[0], best[1]) if best else None
        if decision:
            conn.execute("""UPDATE platform_item SET track_id=?, match_method='rules', match_score=?, match_detail=?,
                            review_state=? WHERE id=?""",
                         (best[2], best[0], json.dumps(best[1]), decision, v["id"]))
            auto += decision == "auto"
            pending += decision == "pending"
        elif v["review_state"] in ("pending", "auto"):  # no longer a plausible match
            conn.execute("UPDATE platform_item SET track_id=NULL, match_score=NULL, match_detail=NULL, "
                         "review_state=NULL WHERE id=?", (v["id"],))
    conn.commit()
    log(f"matching: {len(videos)} videos scored against {len(tracks)} tracks: {auto} auto-matched, "
        f"{pending} sent to review")
    return auto, pending


YT_EXPORT_SQL = """
SELECT t.isrc, t.title AS track_title, r.release_date, r.title AS release_title,
       pi.platform_id AS video_id, pi.title AS video_title, pi.channel, pi.asset_type,
       substr(pi.published_at, 1, 10) AS published, pi.review_state, pi.match_score,
       (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='views' ORDER BY capture_date DESC LIMIT 1) views,
       (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='likes' ORDER BY capture_date DESC LIMIT 1) likes,
       (SELECT value FROM snapshot s WHERE s.platform_item_id=pi.id AND metric='comments' ORDER BY capture_date DESC LIMIT 1) comments,
       pi.url
FROM platform_item pi
LEFT JOIN track t ON t.id = pi.track_id
LEFT JOIN release r ON r.id = t.release_id
WHERE pi.platform='youtube'
ORDER BY (pi.track_id IS NULL), r.release_date DESC, views DESC
"""


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    ycfg, dcfg = cfg["youtube"], cfg["discovery"]
    keys = parse_keys(secrets.get("YOUTUBE_API_KEY"))
    if not keys:
        raise YouTubeError(0, "config", "YouTube API key not set (Sources page)")

    def check():
        if should_stop():
            raise Cancelled()

    owner, queued = threading.get_ident(), []  # raw responses from reader threads, written by this thread

    def flush_raw():
        while queued:
            conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                         " VALUES (?,'youtube',?,?,?,?)", queued.pop(0))

    def on_raw(url, status, body):
        if dcfg["store_raw"]:
            queued.append((run_id, url, status, db.now_iso(), body))
            if threading.get_ident() == owner:
                flush_raw()

    yt = YouTube(keys, on_raw=on_raw, log=log)
    ts = db.now_iso()
    since = dcfg["since"]
    pool_start = (date.fromisoformat(since) - timedelta(days=ycfg["lookback_days"])).isoformat()
    status, stop_reason = "ok", None
    new = read = auto = pending = 0
    channels = config.active_channels(cfg)
    labels = sorted({c["label"] for c in channels})
    log(f"YouTube sync: {len(channels)} channels of {len(labels)} labels ({', '.join(labels)}), "
        f"videos published since {pool_start}, {len(keys)} API key(s)")
    try:
        new = collect_uploads(conn, yt, channels, pool_start, ts, log, check)
        read = take_readings(conn, yt, ycfg, channels, since, ts, log, check, flush_raw)  # uploads since the period start only
    except YouTubeError as e:
        if not e.quota_exceeded:
            raise
        status, stop_reason = "quota_exceeded", "YouTube daily quota exceeded on every key (resets at midnight Pacific time)"
        log(f"⚠ {stop_reason}")
    conn.commit()
    auto, pending = run_matching(conn, since, log)

    s = conn.execute("""SELECT
        (SELECT count(*) FROM track t JOIN release r ON r.id=t.release_id WHERE r.release_date >= ?) tracks,
        (SELECT count(DISTINCT pi.track_id) FROM platform_item pi JOIN track t ON t.id=pi.track_id
           JOIN release r ON r.id=t.release_id
           WHERE pi.platform='youtube' AND pi.review_state IN ('auto','approved','song') AND r.release_date >= ?) matched,
        (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state='pending') pending_total,
        (SELECT count(*) FROM platform_item WHERE platform='youtube' AND review_state IN ('auto','approved','song')) videos_matched,
        (SELECT count(*) FROM platform_item WHERE platform='youtube') videos""", (since, since)).fetchone()
    out = config.OUT_DIR / f"market_lens_youtube_{ts[:10]}_run{run_id or 0}.csv"
    rows = conn.execute(YT_EXPORT_SQL).fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else [])
        w.writerows(tuple(r) for r in rows)
    pct = 100 * s["matched"] / s["tracks"] if s["tracks"] else 0
    log(f"tracks with a YouTube video: {s['matched']}/{s['tracks']} ({pct:.0f}%), "
        f"awaiting review: {s['pending_total']}, quota used: {yt.units} units")
    log(f"wrote {len(rows)} rows -> {out.name}")
    return {"status": status, "stop_reason": stop_reason, "new_videos": new, "videos_read": read,
            "videos": s["videos"], "auto_matched": auto, "sent_to_review": pending,
            "tracks_in_window": s["tracks"], "tracks_matched": s["matched"], "match_rate": round(pct, 1),
            "pending_review": s["pending_total"], "quota_units": yt.units, "quota_units_by_key": yt.units_by_key,
            "csv": out.name}
