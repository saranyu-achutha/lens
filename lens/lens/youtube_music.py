"""YouTube Music: each song's audio track ("Art Track") and its play count, via the YouTube Data API.

YouTube Music plays are counted on auto-generated Art Track videos, uploaded to "<Artist> - Topic" channels with a
description that starts "Provided to YouTube by <label>". For each song of the period this step:
  1. searches once for its Art Track (search.list, 100 quota units): a result is kept only when it is on a
     "- Topic" channel, its title matches the song (title score >= 0.9, no remix/male/female-version mismatch),
     and the providing label or a credited artist matches the song. Up to `ytmusic_searches_per_run` songs a run,
     biggest songs first; songs not found are retried after 7 days. Quota note: the daily search cap and the
     10,000 units are per Google Cloud project, so keys only add capacity when each comes from its own project.
  2. reads the play count of every found track once a day (videos.list, 1 unit per 50 tracks), stored as
     snapshot metric 'plays' on platform 'ytmusic'.
"""
import html
import json
import re
from datetime import date, timedelta

from . import config, db, match
from .youtube import YouTube, YouTubeError, parse_keys

# The "Provided to YouTube by ..." name each tracked label uses (regex on the Art Track description).
PROVIDER = {"T-Series": r"t-?series|super cassettes", "Sony Music India": r"sony music", "Saregama": r"saregama",
            "Zee Music Company": r"zee music|zee entertainment", "Aditya Music": r"aditya music", "Think Music": r"think music",
            "Times Music": r"times music|junglee"}


class Cancelled(Exception):
    pass


def _todo(conn, since, until, today, limit):
    recheck = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    return conn.execute(
        """SELECT t.id, coalesce(t.yt_title, t.title) title,
                  (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) channel,
                  (SELECT sum(s.value) FROM platform_item p JOIN snapshot s ON s.platform_item_id=p.id AND s.metric='views'
                     AND s.capture_date=(SELECT max(capture_date) FROM snapshot x WHERE x.platform_item_id=p.id AND x.metric='views')
                   WHERE p.track_id=t.id AND p.platform='youtube') views
           FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='ytmusic')
             AND coalesce((SELECT value FROM state WHERE key='ytm_lookup:' || t.id), '') < ?
           ORDER BY views DESC NULLS LAST LIMIT ?""", (since, until, recheck, limit)).fetchall()


def _artists(conn, tid):
    """Credited names, YouTube-description credits first (Spotify artists can come from a wrong version)."""
    names = [r[0] for r in conn.execute("""SELECT value FROM song_meta WHERE track_id=? AND field IN ('singer','music_director')
                                           ORDER BY field='music_director', position""", (tid,))]
    names += [r[0] for r in conn.execute("""SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id WHERE ta.track_id=?
                                            ORDER BY ta.position""", (tid,))]
    return [n for n in dict.fromkeys(names) if n and len(match.fold(n)) > 3]


LANGS = ("hindi", "tamil", "telugu", "kannada", "malayalam", "bengali", "bangla", "marathi", "gujarati", "punjabi", "bhojpuri",
         "haryanvi", "odia", "assamese", "rajasthani")


def _lang_tag(title):
    """Language named in a title's brackets: 'Aaya Sher (From "The Paradise") (Hindi)' -> 'hindi'."""
    for part in re.findall(r"[(\[]([^)\]]+)[)\]]", title or ""):
        w = match.fold(part).strip()
        if w in LANGS:
            return "bengali" if w == "bangla" else w
    return None


def _song_lang(conn, tid):
    r = conn.execute("SELECT value FROM song_meta WHERE track_id=? AND field='language' ORDER BY position LIMIT 1", (tid,)).fetchone()
    v = match.fold(r[0]) if r and r[0] else None
    return "bengali" if v == "bangla" else v


def find_track(yt, song, label, artists, lang=None):
    """Best Art Track for a song, or None. Returns dict(video_id, title, channel, published, provider, evidence)."""
    core, _, quals = match.parse_spotify_title(song)
    q = f"{core} {artists[0]}" if artists else core
    res = yt.get("search", part="snippet", q=q, type="video", videoCategoryId="10", maxResults=10, regionCode="IN")
    best = None
    for it in res.get("items", []):
        sn = it["snippet"]
        ch = sn.get("channelTitle", "")
        if not ch.endswith(" - Topic"):
            continue
        title = match.parse_spotify_title(sn["title"])[0]
        ts = match.title_score(core, title)
        if ts < 0.9 or quals ^ match.recording_qualifiers(sn["title"]):
            continue
        tag = _lang_tag(sn["title"])
        if tag and lang and tag != lang:  # another language's version of the song
            continue
        desc = sn.get("description", "")
        prov = re.search(r"Provided to YouTube by\s+(.+?)(?:\s{2,}|\s*$)", desc)
        provider = prov.group(1).strip() if prov else ""
        label_ok = bool(label and PROVIDER.get(label) and re.search(PROVIDER[label], provider + " " + desc, re.I))
        topic_artist = ch[: -len(" - Topic")]
        artist_ok = any(match.fold(a) in match.fold(topic_artist) or match.fold(topic_artist) in match.fold(a) for a in artists)
        if not (label_ok or artist_ok):
            continue
        cand = {"video_id": it["id"]["videoId"], "title": html.unescape(sn["title"]), "channel": ch, "published": sn.get("publishedAt"),
                "provider": provider, "evidence": {"title": ts, "label": label_ok, "artist": artist_ok}}
        rank = (ts, label_ok + artist_ok)
        if not best or rank > best[0]:
            best = (rank, cand)
    return best[1] if best else None


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    keys = parse_keys(secrets.get("YOUTUBE_API_KEY"))
    if not keys:
        return {"status": "ok", "stop_reason": "no YouTube key"}
    since, until = config.window(cfg)
    today = date.today().isoformat()
    ts = db.now_iso()
    yt = YouTube(keys, log=log)
    labels = config.channel_labels(cfg)
    # A Google Cloud project allows ~100 searches a day (= its whole 10,000 units), and keys of one project share
    # that; keep searches small so the daily views reads always have quota left.
    budget = cfg["songs"].get("ytmusic_searches_per_run", 40)
    todo = _todo(conn, since, until, today, budget)
    found = missing = 0
    status, stop_reason = "ok", None
    if todo:
        log(f"YouTube Music: looking for the audio tracks of {len(todo)} songs (one search each, {len(todo) * 100:,} quota units)")
    try:
        for n, song in enumerate(todo, 1):
            if should_stop():
                raise Cancelled()
            hit = find_track(yt, song["title"], labels.get(song["channel"]), _artists(conn, song["id"]), _song_lang(conn, song["id"]))
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"ytm_lookup:{song['id']}", today))
            if hit:
                conn.execute("""INSERT INTO platform_item (platform, platform_id, title, url, published_at, channel, track_id,
                                    review_state, match_method, match_score, match_detail, first_seen_at)
                                VALUES ('ytmusic', ?, ?, ?, ?, ?, ?, 'auto', 'search', ?, ?, ?)
                                ON CONFLICT(platform, platform_id) DO NOTHING""",
                             (hit["video_id"], hit["title"], f"https://music.youtube.com/watch?v={hit['video_id']}", hit["published"],
                              hit["channel"], song["id"], hit["evidence"]["title"], json.dumps(hit), ts))
                found += 1
            else:
                missing += 1
            if n % 25 == 0:
                conn.commit()
                log(f"  {n}/{len(todo)} searched ({found} found)")
    except YouTubeError as e:
        if not e.quota_exceeded:
            raise
        status, stop_reason = "partial", "all keys reached their daily search limit; the remaining songs are searched in later runs"
        log(f"⚠ {stop_reason}")
    conn.commit()
    if todo:
        log(f"YouTube Music: {found} audio tracks found, {missing} not found (retried in 7 days)")

    # Daily play counts of every known track of the period.
    due = [r[0] for r in conn.execute(
        """SELECT p.platform_id FROM platform_item p JOIN track t ON t.id=p.track_id
           WHERE p.platform='ytmusic' AND t.first_published >= ?
             AND NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='plays' AND s.capture_date=?)""",
        (since, today))]
    read = 0
    if due:  # play counts use videos.list, which the search limit does not affect
        for i in range(0, len(due), 50):
            for v in yt.videos(due[i:i + 50]):
                st = v.get("statistics", {})
                if "viewCount" not in st:
                    continue
                item = conn.execute("SELECT id FROM platform_item WHERE platform='ytmusic' AND platform_id=?", (v["id"],)).fetchone()
                if not item:
                    continue
                val = int(st["viewCount"])
                conn.execute("INSERT INTO reading (platform_item_id, metric, value, captured_at) VALUES (?, 'plays', ?, ?)", (item[0], val, ts))
                conn.execute("""INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at) VALUES (?, 'plays', ?, ?, ?)
                                ON CONFLICT(platform_item_id, metric, capture_date) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                             (item[0], val, today, ts))
                read += 1
        conn.commit()
        log(f"YouTube Music: play counts read for {read} tracks")
    total = conn.execute("SELECT count(DISTINCT track_id) FROM platform_item WHERE platform='ytmusic'").fetchone()[0]
    return {"status": status, "stop_reason": stop_reason, "ytmusic_found": found, "ytmusic_missing": missing,
            "ytmusic_read": read, "ytmusic_songs": total, "ytmusic_quota_units": yt.units}
