"""Songs from YouTube: group the labels' channel videos into songs, look each song up on Spotify once
(one search call), and read Deezer rank by ISRC. All rules are fixed; nothing uses AI.

Status of a song (track.release_status):
  new          released on Spotify at most `CATALOGUE_DAYS` before its first YouTube upload (or after it)
  catalogue    Spotify release more than `CATALOGUE_DAYS` older, or only on a compilation: a re-upload
  pending      a plausible Spotify match is waiting on the Review page
  not_on_spotify  no match found (searched again every `spotify_recheck_days`)
"""
import csv
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import config, db, match
from .spotify import BudgetExhausted, Spotify, SpotifyError

SONG_TYPES = ("video", "lyric", "audio", "other")
# Shorts and teasers belong to a song too, but their captions are often not the song's name, so they
# only join a song that the full uploads already formed (never create one).
EXTRA_TYPES = ("short", "teaser")
ASSET_TYPES = SONG_TYPES + EXTRA_TYPES
LINKED = ("auto", "approved", "song")
CATALOGUE_DAYS = 30


class Cancelled(Exception):
    pass


def resolve_key(conn, key):
    """Track id for a song name: a song with that key, or the song it was merged into."""
    row = conn.execute("SELECT id FROM track WHERE song_key=?", (key,)).fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT a.track_id FROM song_alias a JOIN track t ON t.id=a.track_id WHERE a.song_key=?",
                       (key,)).fetchone()
    return row["track_id"] if row else None


def group_videos(conn, since, ts, log, until=None):
    """Create/extend one track per song for videos published in the window (since .. until)."""
    until = until or date.today().isoformat()
    rejected = {(r[0], r[1]) for r in conn.execute("SELECT platform_item_id, track_id FROM match_rejection")}
    cfg = config.load()
    labels = config.channel_labels(cfg)
    titles = [c["title"] for c in config.active_channels(cfg)]
    # Re-apply the current video-type rules to stored videos (title and length only, no API calls).
    for r in conn.execute("""SELECT id, title, duration_ms, asset_type FROM platform_item
                             WHERE platform='youtube' AND published_at >= ?""", (since,)).fetchall():
        at = match.asset_type(r["title"], r["duration_ms"] / 1000 if r["duration_ms"] is not None else None)
        if at != r["asset_type"] and r["duration_ms"] is not None:
            conn.execute("UPDATE platform_item SET asset_type=? WHERE id=?", (at, r["id"]))
    vids = conn.execute(
        f"""SELECT id, title, channel, published_at, duration_ms, asset_type, track_id, review_state FROM platform_item
            WHERE platform='youtube' AND published_at >= ? AND substr(published_at,1,10) <= ?
              AND channel IN (SELECT value FROM json_each(?))
              AND asset_type IN ({','.join('?' * len(ASSET_TYPES))})""",
        (since, until, json.dumps(titles), *ASSET_TYPES)).fetchall()
    groups = {}
    for v in vids:
        name, quals = match.extract_song(v["title"])
        key = match.song_key(name, quals) if name else None
        # Songs of different labels never merge; Sony keys stay unprefixed so existing songs keep theirs.
        label = labels.get(v["channel"], config.FIRST_LABEL)
        if key and label != config.FIRST_LABEL:
            key = f"{label.lower()}::{key}"
        if key:
            groups.setdefault(key, {"name": name, "videos": []})["videos"].append(v)

    new_songs = linked = 0
    claimed, reached = set(), {}
    for key, g in groups.items():
        vs = g["videos"]
        # A video already matched to a Spotify-first track decides the song; else reuse by key (or the song
        # that key was merged into); else create one, unless the group is only shorts/teasers.
        tid = next((v["track_id"] for v in vs if v["track_id"] and v["review_state"] in ("auto", "approved")), None)
        if tid is None:
            tid = resolve_key(conn, key)
        if tid is None:
            # A naming-rule change gives existing songs a new key: keep the song the videos already belong to
            # (with its Spotify link and decisions) and remember the old name as an alias.
            cur = Counter(v["track_id"] for v in vs if v["track_id"] and v["review_state"] == "song").most_common(1)
            if cur and cur[0][0] not in claimed:
                tid = cur[0][0]
                old = conn.execute("SELECT song_key FROM track WHERE id=?", (tid,)).fetchone()
                if old and old["song_key"] and old["song_key"] != key:
                    conn.execute("INSERT OR REPLACE INTO song_alias (song_key, track_id, created_at) VALUES (?,?,?)",
                                 (old["song_key"], tid, ts))
                    conn.execute("UPDATE track SET song_key=? WHERE id=?", (key, tid))
        if tid is not None:
            claimed.add(tid)
            reached.setdefault(tid, []).append((len(vs), key, g["name"]))
        full = [v for v in vs if v["asset_type"] in SONG_TYPES]
        if tid is None and not full:
            continue
        vs_dates = full or vs
        first = min(v["published_at"] for v in vs_dates)
        # Audio/lyric videos carry the plain recording, so their length is closest to the song's.
        best = sorted(vs_dates, key=lambda v: (v["asset_type"] not in ("audio", "lyric"), v["published_at"]))[0]
        if tid is None:
            tid = conn.execute(
                """INSERT INTO track (title, yt_title, duration_ms, source, song_key, first_published, release_status,
                                      first_seen_at, last_seen_at)
                   VALUES (?,?,?, 'youtube', ?,?, NULL, ?,?)""",
                (g["name"], g["name"], best["duration_ms"], key, first, ts, ts)).lastrowid
            new_songs += 1
        else:
            # Songs without Spotify metadata take the name the current rules give (e.g. after a naming fix).
            conn.execute("""UPDATE track SET yt_title=?, title=CASE WHEN release_id IS NULL THEN ? ELSE title END
                            WHERE id=? AND song_key=? AND yt_title IS NOT ?""", (g["name"], g["name"], tid, key, g["name"]))
            conn.execute("""UPDATE track SET song_key=COALESCE(song_key, ?), yt_title=COALESCE(yt_title, ?),
                                first_published=CASE WHEN first_published IS NULL OR first_published > ? THEN ?
                                                     ELSE first_published END,
                                last_seen_at=? WHERE id=?""", (key, g["name"], first, first, ts, tid))
        for v in vs:
            if (v["id"], tid) in rejected:
                continue
            if v["track_id"] is None or (v["review_state"] in ("song", "pending") and v["track_id"] != tid):
                conn.execute("""UPDATE platform_item SET track_id=?, match_method='song_group', review_state='song',
                                match_score=NULL, match_detail=NULL WHERE id=?""", (tid, v["id"]))
                linked += 1
    # A song whose own name no longer comes out of the rules (renamed rule, merged songs) takes the name of
    # its largest group; the old name stays an alias so nothing regroups.
    for tid, ks in reached.items():
        row = conn.execute("SELECT song_key, release_id FROM track WHERE id=?", (tid,)).fetchone()
        if row and row["song_key"] not in {k for _, k, _ in ks}:
            _, key, name = max(ks)
            if conn.execute("SELECT 1 FROM track WHERE song_key=? AND id<>?", (key, tid)).fetchone():
                continue
            if row["song_key"]:
                conn.execute("INSERT OR REPLACE INTO song_alias (song_key, track_id, created_at) VALUES (?,?,?)",
                             (row["song_key"], tid, ts))
            conn.execute("""UPDATE track SET song_key=?, yt_title=?, title=CASE WHEN release_id IS NULL THEN ? ELSE title END
                            WHERE id=?""", (key, name, name, tid))
    # Songs whose videos all moved to another song (e.g. after a naming rule changed) are dropped.
    empty = """SELECT id FROM track WHERE source='youtube' AND isrc IS NULL
               AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=track.id)"""
    for tbl in ("song_meta", "song_alias", "track_artist"):
        conn.execute(f"DELETE FROM {tbl} WHERE track_id IN ({empty})")
    gone = conn.execute(f"DELETE FROM track WHERE id IN ({empty})").rowcount
    conn.execute("DELETE FROM track_artist WHERE track_id NOT IN (SELECT id FROM track)")
    conn.commit()
    log(f"songs: {len(groups)} song names from {len(vids)} videos {since} .. {until} ({new_songs} new, {linked} videos linked"
        + (f", {gone} empty songs removed" if gone else "") + ")")
    return len(groups), new_songs


def merge_tracks(conn, keep, drop):
    """Fold track `drop` into `keep` (same song found twice). Every platform item (YouTube videos, Spotify
    tracks, Deezer) moves to `keep`; metadata `keep` lacks is taken from `drop`; `drop`'s song names become
    aliases of `keep` so grouping runs keep the merge."""
    if keep == drop:
        return
    d = conn.execute("SELECT song_key, isrc FROM track WHERE id=?", (drop,)).fetchone()
    if d is None:
        return
    for key in [d["song_key"]] + [r[0] for r in conn.execute("SELECT song_key FROM song_alias WHERE track_id=?", (drop,))]:
        if key:
            conn.execute("INSERT OR REPLACE INTO song_alias (song_key, track_id, created_at) VALUES (?,?,?)",
                         (key, keep, db.now_iso()))
    conn.execute("UPDATE platform_item SET track_id=? WHERE track_id=?", (keep, drop))
    conn.execute("UPDATE match_rejection SET track_id=? WHERE track_id=? AND NOT EXISTS "
                 "(SELECT 1 FROM match_rejection m WHERE m.track_id=? AND m.platform_item_id=match_rejection.platform_item_id)",
                 (keep, drop, keep))
    isrc = d["isrc"]
    conn.execute("UPDATE track SET isrc=NULL WHERE id=?", (drop,))  # ISRC is unique: free it before moving
    conn.execute("INSERT OR IGNORE INTO track_artist (track_id, artist_id, position) "
                 "SELECT ?, artist_id, position FROM track_artist WHERE track_id=?", (keep, drop))
    conn.execute("""UPDATE track SET
                      song_key=COALESCE(song_key, (SELECT song_key FROM track WHERE id=?)),
                      yt_title=COALESCE(yt_title, (SELECT yt_title FROM track WHERE id=?)),
                      first_published=min(COALESCE(first_published, '9999'),
                                          COALESCE((SELECT first_published FROM track WHERE id=?), '9999')),
                      isrc=COALESCE(isrc, ?),
                      release_id=COALESCE(release_id, (SELECT release_id FROM track WHERE id=?)),
                      duration_ms=COALESCE(duration_ms, (SELECT duration_ms FROM track WHERE id=?)),
                      explicit=COALESCE(explicit, (SELECT explicit FROM track WHERE id=?)),
                      release_status=CASE WHEN release_id IS NULL THEN (SELECT release_status FROM track WHERE id=?)
                                          ELSE release_status END
                    WHERE id=?""", (drop, drop, drop, isrc, drop, drop, drop, drop, keep))
    conn.execute("UPDATE track SET first_published=NULL WHERE id=? AND first_published='9999'", (keep,))
    conn.execute("DELETE FROM track_artist WHERE track_id=?", (drop,))
    conn.execute("DELETE FROM song_meta WHERE track_id=?", (drop,))  # rebuilt for `keep` by the metadata step
    conn.execute("DELETE FROM match_rejection WHERE track_id=?", (drop,))
    conn.execute("UPDATE song_alias SET track_id=? WHERE track_id=?", (keep, drop))
    conn.execute("DELETE FROM track WHERE id=?", (drop,))


def refresh_song_dates_and_status(conn):
    """first_published = earliest linked YouTube video (any date); status from Spotify release vs that."""
    conn.execute("""UPDATE track SET first_published = (
                        SELECT min(p.published_at) FROM platform_item p
                        WHERE p.track_id=track.id AND p.platform='youtube' AND p.review_state IN ('auto','approved','song'))
                    WHERE EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=track.id AND p.platform='youtube'
                                  AND p.review_state IN ('auto','approved','song'))""")
    conn.execute(f"""UPDATE track SET release_status = (
                        SELECT CASE WHEN r.album_type='compilation' THEN 'catalogue'
                                    WHEN julianday(substr(track.first_published,1,10)) - julianday(r.release_date)
                                         > {CATALOGUE_DAYS} THEN 'catalogue'
                                    ELSE 'new' END
                        FROM release r WHERE r.id=track.release_id)
                    WHERE release_id IS NOT NULL AND first_published IS NOT NULL
                      AND EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=track.id AND p.platform='spotify'
                                  AND p.review_state IN ('auto','approved'))""")
    conn.commit()


def status_for(album, since):
    """Provisional status at attach time; refresh_song_dates_and_status() sets the final one."""
    if album.get("album_type") == "compilation":
        return "catalogue"
    d = album.get("release_date") or ""
    return "new" if d[:10] >= since else "catalogue"


def attach_spotify(conn, tid, t, decision, detail, since, ts):
    """Store a Spotify search hit as this song's metadata. Returns the (possibly merged) track id.
    A 'pending' hit is only parked (with the candidate) for review; the song is not changed until approved."""
    from .discover_spotify import upsert_release
    if decision != "auto":
        conn.execute(
            """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                   match_score, match_detail, review_state, published_at, duration_ms, first_seen_at)
               VALUES ('spotify', ?, ?, 'audio', ?, ?, 'search', ?, ?, 'pending', ?, ?, ?)
               ON CONFLICT(platform, platform_id) DO NOTHING""",
            (t["id"], tid, t["name"], (t.get("external_urls") or {}).get("spotify"), detail["total"],
             json.dumps(dict(detail, candidate=t)), t["album"].get("release_date"), t.get("duration_ms"), ts))
        conn.execute("UPDATE track SET release_status='pending', spotify_checked_at=? WHERE id=?", (ts, tid))
        return tid
    album = t["album"]
    known = conn.execute("SELECT id FROM release WHERE spotify_album_id=?", (album["id"],)).fetchone()
    # Keep what label discovery already stored about an album (label, (P) line); add new ones minimally.
    release_id = known["id"] if known else upsert_release(conn, dict(album, copyrights=[], external_ids={}),
                                                          {"youtube_song"}, "youtube_channel", ts)
    isrc = (t.get("external_ids") or {}).get("isrc")
    if isrc:
        other = conn.execute("SELECT id FROM track WHERE isrc=? AND id<>?", (isrc, tid)).fetchone()
        if other:  # this recording is already known (e.g. from Spotify label discovery): merge into it
            merge_tracks(conn, other["id"], tid)
            tid = other["id"]
    status = status_for(album, since)
    conn.execute("""UPDATE track SET title=?, isrc=COALESCE(isrc, ?), duration_ms=?, explicit=?,
                        release_id=COALESCE(release_id, ?), release_status=?, spotify_checked_at=? WHERE id=?""",
                 (t["name"], isrc, t.get("duration_ms"), int(bool(t.get("explicit"))), release_id, status, ts, tid))
    for pos, a in enumerate(t.get("artists") or []):
        conn.execute("INSERT INTO artist (name, spotify_artist_id) VALUES (?,?) "
                     "ON CONFLICT(spotify_artist_id) DO UPDATE SET name=excluded.name", (a["name"], a["id"]))
        aid = conn.execute("SELECT id FROM artist WHERE spotify_artist_id=?", (a["id"],)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO track_artist (track_id, artist_id, position) VALUES (?,?,?)", (tid, aid, pos))
    conn.execute(
        """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
               match_score, match_detail, review_state, published_at, duration_ms, first_seen_at)
           VALUES ('spotify', ?, ?, 'audio', ?, ?, 'search', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(platform, platform_id) DO UPDATE SET track_id=excluded.track_id, match_method='search',
               match_score=excluded.match_score, match_detail=excluded.match_detail, review_state=excluded.review_state""",
        (t["id"], tid, t["name"], (t.get("external_urls") or {}).get("spotify"), detail.get("total"), json.dumps(detail),
         "auto", album.get("release_date"), t.get("duration_ms"), ts))
    return tid


def same_recording(a, b):
    """Two Spotify tracks are the same recording: same ISRC, or same title, same main artist and
    length within 3 seconds (a single and the album copy of one song)."""
    ia, ib = (a.get("external_ids") or {}).get("isrc"), (b.get("external_ids") or {}).get("isrc")
    if ia and ib:
        return ia == ib
    if match.fold(match.parse_spotify_title(a["name"])[0]) != match.fold(match.parse_spotify_title(b["name"])[0]):
        return False
    if not a.get("artists") or not b.get("artists") or a["artists"][0]["id"] != b["artists"][0]["id"]:
        return False
    return abs((a.get("duration_ms") or 0) - (b.get("duration_ms") or 0)) <= 3000


def attach_versions(conn, tid, best, cands, rejected, ts):
    """Link the other Spotify tracks in the same search result that are the same recording (the single,
    the album copy, a re-release). No extra API calls. Returns how many were linked."""
    n = 0
    for t in cands:
        if t["id"] == best["id"] or (t["id"], tid) in rejected or not same_recording(best, t):
            continue
        if conn.execute("SELECT 1 FROM platform_item WHERE platform='spotify' AND platform_id=?", (t["id"],)).fetchone():
            continue
        conn.execute(
            """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                   match_score, match_detail, review_state, published_at, duration_ms, first_seen_at)
               VALUES ('spotify', ?, ?, 'audio', ?, ?, 'same_recording', NULL, ?, 'auto', ?, ?, ?)""",
            (t["id"], tid, t["name"], (t.get("external_urls") or {}).get("spotify"),
             json.dumps({"album": t["album"]["name"], "album_type": t["album"].get("album_type"),
                         "isrc": (t.get("external_ids") or {}).get("isrc"), "same_as": best["id"]}),
             t["album"].get("release_date"), t.get("duration_ms"), ts))
        n += 1
    return n


def rescore_pending(conn, since, ts, log):
    """Re-apply the current rules to parked Spotify candidates, without any API call."""
    rows = conn.execute("SELECT id, track_id, match_detail FROM platform_item "
                        "WHERE platform='spotify' AND review_state='pending' AND match_method='search'").fetchall()
    changed = 0
    for r in rows:
        cand = (json.loads(r["match_detail"] or "{}")).get("candidate")
        if not cand:
            continue
        tr = {"title": cand["name"], "artists": [a["name"] for a in cand["artists"]],
              "release_title": cand["album"]["name"], "release_date": cand["album"].get("release_date"),
              "duration_ms": cand.get("duration_ms")}
        best = None
        for v in conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                                 WHERE track_id=? AND platform='youtube' LIMIT 3""", (r["track_id"],)):
            total, detail = match.score(tr, {"title": v["title"], "description": v["description"],
                                             "published_at": v["published_at"],
                                             "duration_s": (v["duration_ms"] or 0) / 1000 or None})
            if best is None or total > best[0]:
                best = (total, detail)
        decision = match.decide(*best) if best else None
        if decision == "auto":
            attach_spotify(conn, r["track_id"], cand, "auto", best[1], since, ts)
            changed += 1
        elif decision is None:
            conn.execute("DELETE FROM platform_item WHERE id=?", (r["id"],))
            conn.execute("UPDATE track SET release_status='not_on_spotify', spotify_checked_at=? WHERE id=?",
                         (ts, r["track_id"]))
            changed += 1
        else:
            conn.execute("UPDATE platform_item SET match_score=?, match_detail=? WHERE id=?",
                         (best[0], json.dumps(dict(best[1], candidate=cand)), r["id"]))
    conn.commit()
    if changed:
        log(f"Spotify review queue re-scored with current rules: {changed} resolved without new calls")


def spotify_lookup(conn, cfg, secrets, since, ts, log, check, run_id, until="9999"):
    """One Spotify search per song that has no Spotify link yet (rechecks misses every few days)."""
    scfg = cfg["songs"]
    cid, secret = secrets.get("SPOTIFY_CLIENT_ID"), secrets.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        log("Spotify credentials not set; skipping metadata lookup")
        return {}
    recheck_before = (datetime.now(timezone.utc) - timedelta(days=scfg["spotify_recheck_days"])).isoformat()
    todo = conn.execute(
        """SELECT t.id, t.title, t.song_key, t.isrc FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')
             AND (t.spotify_checked_at IS NULL OR t.spotify_checked_at < ?)
           ORDER BY t.first_published DESC LIMIT ?""",
        (since, until, recheck_before, scfg["spotify_searches_per_run"])).fetchall()
    if not todo:
        log("Spotify: every song already looked up")
        return {"spotify_searches": 0}

    def on_raw(url, status, body):
        if cfg["discovery"]["store_raw"]:
            conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                         " VALUES (?,'spotify',?,?,?,?)", (run_id, url, status, db.now_iso(), body))

    sp = Spotify(cid, secret, market=cfg["discovery"]["market"], on_raw=on_raw)
    rejected = {(r[0], r[1]) for r in conn.execute(
        "SELECT p.platform_id, m.track_id FROM match_rejection m JOIN platform_item p ON p.id=m.platform_item_id "
        "WHERE p.platform='spotify'")}
    found = pending = missing = extra_versions = 0
    log(f"Spotify: looking up {len(todo)} songs (one search call each)")
    try:
        for n, song in enumerate(todo, 1):
            check()
            quals = song["song_key"].split("|")[1].split(",") if song["song_key"] and "|" in song["song_key"] else []
            q = f'track:"{song["title"]}"' + "".join(f" {x}" for x in quals if "-" not in x)
            if song["isrc"]:
                q = f"isrc:{song['isrc']}"  # known from Deezer: an exact lookup
            res = sp.get("/search", {"q": q, "type": "track", "market": sp.market, "limit": 10, "offset": 0})
            cands = [t for t in res["tracks"]["items"] if t]
            vids = conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                                   WHERE track_id=? AND platform='youtube' ORDER BY asset_type IN ('audio','lyric') DESC,
                                   published_at LIMIT 3""", (song["id"],)).fetchall()
            best = None
            for t in cands:
                if (t["id"], song["id"]) in rejected:
                    continue
                tr = {"title": t["name"], "artists": [a["name"] for a in t["artists"]],
                      "release_title": t["album"]["name"], "release_date": t["album"].get("release_date"),
                      "duration_ms": t.get("duration_ms")}
                for v in vids:
                    total, detail = match.score(tr, {"title": v["title"], "description": v["description"],
                                                     "published_at": v["published_at"],
                                                     "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                    if best is None or total > best[0]:
                        best = (total, detail, t)
            decision = match.decide(best[0], best[1]) if best else None
            if decision:
                tid = attach_spotify(conn, song["id"], best[2], decision, best[1], since, ts)
                if decision == "auto":
                    extra_versions += attach_versions(conn, tid, best[2], cands, rejected, ts)
                found += decision == "auto"
                pending += decision == "pending"
            else:
                conn.execute("UPDATE track SET release_status='not_on_spotify', spotify_checked_at=? WHERE id=?",
                             (ts, song["id"]))
                missing += 1
            conn.commit()
            if n % 25 == 0:
                log(f"  {n}/{len(todo)} looked up")
    except BudgetExhausted:
        pass
    except SpotifyError as e:
        if not e.quota_exceeded:
            raise
        log(f"⚠ Spotify search quota exceeded; remaining songs are looked up next run")
    conn.commit()
    log(f"Spotify: {found} matched (+{extra_versions} more versions of them), {pending} sent to review, "
        f"{missing} not found ({sp.calls} calls)")
    return {"spotify_searches": sp.calls, "spotify_matched": found, "spotify_pending": pending,
            "spotify_not_found": missing, "spotify_extra_versions": extra_versions}


def musicbrainz_lookup(conn, cfg, since, until, ts, log, check, run_id):
    """ISRCs (and artists, first release) from MusicBrainz, the open music database (data CC0, free; 1 request a
    second with an identifying User-Agent). For songs with no ISRC yet: one recording search by name + credited
    singer, scored with the same rules as Spotify; only confident matches are kept. The ISRC then unlocks the
    Deezer rank and an exact Spotify lookup by ISRC. Misses retry after 7 days."""
    today = ts[:10]
    recheck = (date.today() - timedelta(days=7)).isoformat()
    todo = conn.execute(
        """SELECT t.id, t.title,
                  (SELECT value FROM song_meta m WHERE m.track_id=t.id AND m.field='singer' ORDER BY position LIMIT 1) singer
           FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ? AND t.isrc IS NULL
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='musicbrainz')
             AND coalesce((SELECT value FROM state WHERE key='mb_lookup:' || t.id), '') < ?
           ORDER BY t.first_published DESC""", (since, until, recheck)).fetchall()
    if not todo:
        return {}
    contact = (cfg["songs"].get("musicbrainz_contact") or "").strip()
    ua = f"MarketLens/0.1 ( {contact} )" if contact else "MarketLens/0.1"

    def search(query):
        url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 10})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=30) as resp:
                    body = resp.read().decode()
                if cfg["discovery"]["store_raw"]:
                    conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                                 " VALUES (?,'musicbrainz',?,200,?,?)", (run_id, url, db.now_iso(), body))
                time.sleep(1.1)
                return json.loads(body).get("recordings") or []
            except urllib.error.HTTPError as e:
                time.sleep(5 if e.code == 503 else 2)  # 503 = slow down
            except urllib.error.URLError:
                time.sleep(3)
        return []

    def q(text):
        return '"' + re.sub(r'["\\]', " ", text) + '"'

    found = missing = taken = 0
    log(f"MusicBrainz: looking up {len(todo)} songs without an ISRC (about {len(todo) * 1.2 / 60:.0f} min)")
    for n, song in enumerate(todo, 1):
        check()
        vids = conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                               WHERE track_id=? AND platform='youtube' ORDER BY asset_type IN ('audio','lyric') DESC,
                               published_at LIMIT 3""", (song["id"],)).fetchall()
        recs = search(f'recording:{q(song["title"])} AND artist:{q(song["singer"])}') if song["singer"] else []
        if not recs:
            recs = search(f'recording:{q(song["title"])}')
        best = None
        for r in recs:
            artists = [a.get("name") for a in r.get("artist-credit") or [] if a.get("name")]
            release = (r.get("releases") or [{}])[0]
            tr = {"title": r.get("title") or "", "artists": artists, "release_title": release.get("title"),
                  "release_date": r.get("first-release-date") if len(r.get("first-release-date") or "") == 10 else None,
                  "duration_ms": r.get("length")}
            for v in vids:
                total, detail = match.score(tr, {"title": v["title"], "description": v["description"],
                                                 "published_at": v["published_at"],
                                                 "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                if best is None or total > best[0]:
                    best = (total, detail, r, artists, release)
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"mb_lookup:{song['id']}", today))
        if not best or match.decide(best[0], best[1]) != "auto":
            missing += 1
            continue
        r, isrcs = best[2], best[2].get("isrcs") or []
        detail = dict(best[1], isrcs=isrcs, artists=best[3], release=best[4].get("title"),
                      first_release_date=r.get("first-release-date"))
        conn.execute(
            """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                   match_score, match_detail, review_state, published_at, duration_ms, first_seen_at)
               VALUES ('musicbrainz', ?, ?, 'audio', ?, ?, 'search', ?, ?, 'auto', ?, ?, ?)
               ON CONFLICT(platform, platform_id) DO NOTHING""",
            (r["id"], song["id"], r.get("title"), f"https://musicbrainz.org/recording/{r['id']}", best[0],
             json.dumps(detail), r.get("first-release-date"), r.get("length"), ts))
        free = [i for i in isrcs if not conn.execute("SELECT 1 FROM track WHERE isrc=?", (i,)).fetchone()]
        if free:
            conn.execute("UPDATE track SET isrc=? WHERE id=? AND isrc IS NULL", (free[0], song["id"]))
        elif isrcs:
            taken += 1  # this recording is already another song: the same-ISRC merge rule pairs them
        found += 1
        if n % 10 == 0:
            conn.commit()  # save often: a stop or restart loses at most 10 songs
        if n % 25 == 0:
            log(f"  {n}/{len(todo)} looked up on MusicBrainz ({found} matched)")
    conn.commit()
    with_isrc = conn.execute("SELECT count(*) FROM platform_item WHERE platform='musicbrainz' AND match_detail LIKE '%\"isrcs\": [\"%'").fetchone()[0]
    log(f"MusicBrainz: {found} matched ({with_isrc} with an ISRC so far), {missing} not found or uncertain"
        + (f", {taken} share an ISRC with another song" if taken else ""))
    return {"musicbrainz_matched": found, "musicbrainz_not_found": missing}


def itunes_lookup(conn, cfg, since, until, ts, log, check, run_id):
    """Metadata from Apple's official iTunes Search API (free, no key, India storefront) for songs Spotify hasn't
    reached: album, release date, artists, genre, length. No ISRC. One search per song by name + credited singer,
    scored with the same rules as Spotify; only confident ('auto') matches are kept. Apple allows about 20 calls a
    minute, so calls are spaced 3 s apart. Misses retry after 3 days. (Deezer search returns nothing from India.)"""
    today = ts[:10]
    recheck = (date.today() - timedelta(days=3)).isoformat()
    todo = conn.execute(
        """SELECT t.id, t.title,
                  (SELECT value FROM song_meta m WHERE m.track_id=t.id AND m.field='singer' ORDER BY position LIMIT 1) singer,
                  (SELECT value FROM song_meta m WHERE m.track_id=t.id AND m.field='music_director' ORDER BY position LIMIT 1) composer
           FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND t.release_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform IN ('spotify','apple'))
             AND coalesce((SELECT value FROM state WHERE key='itunes_lookup:' || t.id), '') < ?
           ORDER BY t.first_published DESC""", (since, until, recheck)).fetchall()
    if not todo:
        return {}

    def search(term):
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": term, "country": "IN", "media": "music", "entity": "song", "limit": 10})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    body = resp.read().decode()
                if cfg["discovery"]["store_raw"]:
                    conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                                 " VALUES (?,'itunes',?,200,?,?)", (run_id, url, db.now_iso(), body))
                time.sleep(3.1)
                return json.loads(body).get("results") or []
            except urllib.error.HTTPError as e:
                time.sleep(30 if e.code in (403, 429) else 5)  # rate limited: back off
            except urllib.error.URLError:
                time.sleep(5)
        return []

    found = missing = 0
    log(f"iTunes metadata: looking up {len(todo)} songs Spotify hasn't reached (about {len(todo) * 3.2 / 60:.0f} min)")
    for n, song in enumerate(todo, 1):
        check()
        vids = conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                               WHERE track_id=? AND platform='youtube' ORDER BY asset_type IN ('audio','lyric') DESC,
                               published_at LIMIT 3""", (song["id"],)).fetchall()
        who = song["singer"] or song["composer"] or ""
        best = None
        for c in search(f'{song["title"]} {who}'.strip()):
            artists = [a.strip() for a in re.split(r",|&", c.get("artistName") or "") if a.strip()]
            tr = {"title": c.get("trackName") or "", "artists": artists, "release_title": c.get("collectionName"),
                  "release_date": (c.get("releaseDate") or "")[:10] or None, "duration_ms": c.get("trackTimeMillis")}
            for v in vids:
                total, detail = match.score(tr, {"title": v["title"], "description": v["description"],
                                                 "published_at": v["published_at"],
                                                 "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                if best is None or total > best[0]:
                    best = (total, detail, c, artists)
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"itunes_lookup:{song['id']}", today))
        if not best or match.decide(best[0], best[1]) != "auto":
            missing += 1
            continue
        c = best[2]
        detail = dict(best[1], album=c.get("collectionName"), release_date=(c.get("releaseDate") or "")[:10],
                      artists=best[3], genre=c.get("primaryGenreName"), explicit=c.get("trackExplicitness"))
        conn.execute(
            """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                   match_score, match_detail, review_state, published_at, duration_ms, first_seen_at)
               VALUES ('apple', ?, ?, 'audio', ?, ?, 'search', ?, ?, 'auto', ?, ?, ?)
               ON CONFLICT(platform, platform_id) DO NOTHING""",
            (str(c["trackId"]), song["id"], c.get("trackName"), c.get("trackViewUrl"), best[0], json.dumps(detail),
             (c.get("releaseDate") or "")[:10], c.get("trackTimeMillis"), ts))
        conn.execute("UPDATE track SET duration_ms=COALESCE(duration_ms, ?) WHERE id=?", (c.get("trackTimeMillis"), song["id"]))
        found += 1
        if n % 25 == 0:
            conn.commit()
            log(f"  {n}/{len(todo)} looked up on iTunes ({found} matched)")
    conn.commit()
    log(f"iTunes metadata: {found} matched (album, release date, artists, genre), {missing} not found or uncertain")
    return {"itunes_matched": found, "itunes_not_found": missing}


def deezer_readings(conn, cfg, since, ts, log, check, run_id):
    """Daily Deezer rank per ISRC (free API, no key). One call per song per day."""
    today = ts[:10]
    rows = conn.execute(
        """SELECT t.id, t.isrc FROM track t LEFT JOIN release r ON r.id=t.release_id
           WHERE t.isrc IS NOT NULL AND (t.first_published >= ? OR r.release_date >= ?)
             AND NOT EXISTS (SELECT 1 FROM platform_item p JOIN snapshot s ON s.platform_item_id=p.id
                             WHERE p.track_id=t.id AND p.platform='deezer' AND s.capture_date=?)
             AND coalesce((SELECT value FROM state WHERE key='deezer_missing:' || t.isrc), '') < ?""",
        (since, since, today, (date.today() - timedelta(days=7)).isoformat())).fetchall()
    ok = miss = 0
    for r in rows:
        check()
        url = f"https://api.deezer.com/track/isrc:{r['isrc']}"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                body = resp.read().decode()
        except urllib.error.URLError as e:
            log(f"Deezer unreachable ({e}); skipping")
            break
        if cfg["discovery"]["store_raw"]:
            conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                         " VALUES (?,'deezer',?,200,?,?)", (run_id, url, db.now_iso(), body))
        d = json.loads(body)
        if d.get("error") or not d.get("id"):
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"deezer_missing:{r['isrc']}", today))
            miss += 1
            continue
        conn.execute(
            """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url, match_method,
                   review_state, published_at, duration_ms, first_seen_at)
               VALUES ('deezer', ?, ?, 'audio', ?, ?, 'isrc', 'auto', ?, ?, ?)
               ON CONFLICT(platform, platform_id) DO UPDATE SET track_id=excluded.track_id""",
            (str(d["id"]), r["id"], d.get("title"), d.get("link"), d.get("release_date"),
             (d.get("duration") or 0) * 1000, ts))
        item = conn.execute("SELECT id FROM platform_item WHERE platform='deezer' AND platform_id=?",
                            (str(d["id"]),)).fetchone()["id"]
        if d.get("rank") is not None:
            conn.execute("INSERT INTO reading (platform_item_id, metric, value, captured_at) VALUES (?,?,?,?)",
                         (item, "rank", d["rank"], ts))
            conn.execute("""INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at)
                            VALUES (?, 'rank', ?, ?, ?) ON CONFLICT(platform_item_id, metric, capture_date)
                            DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                         (item, d["rank"], today, ts))
        ok += 1
        time.sleep(0.15)  # Deezer allows 50 requests per 5 s
    conn.commit()
    if rows:
        log(f"Deezer: rank read for {ok} songs, {miss} not on Deezer")
    return {"deezer_read": ok, "deezer_missing": miss}


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    from . import merge, metadata
    since, until = config.window(cfg)
    ts = db.now_iso()

    def check():
        if should_stop():
            raise Cancelled()

    n_songs, n_new = group_videos(conn, since, ts, log, until)
    rescore_pending(conn, since, ts, log)
    stats = {"songs": n_songs, "new_songs": n_new}
    stats.update(spotify_lookup(conn, cfg, secrets, since, ts, log, check, run_id, until))
    from . import spotify_artists
    stats.update(spotify_artists.run(conn, cfg, secrets, since, until, ts, log, check, run_id,
                                     budget=cfg["songs"].get("spotify_artist_calls_per_run", 700)))
    stats.update(itunes_lookup(conn, cfg, since, until, ts, log, check, run_id))
    stats.update(musicbrainz_lookup(conn, cfg, since, until, ts, log, check, run_id))
    stats["auto_merged"] = merge.auto_merge(conn, cfg, log)  # merge rules set to "merge automatically"
    refresh_song_dates_and_status(conn)
    stats["songs_with_metadata"] = metadata.build(conn, cfg, since, until, log)
    stats.update(deezer_readings(conn, cfg, since, ts, log, check, run_id))
    s = conn.execute("""SELECT count(*) n, sum(release_status='new') new, sum(release_status='catalogue') cat,
                               sum(release_status='not_on_spotify') missing, sum(release_status='pending') pending
                        FROM track t WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?""",
                     (since, until)).fetchone()
    log(f"songs {since} .. {until}: {s['n']} (new releases {s['new'] or 0}, re-uploads/catalogue {s['cat'] or 0}, "
        f"not on Spotify {s['missing'] or 0}, Spotify match awaiting review {s['pending'] or 0})")
    out = config.OUT_DIR / f"market_lens_songs_{ts[:10]}_run{run_id or 0}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = export_rows(conn, since, until)
    with out.open("w", newline="", encoding="utf-8") as f:
        write_csv(rows, f)
    log(f"wrote {len(rows)} songs -> {out.name}")
    meta_out = config.OUT_DIR / f"market_lens_song_metadata_{ts[:10]}_run{run_id or 0}.csv"
    with meta_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["song_id", "song", "field", "value", "value_key", "source"])
        w.writerows(conn.execute("""SELECT m.track_id, t.title, m.field, m.value, m.value_key, m.source FROM song_meta m
                                    JOIN track t ON t.id=m.track_id WHERE t.first_published >= ?
                                    AND substr(t.first_published,1,10) <= ? ORDER BY m.track_id, m.field, m.position""",
                                 (since, until)))
    log(f"wrote song metadata (one row per value) -> {meta_out.name}")
    stats["csv"] = out.name
    stats.update({"songs_in_window": s["n"], "new_releases": s["new"] or 0, "catalogue": s["cat"] or 0,
                  "not_on_spotify": s["missing"] or 0, "spotify_review": s["pending"] or 0, "status": "ok"})
    return stats


TYPE_COLS = ("video", "lyric", "audio", "short", "teaser", "other")

SONGS_EXPORT_SQL = """
WITH v AS (
  SELECT p.track_id, p.channel, p.platform_id, p.asset_type,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND metric='views' ORDER BY capture_date DESC LIMIT 1) views,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND metric='likes' ORDER BY capture_date DESC LIMIT 1) likes,
         (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND metric='comments' ORDER BY capture_date DESC LIMIT 1) comments
  FROM platform_item p WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song')),
sp AS (
  SELECT track_id, platform_id, url, id FROM platform_item
  WHERE platform='spotify' AND review_state IN ('auto','approved'))
SELECT t.id AS song_id, substr(t.first_published,1,10) AS first_youtube_upload, t.title AS song, t.yt_title AS youtube_name,
       (SELECT group_concat(name, ', ') FROM (SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
        WHERE ta.track_id=t.id ORDER BY ta.position)) AS artists,
       t.release_status AS status, r.release_date AS spotify_release_date, r.title AS spotify_album, r.album_type,
       t.isrc, t.duration_ms,
       (SELECT max(channel) FROM v WHERE v.track_id=t.id) AS channel,
       (SELECT count(*) FROM v WHERE v.track_id=t.id) AS youtube_videos,
       (SELECT sum(views) FROM v WHERE v.track_id=t.id) AS youtube_views,
       (SELECT sum(likes) FROM v WHERE v.track_id=t.id) AS youtube_likes,
       (SELECT sum(comments) FROM v WHERE v.track_id=t.id) AS youtube_comments,
""" + "".join(f"""       (SELECT count(*) FROM v WHERE v.track_id=t.id AND v.asset_type='{a}') AS {a}_videos,
       (SELECT sum(views) FROM v WHERE v.track_id=t.id AND v.asset_type='{a}') AS {a}_views,
""" for a in TYPE_COLS) + """       (SELECT s.value FROM platform_item d JOIN snapshot s ON s.platform_item_id=d.id
        WHERE d.track_id=t.id AND d.platform='deezer' AND s.metric='rank' ORDER BY s.capture_date DESC LIMIT 1) AS deezer_rank,
       (SELECT platform_id FROM v WHERE v.track_id=t.id ORDER BY views DESC LIMIT 1) AS youtube_video_id,
       'https://www.youtube.com/watch?v=' || (SELECT platform_id FROM v WHERE v.track_id=t.id ORDER BY views DESC LIMIT 1)
         AS youtube_url,
       (SELECT group_concat(platform_id, ' ') FROM v WHERE v.track_id=t.id) AS all_youtube_video_ids,
       (SELECT platform_id FROM sp WHERE sp.track_id=t.id ORDER BY id LIMIT 1) AS spotify_track_id,
       (SELECT url FROM sp WHERE sp.track_id=t.id ORDER BY id LIMIT 1) AS spotify_url,
       (SELECT count(*) FROM sp WHERE sp.track_id=t.id) AS spotify_versions,
       (SELECT group_concat(platform_id, ' ') FROM sp WHERE sp.track_id=t.id) AS all_spotify_track_ids
FROM track t LEFT JOIN release r ON r.id=t.release_id
WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?
ORDER BY t.first_published DESC
"""


META_COLS = ["film", "album", "language", "category", "occasion", "singer", "music_director", "lyricist", "cast",
             "director", "producer", "music_label"]


def export_rows(conn, since, until="9999"):
    """Song rows as dicts: the channel's tracked label before the channel column, metadata after the song."""
    from . import metadata
    lab = config.channel_labels(config.load())
    rows = conn.execute(SONGS_EXPORT_SQL, (since, until)).fetchall()
    meta = metadata.flat(conn, [r["song_id"] for r in rows])
    out = []
    for r in rows:
        d = {}
        for k in r.keys():
            if k == "channel":
                d["tracked_label"] = lab.get(r["channel"], config.FIRST_LABEL) if r["channel"] else ""
            d[k] = r[k]
            if k == "duration_ms":
                for f in META_COLS:
                    d[f if f != "music_label" else "music_label_credits"] = meta[r["song_id"]].get(f, "")
        out.append(d)
    return out


def write_csv(rows, f):
    w = csv.writer(f)
    w.writerow(rows[0].keys() if rows else [])
    w.writerows(tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows)
