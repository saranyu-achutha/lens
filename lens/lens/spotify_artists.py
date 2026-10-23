"""Artist-first Spotify lookup: find songs through their credited artists' recent releases instead of search.

Spotify's development-mode quota is counted per endpoint group, so this keeps working while search is
blocked (and saves search calls when it isn't). For each song still without Spotify metadata:
  1. its credited singers / music director (YouTube credits) -> Spotify artist ids we already know;
  2. those artists' singles and albums released since shortly before the window (artist albums endpoint);
  3. releases whose name or film fits one of the artist's songs -> their track lists;
  4. tracks scored with the same rules as search; a confident match is confirmed with the full track
     (ISRC, album, artists) and attached exactly like a search hit. Uncertain ones go to Review.
"""
import json
from collections import defaultdict
from datetime import date, timedelta

from . import db, match
from .metadata import name_key
from .spotify import BudgetExhausted, Spotify, SpotifyError

PAGES_PER_GROUP = 2  # 20 most recent singles and 20 most recent albums per artist


def run(conn, cfg, secrets, since, until, ts, log, check, run_id, budget=700):
    from .songs import attach_spotify
    cid, secret = secrets.get("SPOTIFY_CLIENT_ID"), secrets.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return {}
    songs = conn.execute(
        """SELECT t.id, t.title,
                  (SELECT group_concat(value, '|') FROM song_meta m WHERE m.track_id=t.id
                     AND m.field IN ('singer','music_director')) people,
                  (SELECT group_concat(value, '|') FROM song_meta m WHERE m.track_id=t.id AND m.field='film') films
           FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND t.release_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')
             AND coalesce((SELECT value FROM state WHERE key='sp_artist_lookup:' || t.id), '') < ?""",
        (since, until, ts[:10])).fetchall()
    known = {}
    for name, aid in conn.execute("SELECT name, spotify_artist_id FROM artist WHERE spotify_artist_id IS NOT NULL"):
        known.setdefault(name_key(name), aid)
    by_artist = defaultdict(set)
    for s in songs:
        for person in (s["people"] or "").split("|"):
            aid = known.get(name_key(person)) if person else None
            if aid:
                by_artist[aid].add(s["id"])
    reachable = {sid for ids in by_artist.values() for sid in ids}
    if not by_artist:
        log("Spotify (artist-first): no unmatched song has a credited artist we know on Spotify")
        return {}
    log(f"Spotify (artist-first): {len(reachable)} of {len(songs)} unmatched songs have a credited artist we know "
        f"({len(by_artist)} artists); checking their recent releases")

    def on_raw(url, status, body):
        if cfg["discovery"]["store_raw"]:
            conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                         " VALUES (?,'spotify',?,?,?,?)", (run_id, url, status, db.now_iso(), body))

    sp = Spotify(cid, secret, market=cfg["discovery"]["market"], on_raw=on_raw, max_calls=budget)
    cutoff = (date.fromisoformat(since) - timedelta(days=120)).isoformat()
    song_rows = {s["id"]: s for s in songs}
    vids = {}

    def videos(sid):
        if sid not in vids:
            vids[sid] = conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                                        WHERE track_id=? AND platform='youtube' ORDER BY asset_type IN ('audio','lyric') DESC,
                                        published_at LIMIT 3""", (sid,)).fetchall()
        return vids[sid]

    best = {}  # song id -> (score, detail, simplified track, album)
    rejected = {(r[0], r[1]) for r in conn.execute(
        "SELECT p.platform_id, m.track_id FROM match_rejection m JOIN platform_item p ON p.id=m.platform_item_id "
        "WHERE p.platform='spotify'")}
    stop = None
    fetched_albums, done_artists, attached = set(), [], set()
    counts = {"auto": 0, "pending": 0}

    def confirm(sid, only_auto):
        """Fetch the full track (ISRC, album, artists) for a song's best candidate and attach it."""
        total, detail, t, album = best[sid]
        decision = match.decide(total, detail)
        if not decision or (only_auto and decision != "auto"):
            return
        full = sp.get(f"/tracks/{t['id']}", {"market": sp.market})
        attached.add(sid)
        if conn.execute("SELECT 1 FROM platform_item WHERE platform='spotify' AND platform_id=?", (full["id"],)).fetchone():
            return  # already another song's Spotify track
        attach_spotify(conn, sid, full, decision, dict(detail, via="artist_albums"), since, ts)
        counts[decision] += 1
        conn.commit()
    try:
        for n, (aid, sids) in enumerate(by_artist.items(), 1):
            check()
            releases = []
            for group in ("single", "album"):
                for page in range(PAGES_PER_GROUP):
                    res = sp.get(f"/artists/{aid}/albums", {"include_groups": group, "market": sp.market,
                                                            "limit": 10, "offset": page * 10})
                    items = res.get("items") or []
                    releases += [a for a in items if (a.get("release_date") or "")[:10] >= cutoff]
                    if len(items) < 10 or min((a.get("release_date") or "")[:10] for a in items) < cutoff:
                        break
            for album in releases:
                core = match.parse_spotify_title(album["name"])[0]
                # Only open releases that plausibly hold one of this artist's songs: the release is named after
                # the song (singles) or after the song's film (soundtracks).
                fits = [sid for sid in sids if sid not in best or best[sid][0] < 0.9
                        if match.title_score(core, song_rows[sid]["title"]) >= 0.6
                        or any(f and match.fold(f) in match.fold(album["name"])
                               for f in (song_rows[sid]["films"] or "").split("|"))]
                if not fits or album["id"] in fetched_albums:
                    continue
                fetched_albums.add(album["id"])
                tracks = sp.get(f"/albums/{album['id']}/tracks", {"market": sp.market, "limit": 50}).get("items") or []
                for t in tracks:
                    tr = {"title": t["name"], "artists": [a["name"] for a in t["artists"]], "release_title": album["name"],
                          "release_date": album.get("release_date"), "duration_ms": t.get("duration_ms")}
                    for sid in fits:
                        if (t["id"], sid) in rejected:
                            continue
                        for v in videos(sid):
                            total, detail = match.score(tr, {"title": v["title"], "description": v["description"],
                                                             "published_at": v["published_at"],
                                                             "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                            if sid not in best or total > best[sid][0]:
                                best[sid] = (total, detail, t, album)
            for sid in sids:  # confident matches are kept right away, so a quota stop loses nothing
                if sid in best and sid not in attached:
                    confirm(sid, only_auto=True)
            done_artists.append(aid)
            if n % 25 == 0:
                log(f"  {n}/{len(by_artist)} artists checked ({len(best)} songs with a candidate, {sp.calls} calls)")
    except BudgetExhausted:
        stop = f"call budget of {budget} reached"
    except SpotifyError as e:
        if not e.quota_exceeded:
            raise
        stop = f"Spotify quota reached for album lookups (retry in {e.retry_after / 3600:.1f} h)"
    try:
        for sid in list(best):  # uncertain candidates go to Review
            if sid not in attached:
                confirm(sid, only_auto=False)
    except (BudgetExhausted, SpotifyError) as e:
        if isinstance(e, SpotifyError) and not e.quota_exceeded:
            raise
        stop = stop or "Spotify quota or budget reached while confirming tracks"
    for sid in {sid for aid in done_artists for sid in by_artist[aid]}:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"sp_artist_lookup:{sid}", ts[:10]))
    conn.commit()
    matched, pending = counts["auto"], counts["pending"]
    log(f"Spotify (artist-first): {matched} matched, {pending} sent to review, {len(fetched_albums)} releases opened, "
        f"{sp.calls} calls" + (f"; stopped: {stop}" if stop else ""))
    return {"spotify_artist_matched": matched, "spotify_artist_pending": pending, "spotify_artist_calls": sp.calls}
