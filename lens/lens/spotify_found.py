"""Spotify links found outside the API (a logged-out browser search on the laptop), imported and checked here.

    python3 -m lens.spotify_found import <file.txt>     # server: load the list (no Spotify call)

The file is tab-separated with a header: Label | YouTube channel | YouTube first upload | Song (YouTube) |
Spotify song | Artists | Spotify link | Spotify release date | Status | Title match (as made from the Market Lens
not-found / unsearched lists, 2026-11-05). Each row is matched back to its song by title, first upload date and channel.

  * Spotify release from 30 days before the song's first YouTube upload on (the new-release rule): queued. The label
    sweep then makes one /tracks/{id} call for it (album, artist IDs, duration, ISRC), scores it with the usual rules and
    guards against the song's videos, and links it, sends it to Review, or drops it.
  * Older release (most likely a different song with the same title), no release date, or nothing found: kept here for
    reference only. Nothing is linked and no plays are read for it (user rule, 2026-11-05: old releases cost no crawl budget).

Every imported song counts as searched today (spotify_checked_at, and the one-search-a-day mark), so neither account
searches it again before the usual recheck days. Rows are in table spotify_found (one per song); the queue is worked
through by the label sweep (spotify_labels.run) before any per-song search.
"""
import json
import re
import sys
from datetime import date, timedelta

from . import config, db, match, songs

SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_found (
    song_id        INTEGER PRIMARY KEY,   -- the YouTube song (track.id)
    label          TEXT,
    channel        TEXT,
    yt_first       TEXT,                  -- first YouTube upload (day)
    yt_title       TEXT,
    track_id       TEXT,                  -- Spotify track ID found by the browser search, if any
    sp_title       TEXT,
    sp_artists     TEXT,
    sp_release     TEXT,
    file_status    TEXT,                  -- the file's own verdict
    title_match    TEXT,
    outcome        TEXT,                  -- queued | linked | review | no_match | older_release | not_found | no_date
                                          -- | already_linked | rejected | taken | gone
    score          REAL,
    imported_at    TEXT,
    checked_at     TEXT
);
CREATE INDEX IF NOT EXISTS spotify_found_outcome ON spotify_found(outcome);
"""
NEW_BEFORE_DAYS = 30  # a Spotify release up to 30 days before the first YouTube upload is the same new song


def ensure(conn):
    conn.executescript(SCHEMA)


def queued(conn):
    ensure(conn)
    return conn.execute("SELECT count(*) FROM spotify_found WHERE outcome='queued'").fetchone()[0]


def _rows(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    head = lines[0].split("\t")
    for line in lines[1:]:
        if line.strip():
            cells = line.split("\t") + [""] * len(head)
            yield dict(zip(head, (c.strip() for c in cells)))


def import_file(conn, path, log=print):
    ensure(conn)
    cfg = config.load()
    since, until = config.window(cfg)
    ts, today = db.now_iso(), date.today().isoformat()
    songs_by = {}  # (title, first day) -> [(id, channel, linked)]
    for r in conn.execute(
            """SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) d,
                      (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) ch,
                      EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify'
                              AND p.review_state IN ('auto','approved')) linked
               FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?""",
            (since, until)):
        songs_by.setdefault((r["title"].strip(), r["d"]), []).append((r["id"], r["ch"] or "", r["linked"]))
    n, counts, unmapped = 0, {}, []
    for row in _rows(path):
        n += 1
        title, day, ch = row.get("Song (YouTube)", ""), row.get("YouTube first upload", ""), row.get("YouTube channel", "")
        cands = songs_by.get((title, day), [])
        if len(cands) > 1:
            cands = [c for c in cands if c[1] == ch] or cands
        if len(cands) != 1:
            unmapped.append(f"{day} {title} [{ch}] ({len(cands)} songs)")
            continue
        sid, _, linked = cands[0]
        m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]{22})", row.get("Spotify link", ""))
        tid, rel = (m.group(1) if m else None), row.get("Spotify release date", "")[:10]
        new_from = (date.fromisoformat(day) - timedelta(days=NEW_BEFORE_DAYS)).isoformat()
        outcome = ("already_linked" if linked else "not_found" if not tid else "no_date" if not rel
                   else "queued" if rel >= new_from else "older_release")
        prev = conn.execute("SELECT outcome FROM spotify_found WHERE song_id=?", (sid,)).fetchone()
        if prev and prev[0] not in ("queued", "not_found", "older_release", "no_date"):
            continue  # already decided here (linked, sent to Review, ...): a re-import keeps that
        conn.execute(
            """INSERT INTO spotify_found (song_id, label, channel, yt_first, yt_title, track_id, sp_title, sp_artists, sp_release,
                                          file_status, title_match, outcome, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(song_id) DO UPDATE SET track_id=excluded.track_id, sp_title=excluded.sp_title,
                   sp_artists=excluded.sp_artists, sp_release=excluded.sp_release, file_status=excluded.file_status,
                   title_match=excluded.title_match, outcome=excluded.outcome, imported_at=excluded.imported_at""",
            (sid, row.get("Label"), ch, day, title, tid, row.get("Spotify song") or None, row.get("Artists") or None,
             rel or None, row.get("Status"), row.get("Title match") or None, outcome, ts))
        counts[outcome] = counts.get(outcome, 0) + 1
        if not linked:  # searched today (by the browser): no account searches it again before the recheck days
            conn.execute("UPDATE track SET spotify_checked_at=? WHERE id=?", (ts, sid))
            conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f"spotify_search_day:{sid}", songs.searched_day()))
    conn.commit()
    log(f"Read {n} rows: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        + (f"; {len(unmapped)} rows matched no single song" if unmapped else ""))
    for u in unmapped[:20]:
        log("  no single song: " + u)
    return {"rows": n, **counts, "unmapped": len(unmapped)}


def _videos(conn, sid):
    return conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item
                           WHERE track_id=? AND platform='youtube' AND review_state IN ('auto','approved','song')
                           ORDER BY asset_type IN ('audio','lyric') DESC, published_at LIMIT 3""", (sid,)).fetchall()


def process(conn, lanes, parallel, should_stop, rejected, known_tracks, since, ts, st, log=print):
    """The queued links: one /tracks/{id} call each (spread over the lanes), then scored and linked here (main thread)."""
    ensure(conn)
    todo = [dict(r) for r in conn.execute(
        "SELECT song_id, track_id, yt_title, yt_first FROM spotify_found WHERE outcome='queued' ORDER BY yt_first DESC")]
    st.update(found_checked=0, found_linked=0, found_review=0, found_dropped=0)
    if not todo or not lanes or all(l.stop for l in lanes):
        return

    def fetch(lane, rows):
        out = []
        for r in rows:
            if should_stop():
                break
            full = lane.call(f"/tracks/{r['track_id']}", {})
            if full is None:
                break  # this account stopped; the rest stay queued for the next run
            out.append((r, full))
        return out

    for got in parallel(fetch, todo):
        for r, t in got:
            sid, outcome, total, decision = r["song_id"], None, None, None
            linked = conn.execute("""SELECT 1 FROM platform_item WHERE track_id=? AND platform='spotify'
                                     AND review_state IN ('auto','approved')""", (sid,)).fetchone()
            if not t.get("id"):
                outcome = "gone"
            elif linked:
                outcome = "already_linked"
            elif (t["id"], sid) in rejected:
                outcome = "rejected"
            elif t["id"] in known_tracks:
                outcome = "taken"  # this Spotify track is already another song's (or waiting in Review)
            else:
                album = t.get("album") or {}
                track = {"title": t["name"], "artists": [a["name"] for a in t.get("artists") or []], "release_title": album.get("name"),
                         "release_date": album.get("release_date"), "duration_ms": t.get("duration_ms")}
                best = None
                for v in _videos(conn, sid):
                    s, detail = match.score(track, {"title": v["title"], "description": v["description"], "published_at": v["published_at"],
                                                    "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                    if best is None or s > best[0]:
                        best = (s, detail)
                decision = match.decide(*best) if best else None
                if decision == "auto":  # same guard as the sweep: the track's own title must share a word with the song
                    core = match.parse_spotify_title(t["name"])[0]
                    if not match.block_keys(core) & match.block_keys(r["yt_title"]):
                        decision = "pending"
                if decision and (album.get("release_date") or "") < (date.fromisoformat(r["yt_first"]) - timedelta(days=NEW_BEFORE_DAYS)).isoformat():
                    decision = None  # an older release after all (the album date): not this song
                if decision:
                    total = best[0]
                    songs.attach_spotify(conn, sid, t, decision, dict(best[1], via="browser_search"), since, ts)
                    known_tracks.add(t["id"])
                    outcome = "linked" if decision == "auto" else "review"
                else:
                    outcome, total = "no_match", best[0] if best else None
            conn.execute("UPDATE spotify_found SET outcome=?, score=?, checked_at=? WHERE song_id=?", (outcome, total, ts, sid))
            st["found_checked"] += 1
            st["found_linked"] += outcome == "linked"
            st["found_review"] += outcome == "review"
            st["found_dropped"] += outcome not in ("linked", "review")
        conn.commit()
    log(f"Browser-found Spotify links: {st['found_checked']} checked, {st['found_linked']} linked, "
        f"{st['found_review']} to Review, {st['found_dropped']} dropped; {queued(conn)} still queued")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "import":
        print(json.dumps(import_file(db.connect(str(config.DB_FILE)), sys.argv[2])))
    else:
        print("usage: python3 -m lens.spotify_found import <file.txt>")
