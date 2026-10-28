"""JioSaavn: each song's JioSaavn track and its play count, from the JSON the jiosaavn.com website itself uses
(public, no login or key). Requests are paced (about one a second) like a visitor's browsing.

  1. search once per song ("<song> <artist>"): a result is kept only when its title matches (score >= 0.9, no
     remix/male/female-version mismatch), its language matches the song's, and its label or a credited artist
     matches. Up to `jiosaavn_searches_per_run` songs a run, biggest songs first; misses are retried after 7 days.
  2. read every found track's play count once a day, 50 tracks per request (snapshot metric 'plays',
     platform 'jiosaavn').
"""
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import config, db, match
from .youtube_music import PROVIDER, _artists, _lang_tag, _song_lang

API = "https://www.jiosaavn.com/api.php"
PACE = 1.0  # seconds between requests


class Cancelled(Exception):
    pass


def _get(call, **params):
    url = API + "?" + urllib.parse.urlencode(dict(__call=call, _format="json", _marker=0, api_version=4, ctx="web6dot0", **params))
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            time.sleep(PACE)
            return body
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # slow down instead of pushing
                time.sleep(30 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(5 * (attempt + 1))
    return {}


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
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='jiosaavn')
             AND coalesce((SELECT value FROM state WHERE key='js_lookup:' || t.id), '') < ?
           ORDER BY views DESC NULLS LAST LIMIT ?""", (since, until, recheck, limit)).fetchall()


def _candidates(res, core, quals, label, artists, lang):
    out = []
    for x in res:
        if x.get("type") != "song":
            continue
        ts = match.title_score(core, match.parse_spotify_title(x.get("title", ""))[0])
        if ts < 0.9 or quals ^ match.recording_qualifiers(x.get("title", "")):
            continue
        tag = _lang_tag(x.get("title", ""))
        if tag and lang and tag != lang:  # title names another language: a different version
            continue
        mi = x.get("more_info") or {}
        names = [a.get("name", "") for grp in (mi.get("artistMap") or {}).values() if isinstance(grp, list) for a in grp] + [x.get("subtitle", "")]
        label_ok = bool(label and PROVIDER.get(label) and re.search(PROVIDER[label], mi.get("label", "") + " " + mi.get("copyright_text", ""), re.I))
        artist_ok = any(match.fold(a) and any(match.fold(a) in match.fold(n) for n in names) for a in artists)
        if not (label_ok or artist_ok):
            continue
        try:
            plays = int(x.get("play_count") or 0)
        except ValueError:
            plays = 0
        out.append({"id": x["id"], "title": html.unescape(x.get("title") or ""), "url": x.get("perma_url"), "label": mi.get("label"),
                    "release_date": mi.get("release_date"), "language": match.fold(x.get("language") or ""), "plays": plays,
                    "evidence": {"title": ts, "label": label_ok, "artist": artist_ok}})
    return out


def find_track(song, label, artists, lang=None):
    """Best JioSaavn track for a song, or None. A second, title-only search runs when the first finds nothing or only
    a little-played match (credits can name the wrong artist). Language: when the song exists on JioSaavn in several
    languages it must be ours; a single-language song is accepted even if our language metadata differs."""
    core, _, quals = match.parse_spotify_title(song)
    cands = {}
    queries = [f"{core} {artists[0]}" if artists else core] + ([core] if artists else [])
    for i, q in enumerate(queries):
        for c in _candidates(_get("search.getResults", q=q, n=10, p=1).get("results") or [], core, quals, label, artists, lang):
            cands[c["id"]] = c
        if i == 0 and cands and max(c["plays"] for c in cands.values()) >= 10000:
            break
    if not cands:
        return None
    langs = {c["language"] for c in cands.values() if c["language"]}
    for c in cands.values():
        c["evidence"]["language"] = not lang or not c["language"] or c["language"] == lang
    if lang and len(langs) > 1:  # several language versions: only ours
        cands = {k: c for k, c in cands.items() if c["evidence"]["language"]}
    if not cands:
        return None
    return max(cands.values(), key=lambda c: (c["evidence"]["language"], c["evidence"]["title"],
                                             c["evidence"]["label"] + c["evidence"]["artist"], c["plays"]))


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    since, until = config.window(cfg)
    today = date.today().isoformat()
    ts = db.now_iso()
    labels = config.channel_labels(cfg)
    todo = _todo(conn, since, until, today, cfg["songs"].get("jiosaavn_searches_per_run", 400))
    found = missing = 0
    if todo:
        log(f"JioSaavn: looking for {len(todo)} songs (one search each, about {len(todo) * PACE / 60:.0f} min)")
    for n, song in enumerate(todo, 1):
        if should_stop():
            raise Cancelled()
        hit = find_track(song["title"], labels.get(song["channel"]), _artists(conn, song["id"]), _song_lang(conn, song["id"]))
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"js_lookup:{song['id']}", today))
        if hit:
            conn.execute("""INSERT INTO platform_item (platform, platform_id, title, url, published_at, track_id, review_state,
                                match_method, match_score, match_detail, first_seen_at)
                            VALUES ('jiosaavn', ?, ?, ?, ?, ?, 'auto', 'search', ?, ?, ?) ON CONFLICT(platform, platform_id) DO NOTHING""",
                         (hit["id"], hit["title"], hit["url"], hit["release_date"], song["id"], hit["evidence"]["title"], json.dumps(hit), ts))
            found += 1
        else:
            missing += 1
        conn.commit()  # save each song: never hold the database while waiting on the network
        if n % 25 == 0:
            log(f"  {n}/{len(todo)} searched ({found} found)")
    conn.commit()
    if todo:
        log(f"JioSaavn: {found} tracks found, {missing} not found (retried in 7 days)")

    # Daily play counts, 50 tracks per request.
    items = conn.execute("""SELECT p.id, p.platform_id FROM platform_item p JOIN track t ON t.id=p.track_id
                            WHERE p.platform='jiosaavn' AND t.first_published >= ?
                              AND NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='plays' AND s.capture_date=?)""",
                         (since, today)).fetchall()
    by_pid = {r[1]: r[0] for r in items}
    pids, read = list(by_pid), 0
    for i in range(0, len(pids), 50):
        if should_stop():
            raise Cancelled()
        d = _get("song.getDetails", pids=",".join(pids[i:i + 50]))
        songs = d.get("songs") or [v for v in d.values() if isinstance(v, dict) and v.get("id")]
        for x in songs:
            iid, pc = by_pid.get(x.get("id")), x.get("play_count")
            if iid is None or pc in (None, ""):
                continue
            val = int(pc)
            conn.execute("INSERT INTO reading (platform_item_id, metric, value, captured_at) VALUES (?, 'plays', ?, ?)", (iid, val, ts))
            conn.execute("""INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at) VALUES (?, 'plays', ?, ?, ?)
                            ON CONFLICT(platform_item_id, metric, capture_date) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                         (iid, val, today, ts))
            read += 1
        conn.commit()
    if pids:
        log(f"JioSaavn: play counts read for {read} of {len(pids)} tracks")
    total = conn.execute("SELECT count(DISTINCT track_id) FROM platform_item WHERE platform='jiosaavn'").fetchone()[0]
    return {"status": "ok", "jiosaavn_found": found, "jiosaavn_missing": missing, "jiosaavn_read": read, "jiosaavn_songs": total}
