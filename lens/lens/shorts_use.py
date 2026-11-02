"""Shorts made with each song's official audio: YouTube's public "Shorts using this sound" page
(youtube.com/source/<video id>/shorts), read once a day per sound, with no key or quota.

Only the labels' own audio counts: the sound pages of the song's official uploads on the label channels (video, audio,
lyric), and a sound counts only when the page names a label channel (config youtube.channels) as its owner. Shorts using
a sound someone else uploaded or made (a re-upload, "original audio", a YouTube Music art track's page, which names other
channels and lists unrelated Shorts) are not counted.

  sound_use    one row per sound per day: the Shorts count YouTube shows ("623 Shorts", "5K Shorts"; rounded above
               1,000; no count shown = 0), the sound's name and owner channel, and the views of the Shorts listed
               (about the top 24). official=0 rows (owner not a label channel) are kept but never counted.
  sound_short  every Short seen on a sound page: title, latest views, first and last day seen.
song_daily sums a song's sounds into yt_shorts (and yt_shorts_gain); weekly, monthly and yearly views sit on top.
Instagram has a `platform` slot in sound_use but no reader: its audio pages need a login and Meta has no API for them.
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date

from . import config, db

PAGE = "https://www.youtube.com/source/{}/shorts"
PACE = 2.0  # seconds between pages
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
OFFICIAL_TYPES = ("video", "audio", "lyric")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sound_use (
    platform_item_id INTEGER NOT NULL,   -- the official upload / audio track whose sound is used
    day              TEXT NOT NULL,
    platform         TEXT NOT NULL DEFAULT 'youtube',
    song_id          INTEGER,
    shorts           INTEGER,            -- Shorts made with this sound, as YouTube shows it (0 when it shows none)
    official         INTEGER,            -- 1 when the sound's owner is a label channel: only these are counted
    count_text       TEXT,               -- as shown, e.g. "5K Shorts" (rounded above 1,000)
    listed           INTEGER,            -- Shorts listed on the page
    listed_views     INTEGER,            -- their views summed
    sound_title      TEXT,
    sound_owner      TEXT,               -- the owner's @handle
    owner_channel    TEXT,               -- the owner's channel ID
    captured_at      TEXT,
    PRIMARY KEY (platform_item_id, day)
);
CREATE INDEX IF NOT EXISTS sound_use_song ON sound_use(song_id, day);
CREATE TABLE IF NOT EXISTS sound_short (
    video_id         TEXT PRIMARY KEY,
    platform_item_id INTEGER,            -- the sound it uses
    song_id          INTEGER,
    title            TEXT,
    views            INTEGER,
    first_seen       TEXT,
    last_seen        TEXT
);
CREATE INDEX IF NOT EXISTS sound_short_song ON sound_short(song_id);
"""


class Cancelled(Exception):
    pass


def ensure(conn):
    conn.executescript(SCHEMA)


def number(text):
    """'623 Shorts' -> 623, '5K' -> 5000, '1.2M views' -> 1200000, 'No views' -> 0."""
    m = re.search(r"([\d.,]+)\s*([KMB]?)", text or "", re.I)
    if not m:
        return 0 if re.search(r"\bno\b", text or "", re.I) else None
    v = float(m.group(1).replace(",", ""))
    return int(round(v * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]))


def _walk(o, key, out):
    if isinstance(o, dict):
        if key in o:
            out.append(o[key])
        for v in o.values():
            _walk(v, key, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, out)
    return out


def parse(html):
    """What a sound page shows: title, owner, count (or None), and the listed Shorts. None if it isn't a sound page."""
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return None
    d = json.loads(m.group(1))
    hdr = (d.get("header") or {}).get("pageHeaderRenderer")
    if not hdr:
        return None
    texts = [t for t in _walk(hdr, "content", []) if isinstance(t, str)]
    count_text = next((t for t in texts if re.fullmatch(r"[\d.,]+[KMB]? Shorts?", t.strip(), re.I)), None)
    owner = next((t for t in texts if t.startswith("@")), None)
    owner_id = next(iter(re.findall(r'"browseId":\s*"(UC[\w-]+)"', json.dumps(hdr))), None)
    shorts = {}
    for x in _walk(d, "shortsLockupViewModel", []):
        vid = ((x.get("onTap") or {}).get("innertubeCommand") or {}).get("reelWatchEndpoint", {}).get("videoId")
        if not vid:
            continue
        acc = x.get("accessibilityText") or ""
        views = number(((x.get("overlayMetadata") or {}).get("secondaryText") or {}).get("content"))
        shorts[vid] = {"id": vid, "title": re.sub(r",\s*[\d.,KMBkmb]+ views?\s*[–-]\s*play Short$|\s*[–-]\s*play Short$", "", acc), "views": views}
    return {"title": hdr.get("pageTitle") or (texts[0] if texts else None), "owner": owner, "owner_id": owner_id,
            "count_text": count_text, "count": number(count_text) if count_text else None, "shorts": list(shorts.values())}


def fetch(video_id):
    req = urllib.request.Request(PAGE.format(video_id), headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.8"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            time.sleep(PACE)
            return parse(body)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # slow down instead of pushing
                time.sleep(60 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    return None


def due(conn, cfg, today):
    """Sounds of the period's songs without today's reading: their uploads on the label channels (not Shorts, teasers
    or jukeboxes)."""
    since, until = config.window(cfg)
    channels = list(config.channel_labels(cfg))
    return conn.execute(f"""
        SELECT p.id, p.platform_id, p.track_id FROM platform_item p JOIN track t ON t.id=p.track_id
        WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?
          AND p.platform='youtube' AND p.asset_type IN ({",".join("?" * len(OFFICIAL_TYPES))})
          AND p.review_state IN ('auto','approved','song') AND p.channel IN ({",".join("?" * len(channels))})
          AND NOT EXISTS (SELECT 1 FROM sound_use u WHERE u.platform_item_id=p.id AND u.day=?)
        ORDER BY t.first_published DESC""", (since, until, *OFFICIAL_TYPES, *channels, today)).fetchall()


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    ensure(conn)
    today = date.today().isoformat()
    label_ids = {c["id"] for c in cfg["youtube"]["channels"]}
    todo = due(conn, cfg, today)[:cfg["songs"].get("shorts_pages_per_run", 300)]
    if todo:
        log(f"Shorts: reading {len(todo)} official sound pages (about {len(todo) * (PACE + 1) / 60:.0f} min)")
    read = counted = failed = not_label = 0
    for n, (iid, vid, tid) in enumerate(todo, 1):
        if should_stop():
            raise Cancelled()
        try:
            p = fetch(vid)
        except Exception as e:  # one page failing doesn't stop the rest
            log(f"  {vid}: {e}")
            p = None
        if p is None:
            failed += 1
            continue
        official = p["owner_id"] in label_ids
        shorts = p["count"] or 0
        ts = db.now_iso()
        conn.execute("""INSERT OR REPLACE INTO sound_use (platform_item_id, day, platform, song_id, shorts, official, count_text, listed,
                            listed_views, sound_title, sound_owner, owner_channel, captured_at) VALUES (?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (iid, today, tid, shorts, int(official), p["count_text"], len(p["shorts"]),
                      sum(s["views"] or 0 for s in p["shorts"]), p["title"], p["owner"], p["owner_id"], ts))
        if not official:  # someone else's sound: kept for checking, its Shorts not stored
            read += 1
            not_label += 1
            conn.commit()
            continue
        for s in p["shorts"]:
            conn.execute("""INSERT INTO sound_short (video_id, platform_item_id, song_id, title, views, first_seen, last_seen)
                            VALUES (?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET views=excluded.views, title=excluded.title,
                            last_seen=excluded.last_seen""", (s["id"], iid, tid, s["title"][:300], s["views"], today, today))
        conn.commit()  # each page saved: never hold the database while waiting on the network
        read += 1
        counted += shorts > 0
        if n % 50 == 0:
            log(f"  {n}/{len(todo)} sounds read ({counted} used in Shorts)")
    if todo:
        log(f"Shorts: {read} sounds read, {counted} used in Shorts, {not_label} not owned by a label channel (not counted), "
            f"{failed} pages not readable")
    return {"status": "ok", "shorts_sounds_read": read, "shorts_sounds_used": counted, "shorts_not_label": not_label,
            "shorts_pages_failed": failed}
