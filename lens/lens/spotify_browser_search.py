"""Spotify IDs from a logged-out search on open.spotify.com (headless browser, no API, no quota).

The query cleaning comes from the laptop's spotify_browser_search.py (2026-11-05), which found Spotify tracks for many
songs the API search had missed: YouTube titles such as "ચમચમતી ચણિયાચોળી I Cham Chamti Chaniyacholi I Vijay Suvada I
Love" are split at " I ", "|", " - ", native-script words are dropped when a Latin spelling is there, and the first two
parts (title, usually the singer) are searched: "Cham Chamti Chaniyacholi Vijay Suvada".

Worker job `spotify_browser_search` (browser lane, songs.spotify_browser_search_*): the period's songs without a Spotify link that
have not been searched this way within the recheck days, newest first, one page every few seconds. For each, the top
results whose own title shares a word with the song are read from the public embed page (open.spotify.com/embed/track/<id>:
title, artist IDs, duration, release date; plain HTTP, no quota) and scored with the usual rules against the song's videos.
  * a match released from 30 days before the first YouTube upload on: queued in spotify_found; the label sweep then makes
    one /tracks/{id} call (album, ISRC) and links it or sends it to Review, as for an imported list
  * the best title match is an older release (most likely another song with the same name): kept for reference only
  * nothing: kept as not found
Each searched song counts as searched today (no API search of it before the recheck days). Stops after 6 empty pages
in a row (Spotify may be blocking); never logs in.
"""
import json
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, db, match, songs, spotify_found
from .spotify_plays import BLOCK, _WS, _kill_group, find_chrome

JOB = "spotify_browser_search"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
SEARCH_JS = """(() => {
  const rows = [...document.querySelectorAll('[data-testid="tracklist-row"]')].map((r) => {
    const a = r.querySelector('a[href*="/track/"]');
    if (!a) return null;
    const id = (a.getAttribute('href').split('/track/')[1] || '').slice(0, 22);
    return {id, name: a.innerText.trim(),
            artists: [...new Set([...r.querySelectorAll('a[href*="/artist/"]')].map((x) => x.innerText.trim()))]};
  }).filter(Boolean);
  const text = document.body ? document.body.innerText : '';
  return JSON.stringify({path: location.pathname, rows, empty: /No results found/i.test(text),
                         loaded: !!document.querySelector('main')}); })()"""


def setting(cfg, key, default):
    return cfg["songs"].get(f"spotify_browser_search_{key}", default)


def _latin(t):
    return re.search(r"[A-Za-z]", t) is not None


def song_parts(title):
    """'દ્વારિકા નો નાથ I Dwarika No Nath I Gaurav Dhola' -> ['Dwarika No Nath', 'Gaurav Dhola']"""
    t = re.sub(r"\((?:official|full|lyric|video|audio)[^)]*\)", " ", title or "", flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[I|｜]\s+|\s*\|\s*|\s+-\s+|\s+–\s+", t) if p.strip()]
    latin = [p for p in parts if _latin(p)]
    if latin:  # Latin text glued to native script: keep only the Latin words
        return [" ".join(w for w in p.split() if _latin(w) or w.isdigit()) or p for p in latin]
    return parts or [title or ""]


def query(title):
    """The search text: the song's first two parts (title, usually the singer)."""
    return " ".join(song_parts(title)[:2])[:100]


def embed(tid):
    """A track's public details (no API): {id, name, artists [{name, id}], duration_ms, release_date, explicit}, {} if gone,
    None if the page could not be read."""
    req = urllib.request.Request(f"https://open.spotify.com/embed/track/{tid}", headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', h, re.S)
            e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"] if m else {}
            return {"id": e.get("id") or tid, "name": e.get("name") or e.get("title"),
                    "artists": [{"name": a["name"], "id": (a.get("uri") or "").rsplit(":", 1)[-1]} for a in e.get("artists") or []],
                    "duration_ms": e.get("duration"), "explicit": e.get("isExplicit"),
                    "release_date": ((e.get("releaseDate") or {}).get("isoString") or "")[:10]} if e.get("name") or e.get("title") else {}
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return {}
            time.sleep(int(err.headers.get("Retry-After") or 0) or 5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            time.sleep(3 * (attempt + 1))
    return None


def due(conn, cfg, limit=None):
    """The period's songs without a Spotify link, not searched here within the recheck days, newest first."""
    spotify_found.ensure(conn)
    since, until = config.window(cfg)
    before = (datetime.now(timezone.utc) - timedelta(days=float(setting(cfg, "recheck_days", 7)))).isoformat()
    return conn.execute(
        """SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) first_day,
                  (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) channel
           FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')
             AND NOT EXISTS (SELECT 1 FROM spotify_found f WHERE f.song_id=t.id
                             AND (f.outcome IN ('queued','linked','review') OR f.imported_at >= ?))
           ORDER BY t.first_published DESC LIMIT ?""", (since, until, before, limit or -1)).fetchall()


def _choose(vids, song, rows, log=None):
    """(outcome, best candidate or None, score): the best result by the match rules, read from its embed page."""
    keys = match.block_keys(song_parts(song["title"])[0]) | match.block_keys(song["title"])
    new_from = (date.fromisoformat(song["first_day"]) - timedelta(days=spotify_found.NEW_BEFORE_DAYS)).isoformat()
    best_new, best_old = None, None
    for r in rows[:5]:
        if not (match.block_keys(match.parse_spotify_title(r["name"])[0]) & keys):
            continue  # its own title shares no word with the song (only a film name or a singer, at most)
        e = embed(r["id"])
        time.sleep(0.5)
        if not e:
            continue
        track = {"title": e["name"], "artists": [a["name"] for a in e["artists"]], "release_title": None,
                 "release_date": e["release_date"], "duration_ms": e["duration_ms"]}
        best = None
        for v in vids:
            s, detail = match.score(track, {"title": v["title"], "description": v["description"], "published_at": v["published_at"],
                                            "duration_s": (v["duration_ms"] or 0) / 1000 or None})
            if best is None or s > best[0]:
                best = (s, detail)
        if not best:
            continue
        if not match.decide(*best):
            continue  # not this song by the match rules, whatever its date
        if e["release_date"] and e["release_date"] < new_from:
            if best_old is None or best[0] > best_old[0]:
                best_old = (best[0], e)
        elif best_new is None or best[0] > best_new[0]:
            best_new = (best[0], e)
    if best_new:
        return "queued", best_new[1], best_new[0]
    if best_old:
        return "older_release", best_old[1], best_old[0]
    return "not_found", None, None


def start_browser(chrome, tabs=1):
    """A light headless browser (low CPU priority, no images) with its DevTools port: (process, port, profile folder)."""
    profile = tempfile.mkdtemp(prefix="lens_sps_")
    port = 9900 + os.getpid() % 90
    env = dict(os.environ, LD_LIBRARY_PATH=str(Path(chrome).parent))
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--mute-audio", "--lang=en-IN",
                             "--blink-settings=imagesEnabled=false", "--disable-extensions", "--disable-background-networking",
                             "--disable-component-update", "--disable-default-apps", "--disable-sync", "--no-pings",
                             f"--renderer-process-limit={tabs + 1}", "--js-flags=--max-old-space-size=384",
                             f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                            preexec_fn=lambda: os.nice(19))
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            break
        except OSError:
            time.sleep(0.5)
    return proc, port, profile


def search(port, q):
    """One search page in a fresh tab (a second Spotify page in one tab can hang): {rows, empty} or None if it failed."""
    got = None
    try:
        target = json.load(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT"), timeout=30))
        ws = _WS(target["webSocketDebuggerUrl"])
        try:
            ws.call("Page.enable")
            ws.call("Network.enable")
            ws.call("Network.setBlockedURLs", urls=BLOCK)
            ws.call("Page.navigate", url="https://open.spotify.com/search/" + urllib.parse.quote(q) + "/tracks")
            for _tick in range(60):  # up to 30 s (the first page after the browser starts can be slow)
                time.sleep(0.5)
                r = ws.call("Runtime.evaluate", expression=SEARCH_JS, returnByValue=True)
                got = json.loads(r.get("result", {}).get("value") or "{}")
                if got.get("rows") or got.get("empty"):
                    break
        finally:
            try:
                ws.sock.close()
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target['id']}", timeout=10)
            except OSError:
                pass
    except (TimeoutError, ConnectionError, OSError, json.JSONDecodeError, KeyError):
        return None
    return got if got and (got.get("rows") or got.get("empty")) else None


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    try:
        chrome = find_chrome()
    except SystemExit as e:
        return {"status": "ok", "stop_reason": str(e)}
    todo = due(conn, cfg, int(setting(cfg, "per_run", 60)))
    if not todo:
        log("Spotify web search: every song of the period searched within the recheck days")
        return {"status": "ok", "web_searched": 0}
    pause_s = float(setting(cfg, "pause_s", 3))
    log(f"Spotify web search (logged out, no API): {len(todo)} songs, one page every {pause_s:g} s")
    conn.execute("PRAGMA busy_timeout = 120000")
    proc, port, profile = start_browser(chrome)
    st = {"web_searched": 0, "web_queued": 0, "web_older": 0, "web_not_found": 0, "web_failed": 0}
    stop_reason, empty_in_row = None, 0
    try:
        for n, song in enumerate(todo, 1):
            if should_stop():
                raise songs.Cancelled()
            q = query(song["title"])
            got = search(port, q) or (time.sleep(pause_s) or search(port, q))  # one retry
            if not got:
                st["web_failed"] += 1
                empty_in_row += 1
                if empty_in_row >= 6:
                    stop_reason = "6 pages in a row without results: Spotify may be blocking; the next run continues"
                    break
                time.sleep(pause_s)
                continue  # not recorded: searched again next run
            empty_in_row = 0
            outcome, e, score = _choose(spotify_found._videos(conn, song["id"]), song, got.get("rows") or [])
            conn.execute(
                """INSERT INTO spotify_found (song_id, label, channel, yt_first, yt_title, track_id, sp_title, sp_artists, sp_release,
                                              file_status, title_match, outcome, score, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
                   ON CONFLICT(song_id) DO UPDATE SET track_id=excluded.track_id, sp_title=excluded.sp_title, sp_artists=excluded.sp_artists,
                       sp_release=excluded.sp_release, file_status=excluded.file_status, title_match=excluded.title_match,
                       outcome=excluded.outcome, score=excluded.score, imported_at=excluded.imported_at""",
                (song["id"], config.channel_labels(cfg).get(song["channel"]), song["channel"], song["first_day"], song["title"],
                 e and e["id"], e and e["name"], e and ", ".join(a["name"] for a in e["artists"]), e and e["release_date"] or None,
                 f"web search: {q}", outcome, score, db.now_iso()))
            conn.execute("UPDATE track SET spotify_checked_at=? WHERE id=?", (db.now_iso(), song["id"]))
            conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f"spotify_search_day:{song['id']}", songs.searched_day()))
            conn.commit()
            st["web_searched"] += 1
            st[{"queued": "web_queued", "older_release": "web_older"}.get(outcome, "web_not_found")] += 1
            if n % 20 == 0:
                log(f"  {n}/{len(todo)} searched: {st['web_queued']} found (queued for linking), {st['web_older']} older releases, "
                    f"{st['web_not_found']} not found")
            time.sleep(pause_s)
    finally:
        _kill_group(proc)
        shutil.rmtree(profile, ignore_errors=True)
    log(f"Spotify web search: {st['web_searched']} searched, {st['web_queued']} found and queued for linking, "
        f"{st['web_older']} older releases (reference only), {st['web_not_found']} not found, {st['web_failed']} pages failed"
        + (f"; {stop_reason}" if stop_reason else ""))
    return dict(st, status="ok", **({"stop_reason": stop_reason} if stop_reason else {}))


# ---- The whole backlog on another machine (the laptop: more CPU, many tabs); the server imports and links.
def export_songs(conn, cfg):
    """Server: the period's songs with no Spotify link and none waiting in Review, with their videos (for the match rules)."""
    spotify_found.ensure(conn)
    since, until = config.window(cfg)
    labels = config.channel_labels(cfg)
    out = []
    for r in conn.execute(
            """SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) first_day,
                      (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) channel
               FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?
                 AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify'
                                 AND p.review_state IN ('auto','approved','pending'))
               ORDER BY t.first_published DESC""", (since, until)).fetchall():
        out.append(dict(r, label=labels.get(r["channel"]),
                        videos=[{"title": v["title"], "description": (v["description"] or "")[:3000], "published_at": v["published_at"],
                                 "duration_ms": v["duration_ms"]} for v in spotify_found._videos(conn, r["id"])]))
    return out


def laptop_search(songs_list, tabs=12, out="spotify_laptop_search.json", log=print, pause_s=1.0):
    """Laptop: every song searched (cleaned title + singer, then the title alone), `tabs` pages at once; the best match
    by the rules among the top 5 results; then the found tracks' own pages (album, plays). Resumable: progress in `out`."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    res = json.load(open(out)) if os.path.exists(out) else {"songs": {}}
    todo = [s for s in songs_list if str(s["id"]) not in res["songs"]]
    log(f"{len(songs_list)} songs, {len(songs_list) - len(todo)} done earlier, {len(todo)} to search with {tabs} tabs")
    proc, port, profile = start_browser(find_chrome(), tabs)
    lock, stop, t0 = threading.Lock(), {"empty": 0, "why": None}, time.time()

    def save():
        tmp = out + ".tmp"
        json.dump(res, open(tmp, "w"))
        os.replace(tmp, out)

    def one(song):
        if stop["why"]:
            return
        qs = list(dict.fromkeys(q for q in (query(song["title"]), song_parts(song["title"])[0][:100], (song["title"] or "")[:100]) if q.strip()))
        outcome, e, score, used, any_page = "not_found", None, None, None, False
        for q in qs:
            got = search(port, q) or (time.sleep(2) or search(port, q))
            time.sleep(pause_s)
            if not got:
                continue
            any_page = True
            o, cand, sc = _choose(song["videos"], song, got.get("rows") or [])
            if o == "queued" or (o == "older_release" and outcome == "not_found"):
                outcome, e, score, used = o, cand, sc, q
            if o == "queued":
                break
        with lock:
            if not any_page:
                stop["empty"] += 1
                if stop["empty"] >= 12:
                    stop["why"] = "12 songs in a row without any page: Spotify may be blocking; re-run later to continue"
                return  # not recorded: searched again next run
            stop["empty"] = 0
            res["songs"][str(song["id"])] = {"outcome": outcome, "track": e, "score": score, "query": used or qs[0]}
            n = len(res["songs"])
            if n % 25 == 0:
                save()
                done = sum(1 for x in todo if str(x["id"]) in res["songs"])
                rate = done / max(time.time() - t0, 1)
                c = {}
                for v in res["songs"].values():
                    c[v["outcome"]] = c.get(v["outcome"], 0) + 1
                log(f"  {done}/{len(todo)} searched ({c}), ~{(len(todo) - done) / max(rate, 1e-6) / 60:.0f} min left")
    try:
        with ThreadPoolExecutor(tabs) as pool:
            list(pool.map(one, todo))
    finally:
        _kill_group(proc)
        shutil.rmtree(profile, ignore_errors=True)
        save()
    if stop["why"]:
        log("stopped: " + stop["why"])
    found = [v["track"]["id"] for v in res["songs"].values() if v["outcome"] == "queued" and v["track"]]
    if found:  # their own pages: album (the embed page leaves it out), play count
        log(f"reading the {len(found)} found tracks' pages (album, plays)")
        ver = json.load(open(spotify_found.verify_collect(found, tabs, out + ".pages.json", log=log)))
        res.update(tracks=ver["tracks"], plays=ver.get("plays", {}), hidden=ver.get("hidden", []), date=ver["date"],
                   captured_at=ver.get("captured_at"))
    save()
    c = {}
    for v in res["songs"].values():
        c[v["outcome"]] = c.get(v["outcome"], 0) + 1
    log(f"done in {(time.time() - t0) / 60:.0f} min: {c}; {len(res.get('tracks') or {})} found tracks read -> {out}")
    return out


def import_laptop(conn, path, log=print):
    """Server: the laptop's search results into spotify_found (every song marked searched today), then the found tracks
    checked and linked like the verified lists (no Spotify call)."""
    spotify_found.ensure(conn)
    data = json.load(open(path))
    cfg = config.load()
    labels = config.channel_labels(cfg)
    ts, day = db.now_iso(), songs.searched_day()
    counts = {}
    for sid, r in data["songs"].items():
        sid = int(sid)
        song = conn.execute("""SELECT coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) d,
                                      (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) ch
                               FROM track t WHERE t.id=?""", (sid,)).fetchone()
        if not song:
            continue
        prev = conn.execute("SELECT outcome FROM spotify_found WHERE song_id=?", (sid,)).fetchone()
        if prev and prev[0] in ("linked", "review", "rejected", "already_linked"):
            continue
        e = r.get("track")
        conn.execute(
            """INSERT INTO spotify_found (song_id, label, channel, yt_first, yt_title, track_id, sp_title, sp_artists, sp_release,
                                          file_status, title_match, outcome, score, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
               ON CONFLICT(song_id) DO UPDATE SET track_id=excluded.track_id, sp_title=excluded.sp_title, sp_artists=excluded.sp_artists,
                   sp_release=excluded.sp_release, file_status=excluded.file_status, title_match=NULL, outcome=excluded.outcome,
                   score=excluded.score, imported_at=excluded.imported_at, checked_at=NULL""",
            (sid, labels.get(song["ch"]), song["ch"], song["d"], song["title"], e and e["id"], e and e["name"],
             e and ", ".join(a["name"] for a in e["artists"]), e and e.get("release_date") or None,
             f"laptop search: {r.get('query')}", r["outcome"], r.get("score"), ts))
        conn.execute("UPDATE track SET spotify_checked_at=? WHERE id=?", (ts, sid))
        conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"spotify_search_day:{sid}", day))
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    conn.commit()
    log(f"Laptop search: {sum(counts.values())} songs recorded: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    st = spotify_found.verify_import(conn, path, log=log) if data.get("tracks") else {}
    return {"songs": counts, **st}


def main():  # python3 -m lens.spotify_browser_search "<song title>" ...   (a test: query and results, nothing stored)
    import sys
    a = sys.argv[1:]
    if a[:1] == ["export"]:  # server
        print(json.dumps(export_songs(db.connect(str(config.DB_FILE)), config.load()), ensure_ascii=False))
        return
    if a[:1] == ["laptop"]:  # laptop: laptop <songs.json> [tabs] [out.json]
        laptop_search(json.load(open(a[1])), int(a[2]) if len(a) > 2 else 12, a[3] if len(a) > 3 else "spotify_laptop_search.json")
        return
    if a[:1] == ["import"]:  # server: import <out.json>
        print(json.dumps(import_laptop(db.connect(str(config.DB_FILE)), a[1])))
        return
    proc, port, profile = start_browser(find_chrome())
    try:
        for t in sys.argv[1:]:
            q = query(t)
            got = search(port, q)
            print(f"{t!r} -> query {q!r}")
            for r in (got or {}).get("rows", [])[:5]:
                e = embed(r["id"]) or {}
                print(f"    {r['name']} | {', '.join(r['artists'])} | {e.get('release_date')} | {e.get('duration_ms')} ms | {r['id']}")
            if not got:
                print("    page failed")
            time.sleep(3)
    finally:
        _kill_group(proc)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
