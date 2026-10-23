"""Bulk merging of duplicate songs with configurable rules (Settings -> Merge rules). No AI: every rule
is a fixed test over names, ISRCs, artists, lengths and dates, and every suggestion shows its evidence.

A duplicate is one song that ended up as two or more songs: a spelling variant ("Yarraa" / "Yaraa"),
a longer name ("Dhol Bajaa" / "Dhol Bajaa Kesari"), or the same recording found on two songs via
Spotify/Deezer (same ISRC). Each rule either suggests merges (Review -> Duplicate songs, merge all of a
rule in one click) or merges automatically during the pipeline. Every merge is recorded in the
decision log and can be undone; "not the same" answers are remembered.

Candidate pairs come from blocking, never from comparing every song with every other: songs sharing an
ISRC, or sharing a rare word of their name (words used by more than `MAX_BLOCK` songs are ignored), so
the work grows with the number of songs, not its square.
"""
import hashlib
import json
from collections import defaultdict
from datetime import date

from . import config, db, match, review

MAX_BLOCK = 300

RULES = {
    "same_isrc": "Same ISRC: the same recording reached two songs through Spotify or Deezer",
    "spotify_same": "Same Spotify title and main artist, similar length (e.g. single and re-release)",
    "similar_name": "Near-identical name (spelling variant), same label, close dates and length",
    "name_contains": "One name contains the other, same label, close dates and length",
}


def similarity(a, b):
    """1 - edit distance / longer length, on the names without spaces ('yarraa' vs 'yaraa' -> 0.83)."""
    a, b = a.replace(" ", ""), b.replace(" ", "")
    if not a or not b:
        return 0.0
    if abs(len(a) - len(b)) > max(len(a), len(b)) // 3:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


def _key(t):
    return t["song_key"] or f"id:{t['id']}"


def _days(a, b):
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except (TypeError, ValueError):
        return None


def _songs(conn, cfg):
    """Songs in the window with what the rules need."""
    since, until = config.window(cfg)
    lab = config.channel_labels(cfg)
    rows = conn.execute("""
        SELECT t.id, t.title, t.yt_title, t.song_key, t.isrc, t.duration_ms, t.first_published, t.release_id,
               (SELECT max(channel) FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
                  AND p.review_state IN ('auto','approved','song')) channel,
               (SELECT count(*) FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
                  AND p.review_state IN ('auto','approved','song')) videos,
               (SELECT sum((SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='views'
                            ORDER BY capture_date DESC LIMIT 1))
                  FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
                  AND p.review_state IN ('auto','approved','song')) views,
               (SELECT a.spotify_artist_id FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
                  WHERE ta.track_id=t.id ORDER BY ta.position LIMIT 1) main_artist,
               (SELECT group_concat(json_extract(p.match_detail, '$.isrc'), ' ') FROM platform_item p
                  WHERE p.track_id=t.id AND p.platform='spotify' AND p.match_method='same_recording') alt_isrcs,
               (SELECT group_concat(j.value, ' ') FROM platform_item p, json_each(p.match_detail, '$.isrcs') j
                  WHERE p.track_id=t.id AND p.platform='musicbrainz') mb_isrcs,
               (SELECT platform_id FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify'
                  AND p.review_state IN ('auto','approved') ORDER BY id LIMIT 1) spotify_id
        FROM track t
        WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?""", (since, until)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = lab.get(d["channel"], config.FIRST_LABEL) if d["channel"] else ""
        name = d["yt_title"] or d["title"] or ""
        d["name"] = match.fold(name)
        d["words"] = [w for w in match.tokens(name) if not w.isdigit()]
        d["quals"] = (d["song_key"] or "").split("|")[1] if "|" in (d["song_key"] or "") else ""
        d["isrcs"] = {x for x in [d["isrc"]] + (d["alt_isrcs"] or "").split() + (d["mb_isrcs"] or "").split() if x}
        out.append(d)
    return out


def _close(a, b, p):
    """Dates and lengths within the rule's limits (unknown values don't block)."""
    days = _days(a["first_published"], b["first_published"])
    if days is not None and days > p.get("max_days_apart", 9999):
        return False, days, None
    dur = None
    if a["duration_ms"] and b["duration_ms"]:
        dur = abs(a["duration_ms"] - b["duration_ms"]) / 1000
        if dur > p.get("max_length_diff_s", 9999):
            return False, days, dur
    return True, days, dur


def _pairs(songs, rule, p, across_labels):
    """Yield (a, b, evidence) for one rule."""
    def same_scope(a, b):
        return a["quals"] == b["quals"] and (across_labels or a["label"] == b["label"])

    if rule == "same_isrc":
        by = defaultdict(list)
        for s in songs:
            for i in s["isrcs"]:
                by[i].append(s)
        for isrc, grp in by.items():
            for b in grp[1:]:
                yield grp[0], b, {"isrc": isrc}
        return
    if rule == "spotify_same":
        by = defaultdict(list)
        for s in songs:
            if s["spotify_id"] and s["main_artist"]:
                core = match.fold(match.parse_spotify_title(s["title"] or "")[0])
                by[(core, s["main_artist"])].append(s)
        for grp in by.values():
            for b in grp[1:]:
                ok, days, dur = _close(grp[0], b, p)
                if ok and grp[0]["quals"] == b["quals"]:
                    yield grp[0], b, {"days_apart": days, "length_diff_s": dur}
        return
    # Name rules: block on rare words.
    index = defaultdict(list)
    for s in songs:
        for w in set(s["words"]):
            index[w].append(s)
    seen = set()
    for grp in index.values():
        if len(grp) < 2 or len(grp) > MAX_BLOCK:
            continue
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair in seen or not same_scope(a, b) or not a["name"] or not b["name"]:
                    continue
                seen.add(pair)
                sim = 1.0 if a["name"] == b["name"] else similarity(a["name"], b["name"])
                if rule == "similar_name":
                    if sim < p.get("min_similarity", 0.9):
                        continue
                    ev = {"name_similarity": round(sim, 3)}
                else:  # name_contains
                    short, long_ = sorted((a, b), key=lambda s: len(s["words"]))
                    if len(short["words"]) < p.get("min_words", 2) or short["words"] == long_["words"] \
                            or not set(short["words"]) <= set(long_["words"]):
                        continue
                    ev = {"contained": " ".join(short["words"])}
                ok, days, dur = _close(a, b, p)
                if ok:
                    yield a, b, dict(ev, days_apart=days, length_diff_s=dur)


def _keep(group):
    """The song that survives: one with Spotify metadata, then most videos, then most views, then oldest."""
    return sorted(group, key=lambda s: (s["release_id"] is None, -(s["videos"] or 0), -(s["views"] or 0), s["id"]))[0]


def suggestions(conn, cfg, only_rule=None):
    """Merge groups per rule: [{rule, group, keep, songs, evidence}]. Songs already rejected as
    different are not paired again; a song is suggested under the first rule that groups it."""
    mcfg = cfg["merge"]
    songs = _songs(conn, cfg)
    rejected = {(r[0], r[1]) for r in conn.execute("SELECT key_a, key_b FROM merge_rejection")}
    taken, out = set(), []
    for rule in RULES:
        p = mcfg["rules"].get(rule, {})
        if not p.get("enabled") or (only_rule and rule != only_rule):
            continue
        parent, ev = {}, defaultdict(list)

        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x
        byid = {}
        for a, b, e in _pairs(songs, rule, p, mcfg.get("across_labels", False)):
            if a["id"] in taken or b["id"] in taken:
                continue
            ka, kb = sorted((_key(a), _key(b)))
            if (ka, kb) in rejected:
                continue
            byid[a["id"]], byid[b["id"]] = a, b
            ra, rb = find(a["id"]), find(b["id"])
            if ra != rb:
                parent[rb] = ra
            ev[(min(a["id"], b["id"]), max(a["id"], b["id"]))] = e
        groups = defaultdict(list)
        for sid in byid:
            groups[find(sid)].append(byid[sid])
        for grp in groups.values():
            ids = {s["id"] for s in grp}
            keep = _keep(grp)
            gid = hashlib.sha1((rule + "|" + "|".join(sorted(_key(s) for s in grp))).encode()).hexdigest()[:12]
            out.append({"rule": rule, "group": gid, "keep": keep["id"],
                        "songs": [{k: s[k] for k in ("id", "title", "yt_title", "channel", "label", "first_published",
                                                     "videos", "views", "isrc", "duration_ms", "spotify_id")}
                                  for s in sorted(grp, key=lambda s: s["id"] != keep["id"])],
                        "evidence": [dict(e, pair=list(k)) for k, e in ev.items() if k[0] in ids]})
            taken |= ids
    return out


def apply(conn, groups, actor, summary):
    """Merge each {keep, drop:[ids]} group; one decision (undoable) for the whole batch."""
    from .songs import merge_tracks, refresh_song_dates_and_status
    groups = [g for g in groups if g.get("drop")]
    if not groups:
        return 0
    ids = {g["keep"] for g in groups} | {d for g in groups for d in g["drop"]}
    before = review.snapshot(conn, ids, [])
    n = 0
    for g in groups:
        for d in g["drop"]:
            if d != g["keep"] and conn.execute("SELECT 1 FROM track WHERE id=?", (d,)).fetchone() \
                    and conn.execute("SELECT 1 FROM track WHERE id=?", (g["keep"],)).fetchone():
                merge_tracks(conn, g["keep"], d)
                n += 1
    refresh_song_dates_and_status(conn)
    review.record(conn, actor, "merge", "songs", None, groups[0]["keep"], f"{summary}: {n} songs merged into "
                  f"{len(groups)}", before)
    conn.commit()
    return n


def reject(conn, keys):
    """Remember that these songs (by song key) are different; no rule pairs them again."""
    keys = sorted(set(keys))
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            conn.execute("INSERT OR IGNORE INTO merge_rejection (key_a, key_b, rejected_at) VALUES (?,?,?)",
                         (a, b, db.now_iso()))
    conn.commit()


def keys_of(conn, track_ids):
    return [r["song_key"] or f"id:{r['id']}" for r in
            conn.execute(f"SELECT id, song_key FROM track WHERE id IN ({','.join('?' * len(track_ids))})", list(track_ids))]


def auto_merge(conn, cfg, log):
    """Apply the rules set to 'merge automatically'. Returns the number of songs merged."""
    total = 0
    for rule, p in cfg["merge"]["rules"].items():
        if p.get("enabled") and p.get("action") == "auto" and rule in RULES:
            groups = suggestions(conn, cfg, only_rule=rule)
            n = apply(conn, [{"keep": g["keep"], "drop": [s["id"] for s in g["songs"] if s["id"] != g["keep"]]}
                             for g in groups], f"pipeline (rule {rule})", f"Automatic merge, rule {rule}")
            if n:
                log(f"merge rule {rule}: {n} duplicate songs merged automatically")
            total += n
    return total
