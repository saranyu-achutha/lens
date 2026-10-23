"""Reviewer decisions, recorded separately from the pipeline data with a before-snapshot so each one
can be undone exactly (Review -> Decision history)."""
import json

from . import db

TRACK_COLS = None


def _cols(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def snapshot(conn, track_ids, item_ids):
    """Rows a decision may touch: the tracks, their artists, and every item linked to them."""
    track_ids = {t for t in track_ids if t}
    tracks = {}
    for tid in track_ids:
        row = conn.execute("SELECT * FROM track WHERE id=?", (tid,)).fetchone()
        tracks[str(tid)] = dict(row) if row else None
    artists = {str(tid): [dict(r) for r in conn.execute("SELECT * FROM track_artist WHERE track_id=?", (tid,))]
               for tid in track_ids}
    ids = set(item_ids) | {r["id"] for tid in track_ids
                           for r in conn.execute("SELECT id FROM platform_item WHERE track_id=?", (tid,))}
    items = {str(i): dict(conn.execute("SELECT * FROM platform_item WHERE id=?", (i,)).fetchone()) for i in ids
             if conn.execute("SELECT 1 FROM platform_item WHERE id=?", (i,)).fetchone()}
    aliases = [dict(r) for tid in track_ids for r in conn.execute("SELECT * FROM song_alias WHERE track_id=?", (tid,))]
    return {"tracks": tracks, "artists": artists, "items": items, "aliases": aliases}


def record(conn, actor, action, platform, item_id, track_id, summary, before, rejection=None):
    before = dict(before, rejection=rejection)
    return conn.execute(
        """INSERT INTO review_decision (decided_at, decided_by, action, platform, item_id, track_id, summary, before)
           VALUES (?,?,?,?,?,?,?,?)""",
        (db.now_iso(), actor, action, platform, item_id, track_id, summary, json.dumps(before))).lastrowid


def undo(conn, decision_id, actor):
    d = conn.execute("SELECT * FROM review_decision WHERE id=?", (decision_id,)).fetchone()
    if not d:
        raise ValueError("decision not found")
    if d["undone_at"]:
        raise ValueError("already undone")
    snap = json.loads(d["before"])
    tids = {int(t) for t in snap["tracks"]}
    # Undo in reverse order only: a later decision on the same songs must be undone first.
    for later in conn.execute("SELECT id, before FROM review_decision WHERE id>? AND undone_at IS NULL", (decision_id,)):
        if tids & {int(t) for t in json.loads(later["before"])["tracks"]}:
            raise ValueError(f"decision #{later['id']} changed the same song later; undo that one first")
    tcols = _cols(conn, "track")
    for tid in tids:  # ISRCs are unique and may have moved between these songs: free them first
        conn.execute("UPDATE track SET isrc=NULL WHERE id=?", (tid,))
    for tid, row in snap["tracks"].items():
        if row is None:
            continue
        if conn.execute("SELECT 1 FROM track WHERE id=?", (int(tid),)).fetchone():
            conn.execute(f"UPDATE track SET {', '.join(f'{c}=?' for c in tcols if c != 'id')} WHERE id=?",
                         [row.get(c) for c in tcols if c != "id"] + [int(tid)])
        else:  # removed by a merge: put it back
            conn.execute(f"INSERT INTO track ({', '.join(tcols)}) VALUES ({', '.join('?' * len(tcols))})",
                         [row.get(c) for c in tcols])
    for tid, rows in snap["artists"].items():
        conn.execute("DELETE FROM track_artist WHERE track_id=?", (int(tid),))
        for r in rows:
            conn.execute("INSERT OR IGNORE INTO track_artist (track_id, artist_id, position) VALUES (?,?,?)",
                         (r["track_id"], r["artist_id"], r["position"]))
    icols = _cols(conn, "platform_item")
    for iid, row in snap["items"].items():
        conn.execute(f"UPDATE platform_item SET {', '.join(f'{c}=?' for c in icols if c != 'id')} WHERE id=?",
                     [row.get(c) for c in icols if c != "id"] + [int(iid)])
    if "aliases" in snap:  # merged song names point back to where they pointed before
        for tid in tids:
            conn.execute("DELETE FROM song_alias WHERE track_id=?", (tid,))
        for a in snap["aliases"]:
            conn.execute("INSERT OR REPLACE INTO song_alias (song_key, track_id, created_at) VALUES (?,?,?)",
                         (a["song_key"], a["track_id"], a["created_at"]))
    if snap.get("rejection"):
        conn.execute("DELETE FROM match_rejection WHERE platform_item_id=? AND track_id=?", tuple(snap["rejection"]))
    # Items the decision linked that were not linked before go back to unlinked.
    conn.execute("DELETE FROM release WHERE discovered_via='youtube_song' "
                 "AND NOT EXISTS (SELECT 1 FROM track t WHERE t.release_id=release.id)")
    conn.execute("UPDATE review_decision SET undone_at=?, undone_by=? WHERE id=?", (db.now_iso(), actor, decision_id))
    conn.commit()
