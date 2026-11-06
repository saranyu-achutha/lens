"""Streaming links the labels put in their own YouTube descriptions ("Spotify: open.spotify.com/track/…", "Stream on
JioSaavn …", "YouTube Music …", "Instagram audio …"): official IDs for free, from text we already store.

Each upload on a label channel is scanned once (state 'desc_links:<item id>'). A link is only attached to the song when
the song has no link on that platform yet and the linked track checks out as the same song:
  spotify    embed page (title, artists, date, duration) scored by match.score / decide, like the other routes
  jiosaavn   track details from JioSaavn's public JSON (webapi.get), title score >= 0.9
  ytmusic    title from YouTube's public oEmbed (no quota), title score >= 0.85; only when the description has one link
  instagram  attached for the daily reader, which counts it only when the audio page's title matches (official audio)
Albums, playlists and profile links are skipped (several songs, or none in particular).
"""
import html
import json
import re
import time
import urllib.parse

from . import config, instagram_audio, jiosaavn, match
from .spotify_links import _score, _text, _vids, attach, embed_details

SPOTIFY = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?track/([A-Za-z0-9]{22})")
JIOSAAVN = re.compile(r"jiosaavn\.com/song/[^/\s]+/([A-Za-z0-9_-]{8,14})")
YTMUSIC = re.compile(r"music\.youtube\.com/watch\?v=([\w-]{11})")
INSTAGRAM = re.compile(r"instagram\.com/reels?/audio/(\d{8,25})")


def _todo(conn, cfg, limit):
    since, until = config.window(cfg)
    channels = list(config.channel_labels(cfg))
    return conn.execute(f"""
        SELECT p.id, p.track_id, p.description FROM platform_item p JOIN track t ON t.id=p.track_id
        WHERE p.platform='youtube' AND p.review_state IN ('song','auto','approved') AND coalesce(p.description,'') != ''
          AND p.channel IN ({",".join("?" * len(channels))}) AND t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?
          AND NOT EXISTS (SELECT 1 FROM state s WHERE s.key='desc_links:' || p.id)
        ORDER BY t.first_published DESC LIMIT ?""", (*channels, since, until, limit)).fetchall()


def _has(conn, tid, platform):
    return conn.execute("SELECT 1 FROM platform_item WHERE track_id=? AND platform=? AND review_state IN ('auto','approved','song')",
                        (tid, platform)).fetchone() is not None


def _song_title(conn, tid):
    return match.parse_spotify_title(conn.execute("SELECT coalesce(yt_title, title) FROM track WHERE id=?", (tid,)).fetchone()[0])[0]


def run(conn, cfg, ts, log=print, check=lambda: None):
    todo = _todo(conn, cfg, cfg["songs"].get("description_links_per_run", 2000))
    found = {"spotify": 0, "jiosaavn": 0, "ytmusic": 0, "instagram": 0}
    for iid, tid, desc in todo:
        check()
        sp, js, ym, ig = (list(dict.fromkeys(r.findall(desc))) for r in (SPOTIFY, JIOSAAVN, YTMUSIC, INSTAGRAM))
        if sp and not _has(conn, tid, "spotify"):
            vids = _vids(conn, tid)
            for sid in sp[:3]:
                det = embed_details(sid)
                time.sleep(1)
                ok = _score(dict(det, release_title=None), vids) if det else None
                if ok and attach(conn, tid, sid, "description", ok[0], dict(ok[1], via="youtube_description", candidate_title=det["title"],
                                                                            candidate_artists=det["artists"]), det["title"], ts):
                    conn.execute("UPDATE platform_item SET published_at=?, duration_ms=? WHERE platform='spotify' AND platform_id=?",
                                 (det["release_date"], det["duration_ms"], sid))
                    found["spotify"] += 1
                    break
        if js and not _has(conn, tid, "jiosaavn"):
            core = _song_title(conn, tid)
            for token in js[:3]:
                d = jiosaavn._get("webapi.get", token=token, type="song")
                x = (d.get("songs") or [None])[0] if isinstance(d, dict) else None
                if not x or not x.get("id"):
                    continue
                title = html.unescape(x.get("title") or "")
                ts_ = match.title_score(core, match.parse_spotify_title(title)[0])
                if ts_ < 0.9:
                    continue
                mi = x.get("more_info") or {}
                conn.execute("""INSERT INTO platform_item (platform, platform_id, title, url, published_at, track_id, review_state,
                                    match_method, match_score, match_detail, first_seen_at)
                                VALUES ('jiosaavn', ?, ?, ?, ?, ?, 'auto', 'description', ?, ?, ?) ON CONFLICT(platform, platform_id) DO NOTHING""",
                             (x["id"], title, x.get("perma_url"), x.get("release_date") or mi.get("release_date") or (str(x.get("year")) if x.get("year") else None),
                              tid, ts_, json.dumps({"via": "youtube_description", "title": ts_}), ts))
                found["jiosaavn"] += 1
                break
        if len(ym) == 1 and not _has(conn, tid, "ytmusic"):
            try:
                o = json.loads(_text("https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": f"https://www.youtube.com/watch?v={ym[0]}", "format": "json"})))
            except (ValueError, TypeError):
                o = None
            time.sleep(1)
            if o and match.title_score(_song_title(conn, tid), match.parse_spotify_title(o.get("title") or "")[0]) >= 0.85:
                conn.execute("""INSERT INTO platform_item (platform, platform_id, title, url, channel, track_id, review_state, match_method,
                                    match_score, match_detail, first_seen_at)
                                VALUES ('ytmusic', ?, ?, ?, ?, ?, 'auto', 'description', NULL, ?, ?) ON CONFLICT(platform, platform_id) DO NOTHING""",
                             (ym[0], o.get("title"), f"https://music.youtube.com/watch?v={ym[0]}", o.get("author_name"), tid,
                              json.dumps({"via": "youtube_description"}), ts))
                found["ytmusic"] += 1
        for aid in ig[:3]:
            if not conn.execute("SELECT 1 FROM platform_item WHERE platform='instagram' AND platform_id=?", (aid,)).fetchone():
                instagram_audio.link(conn, tid, aid, who="youtube_description", method="description")
                found["instagram"] += 1
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"desc_links:{iid}", ts[:10]))
        conn.commit()
    if todo:
        log(f"Description links: {len(todo)} uploads scanned; linked " + ", ".join(f"{v} {k}" for k, v in found.items()))
    return {"desc_scanned": len(todo), **{f"desc_{k}": v for k, v in found.items()}}
