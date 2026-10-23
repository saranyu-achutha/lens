"""Discover Sony Music India releases on Spotify and take a daily popularity reading.

Usage:
    python3 -m lens.discover_spotify [--since 2026-01-01] [--until YYYY-MM-DD]

Settings come from config.json (editable in the Console); credentials from .env.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import config, db
from .spotify import BudgetExhausted, Spotify, SpotifyError


class Cancelled(Exception):
    pass


def release_in_window(album, since, until):
    """Compare on the precision Spotify gives; year-only dates are kept if the year overlaps."""
    d, prec = album.get("release_date") or "", album.get("release_date_precision")
    if prec == "year":
        return since[:4] <= d <= until[:4]
    if prec == "month":
        return since[:7] <= d <= until[:7]
    return since <= d <= until


def label_of(album):
    """Spotify's development mode omits `label`; fall back to the owner named in the (P)/(C) line."""
    if album.get("label"):
        return album["label"]
    for c in sorted(album.get("copyrights") or [], key=lambda c: c.get("type") != "P"):
        text = re.sub(r"^\s*(\(?[PC℗©]\)?\s*)?(\d{4}\s+)?", "", c.get("text") or "").strip()
        if text:
            return text
    return None


def sony_match(album, label_re):
    if label_re.search(album.get("label") or ""):
        return "label"
    if any(label_re.search(c.get("text") or "") for c in album.get("copyrights") or []):
        return "copyright"
    return None


def collect_candidates(sp, dcfg, since, until, log, check):
    """Return {album_id: set(discovery sources)} from every enabled discovery path."""
    cands, totals = {}, {}

    def add(album_id, via):
        cands.setdefault(album_id, set()).add(via)

    types = [("album", "label_search")]
    if dcfg["use_track_search"]:
        types.append(("track", "track_search"))
    for year in range(int(since[:4]), int(until[:4]) + 1):
        for phrase in dcfg["label_queries"]:
            for type_, via in types:
                q = f'label:"{phrase}" year:{year}'
                before = len(cands)
                for item in sp.search(q, type_):
                    check()
                    if isinstance(item, tuple):
                        totals[f"{type_}: {q}"] = item[1]
                        continue
                    album = item if type_ == "album" else item.get("album") or {}
                    if album.get("id") and release_in_window(album, since, until):
                        add(album["id"], via)
                n = totals.get(f"{type_}: {q}", 0)
                cap = "  ⚠ hit Spotify's 1000-result cap, results truncated" if n > 1000 else ""
                log(f"search {type_:5} {q!r}: {n} results, +{len(cands) - before} new candidates{cap}")

    if dcfg["use_artist_seeds"]:
        for artist_id, note in config.read_seeds():
            check()
            before = len(cands)
            try:
                for album in sp.artist_albums(artist_id):
                    if release_in_window(album, since, until):
                        add(album["id"], "artist_seed")
            except SpotifyError as e:
                log(f"seed {artist_id} ({note}): skipped, {e.status}")
                continue
            log(f"seed artist {note or artist_id}: +{len(cands) - before} new candidates")

    return cands, totals


def upsert_release(conn, album, via, match, ts):
    conn.execute(
        """INSERT INTO release (title, upc, release_date, release_date_precision, album_type, label,
               copyrights, total_tracks, spotify_album_id, discovered_via, label_match, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(spotify_album_id) DO UPDATE SET
               title=excluded.title, upc=excluded.upc, release_date=excluded.release_date,
               release_date_precision=excluded.release_date_precision, album_type=excluded.album_type,
               label=excluded.label, copyrights=excluded.copyrights, total_tracks=excluded.total_tracks,
               discovered_via=excluded.discovered_via, label_match=excluded.label_match,
               last_seen_at=excluded.last_seen_at""",
        (album["name"], (album.get("external_ids") or {}).get("upc"), album.get("release_date"),
         album.get("release_date_precision"), album.get("album_type"), label_of(album),
         json.dumps(album.get("copyrights") or []), album.get("total_tracks"), album["id"],
         ",".join(sorted(via)), match, ts, ts),
    )
    return conn.execute("SELECT id FROM release WHERE spotify_album_id=?", (album["id"],)).fetchone()["id"]


def upsert_track(conn, t, release_id, ts):
    isrc = (t.get("external_ids") or {}).get("isrc")
    item = conn.execute("SELECT id, track_id FROM platform_item WHERE platform='spotify' AND platform_id=?",
                        (t["id"],)).fetchone()
    track_id = item["track_id"] if item else None
    if track_id is None and isrc:
        row = conn.execute("SELECT id FROM track WHERE isrc=?", (isrc,)).fetchone()
        track_id = row["id"] if row else None

    if track_id is None:
        track_id = conn.execute(
            """INSERT INTO track (isrc, title, duration_ms, explicit, release_id, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?)""",
            (isrc, t["name"], t.get("duration_ms"), int(bool(t.get("explicit"))), release_id, ts, ts),
        ).lastrowid
    elif release_id is not None:
        # Same ISRC on several releases (single + album): keep the earliest release as primary.
        conn.execute(
            """UPDATE track SET title=?, duration_ms=?, explicit=?, last_seen_at=?,
                   isrc=COALESCE(isrc, ?),
                   release_id=CASE WHEN (SELECT release_date FROM release WHERE id=release_id)
                                        <= (SELECT release_date FROM release WHERE id=?)
                                   THEN release_id ELSE ? END
               WHERE id=?""",
            (t["name"], t.get("duration_ms"), int(bool(t.get("explicit"))), ts, isrc,
             release_id, release_id, track_id),
        )

    for pos, a in enumerate(t.get("artists") or []):
        conn.execute("INSERT INTO artist (name, spotify_artist_id) VALUES (?,?) "
                     "ON CONFLICT(spotify_artist_id) DO UPDATE SET name=excluded.name", (a["name"], a["id"]))
        artist_id = conn.execute("SELECT id FROM artist WHERE spotify_artist_id=?", (a["id"],)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO track_artist (track_id, artist_id, position) VALUES (?,?,?)",
                     (track_id, artist_id, pos))

    conn.execute(
        """INSERT INTO platform_item (platform, platform_id, track_id, asset_type, title, url,
               match_method, match_score, review_state)
           VALUES ('spotify', ?, ?, 'audio', ?, ?, 'spotify_source', 1.0, 'auto')
           ON CONFLICT(platform, platform_id) DO UPDATE SET title=excluded.title""",
        (t["id"], track_id, t["name"], (t.get("external_urls") or {}).get("spotify")),
    )
    item_id = conn.execute("SELECT id FROM platform_item WHERE platform='spotify' AND platform_id=?",
                           (t["id"],)).fetchone()["id"]
    if t.get("popularity") is not None:
        conn.execute(
            """INSERT OR IGNORE INTO snapshot (platform_item_id, metric, value, capture_date, captured_at)
               VALUES (?, 'popularity', ?, ?, ?)""",
            (item_id, t["popularity"], ts[:10], ts),
        )
    return isrc


EXPORT_SQL = """
SELECT r.release_date, r.title AS release_title, r.album_type, r.label, r.upc,
       t.isrc, pi.title AS track_title,
       (SELECT group_concat(name, ', ') FROM (SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
        WHERE ta.track_id=t.id ORDER BY ta.position)) AS artists,
       t.duration_ms,
       (SELECT s.value FROM snapshot s WHERE s.platform_item_id=pi.id AND s.metric='popularity'
        ORDER BY s.capture_date DESC LIMIT 1) AS popularity,
       pi.platform_id AS spotify_track_id, pi.url
FROM platform_item pi
JOIN track t ON t.id = pi.track_id
JOIN release r ON r.id = t.release_id
WHERE pi.platform='spotify'
ORDER BY r.release_date DESC, r.title, pi.title
"""


def export_csv(conn, path):
    rows = conn.execute(EXPORT_SQL).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else [])
        w.writerows(tuple(r) for r in rows)
    return len(rows)


def set_isrc(conn, spotify_track_id, isrc):
    """Attach an ISRC found later. If another track already holds it (same recording on another
    release), merge this track into that one."""
    row = conn.execute("SELECT track_id FROM platform_item WHERE platform='spotify' AND platform_id=?",
                       (spotify_track_id,)).fetchone()
    if not row:
        return
    tid = row["track_id"]
    other = conn.execute("SELECT id FROM track WHERE isrc=?", (isrc,)).fetchone()
    if not other or other["id"] == tid:
        conn.execute("UPDATE track SET isrc=? WHERE id=?", (isrc, tid))
        return
    keep = other["id"]
    conn.execute("UPDATE platform_item SET track_id=? WHERE track_id=?", (keep, tid))
    conn.execute("INSERT OR IGNORE INTO track_artist (track_id, artist_id, position) "
                 "SELECT ?, artist_id, position FROM track_artist WHERE track_id=?", (keep, tid))
    conn.execute("""UPDATE track SET release_id=(
                        SELECT r.id FROM release r WHERE r.id IN (track.release_id, (SELECT release_id FROM track WHERE id=?))
                        ORDER BY r.release_date LIMIT 1) WHERE id=?""", (tid, keep))
    conn.execute("DELETE FROM track_artist WHERE track_id=?", (tid,))
    conn.execute("DELETE FROM track WHERE id=?", (tid,))


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, since=None, until=None, run_id=None):
    """Run one discovery pass. Returns a stats dict (stats["status"] is ok / partial / quota_exceeded);
    raises SpotifyError / Cancelled.

    Spotify's development mode has no batch endpoints and a small daily quota, so the pass is
    incremental and budgeted: albums already stored (or already rejected under the current match
    rule) are skipped, tracks are stored straight from the album tracklist, and ISRCs (one call per
    track) are filled in afterwards, newest releases first, within the remaining budget."""
    dcfg = cfg["discovery"]
    since = since or dcfg["since"]
    until = until or dcfg["until"] or date.today().isoformat()
    label_re = re.compile(dcfg["label_regex"], re.I)
    cid, secret = secrets.get("SPOTIFY_CLIENT_ID"), secrets.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        raise SpotifyError(0, "config", "Spotify client ID / secret not set (Sources page or .env)")

    def check():
        if should_stop():
            raise Cancelled()

    def on_raw(url, status, body):
        if dcfg["store_raw"]:
            conn.execute("INSERT INTO raw_capture (run_id, source, url, status, captured_at, body)"
                         " VALUES (?,'spotify',?,?,?,?)", (run_id, url, status, db.now_iso(), body))

    sp = Spotify(cid, secret, market=dcfg["market"], on_raw=on_raw, max_calls=dcfg["max_calls_per_run"] or None)
    ts = db.now_iso()
    status, stop_reason, retry_at = "ok", None, None
    totals, cands, already, ids = {}, {}, [], []
    accepted_labels, rejected_labels = Counter(), Counter()
    n_releases = n_tracks = n_isrc = no_pop = 0

    log(f"Discovering Sony Music India releases {since} .. {until} (market {dcfg['market']}, "
        f"budget {dcfg['max_calls_per_run'] or 'unlimited'} calls)")
    try:
        # 1. Candidate albums from every discovery path.
        cands, totals = collect_candidates(sp, dcfg, since, until, log, check)
        known = set() if dcfg.get("refresh_existing") else \
            {r["spotify_album_id"] for r in conn.execute("SELECT spotify_album_id FROM release")}
        rejected = {r[0] for r in conn.execute("SELECT spotify_album_id FROM rejected_album WHERE label_regex=?",
                                               (dcfg["label_regex"],))}
        already = [i for i in cands if i in known]
        ids = [i for i in cands if i not in known and i not in rejected]
        conn.executemany("UPDATE release SET last_seen_at=? WHERE spotify_album_id=?", [(ts, i) for i in already])
        conn.commit()
        skipped = sum(1 for i in cands if i in rejected and i not in known)
        log(f"candidate albums: {len(cands)} ({len(already)} already stored, {skipped} known non-Sony, "
            f"{len(ids)} to fetch)")

        # 2. New albums: fetch, check Sony India, store release + tracklist. One commit per album,
        #    so an interrupted run keeps everything finished so far.
        for n, album_id in enumerate(ids, 1):
            check()
            for a in sp.albums([album_id]):
                m = sony_match(a, label_re)
                if m and release_in_window(a, since, until):
                    accepted_labels[label_of(a)] += 1
                    tracks = sp.album_tracks(a)
                    release_id = upsert_release(conn, a, cands[a["id"]], m, ts)
                    for t in tracks:
                        if t.get("id"):
                            upsert_track(conn, t, release_id, ts)
                            n_tracks += 1
                    n_releases += 1
                else:
                    rejected_labels[label_of(a)] += 1
                    if release_in_window(a, since, until):
                        conn.execute("INSERT OR REPLACE INTO rejected_album VALUES (?,?,?,?,?)",
                                     (a["id"], dcfg["label_regex"], label_of(a), a.get("release_date"), ts))
            conn.commit()
            if n % 50 == 0 or n == len(ids):
                log(f"  albums {n}/{len(ids)}: {n_releases} Sony India releases, {n_tracks} tracks stored "
                    f"({sp.calls} calls)")

        # 3. ISRCs for tracks that don't have one yet, newest releases first.
        cap = dcfg["isrc_lookups_per_run"]
        todo = [r[0] for r in conn.execute(
            """SELECT pi.platform_id FROM platform_item pi JOIN track t ON t.id=pi.track_id
               JOIN release r ON r.id=t.release_id
               WHERE pi.platform='spotify' AND t.isrc IS NULL ORDER BY r.release_date DESC, t.id LIMIT ?""",
            (cap,))]
        if todo:
            log(f"looking up ISRCs for {len(todo)} tracks (up to {cap} per run)")
        for n, tid in enumerate(todo, 1):
            check()
            for t in sp.tracks([tid]):
                isrc = (t.get("external_ids") or {}).get("isrc")
                if isrc:
                    set_isrc(conn, t["id"], isrc)
                    n_isrc += 1
                if t.get("popularity") is None:
                    no_pop += 1
                else:
                    upsert_track(conn, t, None, ts)  # stores the popularity snapshot
            if n % 25 == 0 or n == len(todo):
                conn.commit()
            if n % 100 == 0:
                log(f"  ISRCs {n}/{len(todo)}")
    except BudgetExhausted:
        status, stop_reason = "partial", f"call budget of {dcfg['max_calls_per_run']} reached; the next run continues"
    except SpotifyError as e:
        if not e.quota_exceeded:
            raise
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)).isoformat(timespec="seconds")
        status = "quota_exceeded"
        stop_reason = (f"Spotify daily quota exceeded; requests allowed again in {e.retry_after / 3600:.1f} h "
                       f"(about {retry_at}). The run resumes automatically then.")
    conn.commit()
    if stop_reason:
        log(f"⚠ stopped early: {stop_reason}")

    left = conn.execute("""SELECT count(*) FROM platform_item pi JOIN track t ON t.id=pi.track_id
                           WHERE pi.platform='spotify' AND t.isrc IS NULL""").fetchone()[0]
    out = config.OUT_DIR / f"sony_india_spotify_{ts[:10]}_run{run_id or 0}.csv"
    exported = export_csv(conn, out)
    log(f"new releases: {n_releases}, new tracks: {n_tracks}, ISRCs added: {n_isrc} ({left} tracks still without ISRC), "
        f"API calls: {sp.calls}")
    if n_isrc and no_pop >= n_isrc:
        log("⚠ Spotify returned no popularity values (removed for apps in development mode)")
    if accepted_labels:
        log("accepted label strings: " + "; ".join(f"{l} ({c})" for l, c in accepted_labels.most_common()))
    if rejected_labels:
        log("top rejected label strings: " + "; ".join(f"{l} ({c})" for l, c in rejected_labels.most_common(10)))
    log(f"wrote {exported} rows -> {out.name}")
    return {"status": status, "stop_reason": stop_reason, "retry_at": retry_at,
            "since": since, "until": until, "candidates": len(cands), "already_stored": len(already),
            "releases": n_releases, "tracks": n_tracks, "isrcs_added": n_isrc, "tracks_without_isrc": left,
            "api_calls": sp.calls, "search_totals": totals, "accepted_labels": dict(accepted_labels.most_common()),
            "top_rejected_labels": dict(rejected_labels.most_common(25)), "csv": out.name}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since")
    ap.add_argument("--until")
    args = ap.parse_args()

    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(config.DB_FILE))
    run_id = conn.execute("INSERT INTO run_log (job, started_at, status) VALUES ('spotify_discovery', ?, 'running')",
                          (db.now_iso(),)).lastrowid
    conn.commit()
    stats, status = {}, "ok"
    try:
        stats = run(conn, config.load(), config.read_secrets(), since=args.since, until=args.until,
                    run_id=run_id)
        status = stats["status"]
    except SpotifyError as e:
        print(f"Spotify error: {e}", file=sys.stderr)
        stats, status = {"error": str(e)}, "failed"
    conn.execute("UPDATE run_log SET finished_at=?, status=?, stats=? WHERE id=?",
                 (db.now_iso(), status, json.dumps(stats), run_id))
    conn.commit()
    sys.exit(0 if status in ("ok", "partial") else 1)


if __name__ == "__main__":
    main()
