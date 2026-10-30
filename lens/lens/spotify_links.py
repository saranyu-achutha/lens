"""Spotify track IDs without spending Spotify's search quota, from links we already have. The ID is all we take
from these sources; release date, duration and plays come from the public Spotify page (spotify_plays), as for
every Spotify link.

  1. MusicBrainz: a recording already matched to the song often lists its open.spotify.com/track link among its
     URL relations (open data, free, 1 request a second).
  2. Odesli (song.link), only when ODESLI_API_KEY is set in .env (its keyless public API closed in 2026): the song's
     Apple Music, Deezer, YouTube Music or YouTube link -> the same song on Spotify. A candidate is kept only when
     its title and artists match the song by the usual rules (lens.match, automatic matches only).

Songs tried without a result are retried after 7 days. Whatever is left goes to the Spotify search as before
(songs.spotify_lookup skips every song that already has a Spotify link).
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import match

TRACK_ID = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]{22})")
ODESLI = "https://api.song.link/v1-alpha.1/links"
SOURCE_ORDER = ("apple", "deezer", "ytmusic", "youtube")  # most exact first


def _get(url, headers=None):
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                return {"_status": e.code}
            time.sleep(10 if e.code in (429, 503) else 3)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(3)
    return None


def _vids(conn, tid):
    return conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                           WHERE track_id=? AND platform='youtube' ORDER BY asset_type IN ('audio','lyric') DESC,
                           published_at LIMIT 3""", (tid,)).fetchall()


def _score(tr, vids):
    best = None
    for v in vids:
        total, detail = match.score(tr, {"title": v["title"], "description": v["description"], "published_at": v["published_at"],
                                         "duration_s": (v["duration_ms"] or 0) / 1000 or None})
        if best is None or total > best[0]:
            best = (total, detail)
    return best if best and match.decide(*best) == "auto" else None


def run(conn, cfg, secrets, since, until, ts, log, check):
    today = ts[:10]
    key = (secrets.get("ODESLI_API_KEY") or "").strip()
    mb_contact = (cfg["songs"].get("musicbrainz_contact") or "").strip()
    ua = {"User-Agent": f"MarketLens/0.1 ( {mb_contact} )" if mb_contact else "MarketLens/0.1"}
    todo = conn.execute(
        """SELECT t.id, coalesce(t.yt_title, t.title) title FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND coalesce(t.release_check, 'unknown') != 'old' AND coalesce(t.release_status, '') != 'catalogue'
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')
             AND coalesce((SELECT value FROM state WHERE key='spotify_links:' || t.id), '') < ?
             AND (EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='musicbrainz') OR ?)
           ORDER BY t.first_published DESC LIMIT ?""",
        (since, until, (date.today() - timedelta(days=7)).isoformat(), 1 if key else 0,
         cfg["songs"].get("spotify_links_per_run", 300))).fetchall()
    if not todo:
        return {}
    log(f"Spotify links from MusicBrainz{' and Odesli' if key else ''}: {len(todo)} songs without a Spotify link"
        + ("" if key else " (Odesli off: no ODESLI_API_KEY)"))
    found = {"musicbrainz": 0, "odesli": 0}
    pace = cfg["songs"].get("odesli_pace_seconds", 6.5)

    def attach(tid, sid, method, score, detail, title=None):
        if conn.execute("SELECT 1 FROM platform_item WHERE platform='spotify' AND platform_id=?", (sid,)).fetchone():
            return False  # already another song's (or this song's) Spotify track
        conn.execute("""INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                            match_score, match_detail, review_state, first_seen_at)
                        VALUES ('spotify', ?, ?, 'audio', ?, ?, ?, ?, ?, 'auto', ?)""",
                     (sid, tid, title, f"https://open.spotify.com/track/{sid}", method, score, json.dumps(detail), ts))
        return True

    for n, song in enumerate(todo, 1):
        check()
        done = False
        # 1. MusicBrainz recording -> its Spotify link (the recording was already matched to this song)
        for rec, score in conn.execute("SELECT platform_id, match_score FROM platform_item WHERE track_id=? AND platform='musicbrainz'",
                                       (song["id"],)).fetchall():
            d = _get(f"https://musicbrainz.org/ws/2/recording/{rec}?inc=url-rels&fmt=json", ua) or {}
            time.sleep(1.1)
            for rel in d.get("relations") or []:
                m = TRACK_ID.search((rel.get("url") or {}).get("resource") or "")
                if m and attach(song["id"], m.group(1), "musicbrainz_link", score, {"via": "musicbrainz", "recording": rec}, d.get("title")):
                    found["musicbrainz"] += 1
                    done = True
                    break
            if done:
                break
        # 2. Odesli: one of the song's own platform links -> Spotify (only with a key)
        if not done and key:
            vids = _vids(conn, song["id"])
            links = conn.execute(f"""SELECT platform, url FROM platform_item WHERE track_id=? AND url IS NOT NULL
                                     AND platform IN ({','.join('?' * len(SOURCE_ORDER))})
                                     AND review_state IN ('auto','approved','song')""", (song["id"], *SOURCE_ORDER)).fetchall()
            links.sort(key=lambda r: SOURCE_ORDER.index(r["platform"]))
            for plat, url in links[:2]:  # the two most exact links: enough, and keeps within Odesli's pace
                d = _get(f"{ODESLI}?{urllib.parse.urlencode({'url': url, 'userCountry': 'IN', 'key': key})}")
                time.sleep(pace)
                if not d or d.get("_status") in (401, 403):
                    if d and d.get("_status") in (401, 403):
                        log("Odesli refused the key; stopping Odesli for this run")
                        key = ""
                    break
                sp = (d.get("linksByPlatform") or {}).get("spotify")
                ent = (d.get("entitiesByUniqueId") or {}).get(sp["entityUniqueId"]) if sp else None
                m = TRACK_ID.search((sp or {}).get("url") or "")
                if not (m and ent):
                    continue
                tr = {"title": ent.get("title") or "", "artists": [a.strip() for a in re.split(r",|&", ent.get("artistName") or "") if a.strip()],
                      "release_title": None, "release_date": None, "duration_ms": None}
                ok = _score(tr, vids)
                if ok and attach(song["id"], m.group(1), "odesli", ok[0], dict(ok[1], via=f"odesli:{plat}", candidate_title=tr["title"],
                                                                                  candidate_artists=tr["artists"]), tr["title"]):
                    found["odesli"] += 1
                    done = True
                    break
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"spotify_links:{song['id']}", today))
        if n % 10 == 0:
            conn.commit()
        if n % 50 == 0:
            log(f"  {n}/{len(todo)} tried ({found['musicbrainz']} from MusicBrainz, {found['odesli']} from Odesli)")
    conn.commit()
    log(f"Spotify links without search: {found['musicbrainz']} from MusicBrainz, {found['odesli']} from Odesli, "
        f"{len(todo) - sum(found.values())} left for the Spotify search")
    return {"spotify_links_musicbrainz": found["musicbrainz"], "spotify_links_odesli": found["odesli"]}
