"""Spotify track IDs without spending Spotify's search quota, from links we already have. The ID is all we take
from these sources; release date, duration and plays come from the public Spotify page (spotify_plays), as for
every Spotify link.

  1. MusicBrainz: a recording already matched to the song often lists its open.spotify.com/track link among its
     URL relations (open data, free, 1 request a second).
  2. Odesli (song.link), only when ODESLI_API_KEY is set in .env (its keyless public API closed in 2026): the song's
     Apple Music, Deezer, YouTube Music or YouTube link -> the same song on Spotify.
  3. Web search (songs.spotify_web_search): DuckDuckGo, then Bing, for "<song> <singer> spotify"; open.spotify.com
     track links in the results are candidates. Search engines index new Spotify pages late, so this mostly finds
     songs that are a few weeks old, and older songs re-uploaded (which the release-year check then marks older).
     Slow and polite (one search every few seconds); a blocked or empty search just moves on.
A candidate from Odesli or a search is read on Spotify's public embed page (title, artists, release date, duration;
no API, no browser) and kept only when it matches the song by the usual rules (lens.match, automatic matches only).

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

TRACK_ID = re.compile(r"open\.spotify\.com(?:/|%2F)(?:intl-[a-z]+(?:/|%2F))?track(?:/|%2F)([A-Za-z0-9]{22})")
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
              "Accept-Language": "en-IN,en;q=0.9"}
SEARCH_ENGINES = (("duckduckgo", "https://html.duckduckgo.com/html/?q={q}"), ("bing", "https://www.bing.com/search?q={q}&cc=IN"))
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


def _text(url, headers=BROWSER_UA):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as r:
            return r.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return ""


def embed_details(sid):
    """Title, artists, release date and duration of a Spotify track from its public embed page (no API, no browser)."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  _text(f"https://open.spotify.com/embed/track/{sid}"), re.S)
    try:
        e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return {"title": e.get("name") or e.get("title") or "", "artists": [a["name"] for a in e.get("artists") or [] if a.get("name")],
            "release_date": ((e.get("releaseDate") or {}).get("isoString") or "")[:10] or None, "duration_ms": e.get("duration")}


def web_candidates(query, pace):
    """Spotify track IDs from the first search engine that returns any (DuckDuckGo, then Bing)."""
    for name, url in SEARCH_ENGINES:
        ids = list(dict.fromkeys(TRACK_ID.findall(_text(url.format(q=urllib.parse.quote(query))))))
        time.sleep(pace)
        if ids:
            return name, ids[:3]
    return None, []


def attach(conn, tid, sid, method, score, detail, title, ts):
    """Link Spotify track `sid` to song `tid` (automatic match). False when that track is already linked."""
    if conn.execute("SELECT 1 FROM platform_item WHERE platform='spotify' AND platform_id=?", (sid,)).fetchone():
        return False
    conn.execute("""INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                        match_score, match_detail, review_state, first_seen_at)
                    VALUES ('spotify', ?, ?, 'audio', ?, ?, ?, ?, ?, 'auto', ?)""",
                 (sid, tid, title, f"https://open.spotify.com/track/{sid}", method, score, json.dumps(detail), ts))
    return True


BACKOFF_DAYS = (1, 3, 7, 14, 30)  # wait after the 1st, 2nd, 3rd ... try without a result
GIVE_UP_DAYS = 60                 # a song still not found 60 days after its first try is almost surely not on Spotify


def due(conn, since, until, today=None, limit=None, need_mb=False):
    """Songs without a Spotify link whose next try is due (back-off schedule), new releases first.
    The state value is "first try|last try|tries" (an older plain date counts as one try)."""
    today = date.fromisoformat(today or date.today().isoformat())
    rows = conn.execute(
        f"""SELECT t.id, coalesce(t.yt_title, t.title) title, coalesce(t.release_check, '') = 'old' old,
                  (SELECT value FROM song_meta m WHERE m.track_id=t.id AND m.field='singer' ORDER BY position LIMIT 1) singer,
                  (SELECT value FROM state WHERE key='spotify_links:' || t.id) st FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')
             {"AND EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='musicbrainz')" if need_mb else ""}
           ORDER BY old, t.first_published DESC""", (since, until)).fetchall()
    out = []
    for r in rows:
        if r["st"]:
            parts = r["st"].split("|")
            first, last, n = (parts + [parts[0], "1"])[:3] if len(parts) < 3 else parts
            first, last, n = date.fromisoformat(first[:10]), date.fromisoformat(last[:10]), int(n)
            if (today - first).days > GIVE_UP_DAYS or (today - last).days < BACKOFF_DAYS[min(n, len(BACKOFF_DAYS)) - 1]:
                continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def mark_tried(conn, tid, today):
    st = conn.execute("SELECT value FROM state WHERE key=?", (f"spotify_links:{tid}",)).fetchone()
    parts = (st[0].split("|") if st and st[0] else [])
    first = parts[0][:10] if parts else today
    n = int(parts[2]) + 1 if len(parts) == 3 else (2 if parts else 1)
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"spotify_links:{tid}", f"{first}|{today}|{n}"))


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
    web = cfg["songs"].get("spotify_web_search", True)
    mb_contact = (cfg["songs"].get("musicbrainz_contact") or "").strip()
    ua = {"User-Agent": f"MarketLens/0.1 ( {mb_contact} )" if mb_contact else "MarketLens/0.1"}
    per_run = cfg["songs"].get("spotify_links_per_run", 200)
    if web:  # adapt: when web search has found almost nothing for a week, spend less time on it
        tries, hits = conn.execute("""SELECT coalesce(sum(items_read), 0), coalesce(sum(found), 0) FROM crawl_stat
                                      WHERE source='spotify_web' AND day >= date('now', '-7 day')""").fetchone() \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='crawl_stat'").fetchone() else (0, 0)
        if tries >= 300 and hits / tries < 0.02:
            per_run = min(per_run, 50)
            log(f"Spotify web search: only {hits} links from {tries} searches in 7 days; {per_run} songs this run")
    todo = due(conn, since, until, today, per_run, need_mb=not (key or web))
    if not todo:
        return {}
    log(f"Spotify links from MusicBrainz{', Odesli' if key else ''}{' and web search' if web else ''}: {len(todo)} songs without a Spotify link"
        + ("" if key else " (Odesli off: no ODESLI_API_KEY)"))
    found = {"musicbrainz": 0, "odesli": 0, "web": 0}
    searches = 0
    pace = cfg["songs"].get("odesli_pace_seconds", 6.5)


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
                if m and attach(conn, song["id"], m.group(1), "musicbrainz_link", score, {"via": "musicbrainz", "recording": rec}, d.get("title"), ts):
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
                tr = embed_details(m.group(1)) or {"title": ent.get("title") or "", "release_date": None, "duration_ms": None,
                                                   "artists": [a.strip() for a in re.split(r",|&", ent.get("artistName") or "") if a.strip()]}
                tr["release_title"] = None
                ok = _score(tr, vids)
                if ok and attach(conn, song["id"], m.group(1), "odesli", ok[0], dict(ok[1], via=f"odesli:{plat}", candidate_title=tr["title"],
                                                                                  candidate_artists=tr["artists"]), tr["title"], ts):
                    found["odesli"] += 1
                    done = True
                    break
        # 3. Web search (DuckDuckGo, then Bing): candidates checked on the embed page and by the match rules
        if not done and web:
            searches += 1
            engine, ids = web_candidates(f'{song["title"]} {song["singer"] or ""} spotify'.strip(), cfg["songs"].get("web_search_pace_seconds", 5))
            vids = _vids(conn, song["id"]) if ids else []
            best = None
            for sid in ids:
                det = embed_details(sid)
                time.sleep(1)
                if not det:
                    continue
                ok = _score(dict(det, release_title=None), vids)
                if ok and (not best or ok[0] > best[0]):
                    best = (ok[0], ok[1], sid, det)
            if best and attach(conn, song["id"], best[2], "web_search", best[0],
                               dict(best[1], via=engine, candidate_title=best[3]["title"], candidate_artists=best[3]["artists"],
                                    candidate_release_date=best[3]["release_date"]), best[3]["title"], ts):
                conn.execute("UPDATE platform_item SET published_at=?, duration_ms=? WHERE platform='spotify' AND platform_id=?",
                             (best[3]["release_date"], best[3]["duration_ms"], best[2]))
                found["web"] += 1
                done = True
        mark_tried(conn, song["id"], today)
        if n % 10 == 0:
            conn.commit()
        if n % 50 == 0:
            log(f"  {n}/{len(todo)} tried ({found['musicbrainz']} from MusicBrainz, {found['odesli']} from Odesli, {found['web']} from web search)")
    conn.commit()
    log(f"Spotify links without the Spotify search: {found['musicbrainz']} from MusicBrainz, {found['odesli']} from Odesli, {found['web']} from web search, "
        f"{len(todo) - sum(found.values())} left for the Spotify search")
    return {"spotify_links_musicbrainz": found["musicbrainz"], "spotify_links_odesli": found["odesli"], "spotify_links_web": found["web"], "spotify_web_searches": searches}
