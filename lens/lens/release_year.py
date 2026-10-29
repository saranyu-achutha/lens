"""Release-year check: is a song a new release or an older song re-uploaded? Decided from free sources BEFORE any
Spotify search, so Spotify's small daily quota is spent only on new (or still unknown) songs.

Evidence per song (dates, earliest wins per source):
  musicbrainz  first-release-date of the matched recording
  apple        iTunes release date
  deezer       release date of the ISRC match
  jiosaavn     release date (or year) of the matched track
  spotify      album release date, when already linked
  youtube      "℗ 2014" / "© 2014" / "Released on: 2014-03-14" in the uploads' descriptions (supporting only)

Verdict, against the song's first YouTube upload (a source more than CATALOGUE_DAYS earlier says "older"):
  new        every platform source says it came out around the first upload
  old        at least one platform source says older and none says new (or only the descriptions say older)
  conflict   platform sources disagree: shown for review, Spotify still allowed
  unknown    no evidence yet
"""
import re
from datetime import date

from . import db

CATALOGUE_DAYS = 30
PLATFORMS = ("musicbrainz", "apple", "deezer", "jiosaavn")
P_YEAR = re.compile(r"(?:℗|\(P\)|©|\(C\))\s*((?:19|20)\d{2})")
RELEASED = re.compile(r"(?:released?\s*(?:on|date)?|release date)\s*[:\-]?\s*((?:19|20)\d{2})(?:-(\d{2})-(\d{2}))?", re.I)

SCHEMA_COLS = (("orig_release", "TEXT"), ("orig_release_source", "TEXT"), ("release_check", "TEXT"), ("release_checked_at", "TEXT"))


def ensure(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(track)")}
    for col, typ in SCHEMA_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE track ADD COLUMN {col} {typ}")
    conn.commit()


def _norm(d):
    """'2014' -> '2014' (a year only), '2014-03' / '2014-03-14T..' -> '2014-03-01' / '2014-03-14'; None if not a date."""
    d = (d or "").strip()
    m = re.match(r"((?:19|20)\d{2})(?:-(\d{2}))?(?:-(\d{2}))?", d)
    if not m:
        return None
    return m.group(1) if not m.group(2) else f"{m.group(1)}-{m.group(2)}-{m.group(3) or '01'}"


def evidence(conn, tid):
    """{source: earliest date} for one song."""
    ev = {}

    def add(src, d):
        d = _norm(d)
        if d and (src not in ev or d < ev[src]):
            ev[src] = d

    for plat, pub in conn.execute("""SELECT platform, published_at FROM platform_item WHERE track_id=? AND platform IN (?,?,?,?)
                                     AND review_state IN ('auto','approved')""", (tid,) + PLATFORMS):
        add(plat, pub)
    for (rd,) in conn.execute("""SELECT r.release_date FROM platform_item p JOIN track t ON t.id=p.track_id JOIN release r ON r.id=t.release_id
                                 WHERE p.track_id=? AND p.platform='spotify' AND p.review_state IN ('auto','approved')""", (tid,)):
        add("spotify", rd)
    for (desc,) in conn.execute("""SELECT description FROM platform_item WHERE track_id=? AND platform='youtube'
                                   AND review_state IN ('auto','approved','song') AND asset_type IN ('video','lyric','audio')""", (tid,)):
        for y in P_YEAR.findall(desc or ""):
            add("youtube", y)
        for y, mo, dd in RELEASED.findall(desc or ""):
            add("youtube", f"{y}-{mo}-{dd or '01'}" if mo else y)
    return ev


def verdict(ev, first_upload):
    """('new'|'old'|'conflict'|'unknown', original release date or None, sources string)."""
    if not first_upload:
        return "unknown", None, ""
    fu = date.fromisoformat(first_upload[:10])
    # A full date counts as older when it is more than CATALOGUE_DAYS before the first upload;
    # a year alone only when it is an earlier year (a "℗ 2026" on a September 2026 upload is not older).
    old = lambda d: (int(d) < fu.year) if len(d) == 4 else (fu - date.fromisoformat(d)).days > CATALOGUE_DAYS
    plat = {s: d for s, d in ev.items() if s != "youtube"}
    old_src = sorted(s for s, d in plat.items() if old(d))
    new_src = sorted(s for s, d in plat.items() if not old(d))
    yt_old = "youtube" in ev and old(ev["youtube"])
    if old_src and not new_src:
        srcs = old_src + (["youtube"] if yt_old else [])
        return "old", min(plat[s] for s in old_src)[:10], ",".join(srcs)
    if old_src and new_src:
        return "conflict", min(plat.values())[:10], ",".join(sorted(plat))
    if new_src:
        return "new", min(plat[s] for s in new_src)[:10], ",".join(new_src)
    if yt_old:  # only the descriptions say older (a re-upload citing its original ℗ year)
        return "old", ev["youtube"], "youtube"
    return "unknown", None, ""


def check_all(conn, since, until, log=print):
    """Re-check every song of the period; older songs without a Spotify link become 'catalogue' (older release)."""
    ensure(conn)
    ts = db.now_iso()
    counts = {"new": 0, "old": 0, "conflict": 0, "unknown": 0}
    rows = conn.execute("""SELECT id, first_published FROM track WHERE source='youtube' AND first_published >= ?
                           AND substr(first_published,1,10) <= ?""", (since, until)).fetchall()
    for tid, fp in rows:
        v, d, src = verdict(evidence(conn, tid), fp)
        counts[v] += 1
        conn.execute("UPDATE track SET release_check=?, orig_release=?, orig_release_source=?, release_checked_at=? WHERE id=?",
                     (v, d, src, ts, tid))
        if v == "old":  # status without spending a Spotify search; a later Spotify link still refines it
            conn.execute("""UPDATE track SET release_status='catalogue' WHERE id=? AND NOT EXISTS
                            (SELECT 1 FROM platform_item p WHERE p.track_id=track.id AND p.platform='spotify' AND p.review_state IN ('auto','approved'))""", (tid,))
    conn.commit()
    log(f"release-year check: {len(rows):,} songs, new {counts['new']:,}, older {counts['old']:,}, "
        f"conflicting sources {counts['conflict']:,}, no evidence yet {counts['unknown']:,}")
    return counts
