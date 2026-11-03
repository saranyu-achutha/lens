"""YouTube Music: each song's audio track ("Art Track") and its play count, via the YouTube Data API.

YouTube Music plays are counted on auto-generated Art Track videos, uploaded to "<Artist> - Topic" channels with a
description that starts "Provided to YouTube by <label>". For each song of the period this step:
  1. searches once for its Art Track (search.list, 100 quota units): a result is kept only when it is on a
     "- Topic" channel, its title matches the song (title score >= 0.9, no remix/male/female-version mismatch),
     and the providing label or a credited artist matches the song. Up to `ytmusic_searches_per_run` songs a run,
     never-searched songs first, then the biggest; a song not found is tried again after 7, 14 and 30 days and not
     after 60 days from its first try (most quota used to go on re-searching misses). Quota note: the daily search cap
     and the 10,000 units are per Google Cloud project, so keys only add capacity when each comes from its own project.
  2. reads the play count of every found track once a day (videos.list, 1 unit per 50 tracks), stored as
     snapshot metric 'plays' on platform 'ytmusic'; the same call names each track's "<Artist> - Topic" channel.
  3. scans those Topic channels (and album playlists linked in the labels' descriptions) for the songs still missing:
     listing a channel's newest uploads costs 1 unit per 50 tracks against 100 units for one search. Only channels whose
     artist has songs still missing are listed; the first time back to the period start, then the newest page a day.
     Uploads are matched with the same rules as a search result (title score >= 0.9, versions and language, artist).
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


BACKOFF_DAYS = (7, 14, 30)  # wait after the 1st, 2nd, 3rd search without a result
GIVE_UP_DAYS = 60
GENERIC_TOPICS = ("release - topic", "various artists - topic")  # catch-all channels, too big to list


def _tries(value):
    """state 'ytm_lookup:<song>' -> (first try, last try, tries); an older plain date counts as one try."""
    if not value:
        return None
    parts = value.split("|")
    return (parts[0], parts[0], 1) if len(parts) == 1 else (parts[0], parts[1], int(parts[2]))


def _todo(conn, since, until, today, limit):
    """Songs due a search: never searched first (biggest first), then misses whose wait is over."""
    rows = conn.execute(
        """SELECT t.id, coalesce(t.yt_title, t.title) title, (SELECT value FROM state WHERE key='ytm_lookup:' || t.id) tried,
                  (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) channel,
                  (SELECT sum(s.value) FROM platform_item p JOIN snapshot s ON s.platform_item_id=p.id AND s.metric='views'
                     AND s.capture_date=(SELECT max(capture_date) FROM snapshot x WHERE x.platform_item_id=p.id AND x.metric='views')
                   WHERE p.track_id=t.id AND p.platform='youtube') views
           FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='ytmusic')
           ORDER BY views DESC NULLS LAST""", (since, until)).fetchall()
    d = date.fromisoformat(today)
    fresh, retry = [], []
    for r in rows:
        t = _tries(r["tried"])
        if t is None:
            fresh.append(r)
        elif (d - date.fromisoformat(t[0][:10])).days < GIVE_UP_DAYS \
                and (d - date.fromisoformat(t[1][:10])).days >= BACKOFF_DAYS[min(t[2], len(BACKOFF_DAYS)) - 1]:
            retry.append(r)
    return (fresh + retry)[:limit]


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
            t = _tries(song["tried"])
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)",
                         (f"ytm_lookup:{song['id']}", f"{t[0] if t else today}|{today}|{(t[2] if t else 0) + 1}"))
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
        log(f"YouTube Music: {found} audio tracks found, {missing} not found (tried again after 7, 14, 30 days)")

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
                ch = (v.get("snippet") or {}).get("channelTitle") or ""
                if ch.endswith(" - Topic") and ch.lower() not in GENERIC_TOPICS:  # remember the artist's Topic channel (no extra cost)
                    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"ytm_topic:{v['snippet']['channelId']}", ch))
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
    scanned = {}
    try:
        scanned = scan_channels(conn, yt, cfg, since, until, today, ts, labels, log, should_stop)
    except YouTubeError as e:
        if not e.quota_exceeded:
            raise
        log("YouTube Music: quota reached during the Topic-channel scan; it continues in the next run")
    total = conn.execute("SELECT count(DISTINCT track_id) FROM platform_item WHERE platform='ytmusic'").fetchone()[0]
    return {"status": status, "stop_reason": stop_reason, "ytmusic_found": found + scanned.get("found", 0), "ytmusic_missing": missing,
            "ytmusic_read": read, "ytmusic_songs": total, "ytmusic_quota_units": yt.units,
            "ytmusic_topic_found": scanned.get("found", 0), "ytmusic_topic_pages": scanned.get("pages", 0)}


def _match_upload(sn, songs):
    """The song (of `songs`) an Art Track upload is, or None: same rules as a search result."""
    raw = sn.get("title") or ""
    title = match.parse_spotify_title(raw)[0]
    tag = _lang_tag(raw)
    best = None
    for s in songs:
        ts = match.title_score(s["core"], title)
        if ts < 0.9 or s["quals"] ^ match.recording_qualifiers(raw) or (tag and s["lang"] and tag != s["lang"]):
            continue
        if not best or ts > best[0]:
            best = (ts, s)
    return best


def _attach(conn, s, vid, sn, method, score, ts):
    conn.execute("""INSERT INTO platform_item (platform, platform_id, title, url, published_at, channel, track_id,
                        review_state, match_method, match_score, match_detail, first_seen_at)
                    VALUES ('ytmusic', ?, ?, ?, ?, ?, ?, 'auto', ?, ?, ?, ?) ON CONFLICT(platform, platform_id) DO NOTHING""",
                 (vid, html.unescape(sn.get("title") or ""), f"https://music.youtube.com/watch?v={vid}", sn.get("publishedAt"),
                  sn.get("videoOwnerChannelTitle") or sn.get("channelTitle"), s["id"], method, score, json.dumps({"via": method, "title": score}), ts))


def scan_channels(conn, yt, cfg, since, until, today, ts, labels, log, should_stop):
    """Find missing songs in the Topic channels of their artists and in album playlists the labels link (cheap lists)."""
    rows = conn.execute("""SELECT t.id, coalesce(t.yt_title, t.title) title FROM track t
                           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='ytmusic')""",
                        (since, until)).fetchall()
    songs = []
    for r in rows:
        core, _, quals = match.parse_spotify_title(r["title"])
        songs.append({"id": r["id"], "core": core, "quals": quals, "lang": _song_lang(conn, r["id"]),
                      "artists": [match.fold(a) for a in _artists(conn, r["id"])]})
    if not songs:
        return {}
    found = pages = 0
    linked = set()
    # 1. Album playlists linked in the labels' descriptions (each listed once)
    for tid, desc in conn.execute("""SELECT track_id, description FROM platform_item WHERE platform='youtube' AND track_id IS NOT NULL
                                     AND description LIKE '%OLAK5uy%'""").fetchall():
        for pid in set(re.findall(r"list=(OLAK5uy_[\w-]{20,})", desc or "")):
            if conn.execute("SELECT 1 FROM state WHERE key=?", (f"ytm_album:{pid}",)).fetchone():
                continue
            try:
                res = yt.get("playlistItems", part="snippet", playlistId=pid, maxResults=50)
            except YouTubeError as e:
                if e.status != 404:
                    raise
                res = {}  # the label removed the album playlist; remember it as listed
            pages += 1
            for it in res.get("items", []):
                sn = it["snippet"]
                hit = _match_upload(sn, [s for s in songs if s["id"] == tid and s["id"] not in linked])  # the song whose description links it
                if hit:
                    _attach(conn, hit[1], sn["resourceId"]["videoId"], sn, "album_playlist", hit[0], ts)
                    linked.add(hit[1]["id"])
                    found += 1
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"ytm_album:{pid}", today))
            conn.commit()
    # 2. Artists' Topic channels, only those with songs still missing; full back to the period start once, then a page a day
    for key, ch in conn.execute("SELECT key, value FROM state WHERE key LIKE 'ytm_topic:%'").fetchall():
        if should_stop():
            raise Cancelled()
        artist = match.fold(ch[: -len(" - Topic")])
        mine = [s for s in songs if s["id"] not in linked and any(artist in a or a in artist for a in s["artists"] if a)]
        if not mine or len(artist) < 4:
            continue
        cid = key.split(":", 1)[1]
        last = conn.execute("SELECT value FROM state WHERE key=?", (f"ytm_topic_scan:{cid}",)).fetchone()
        if last and last[0] == today:
            continue
        token = None
        while True:
            try:
                res = yt.uploads(cid, token)
            except YouTubeError as e:
                if e.status != 404:
                    raise
                break  # the Topic channel is gone; tried again tomorrow
            pages += 1
            old = False
            for it in res.get("items", []):
                sn = it["snippet"]
                if (it.get("contentDetails", {}).get("videoPublishedAt") or sn.get("publishedAt") or "")[:10] < since:
                    old = True
                    continue
                hit = _match_upload(sn, [s for s in mine if s["id"] not in linked])
                if hit:
                    _attach(conn, hit[1], it["contentDetails"]["videoId"], sn, "topic_channel", hit[0], ts)
                    linked.add(hit[1]["id"])
                    found += 1
            token = res.get("nextPageToken")
            if not token or old or last:  # after the first full scan, the newest page is enough
                break
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"ytm_topic_scan:{cid}", today))
        conn.commit()
    if pages:
        log(f"YouTube Music: {found} audio tracks found by listing {pages} Topic-channel / album pages ({pages} quota units)")
    return {"found": found, "pages": pages}
