"""Daily additions (2026-11-10, developer page `#added`): how many songs were added each day, and on each platform.

Songs of the period only (first YouTube upload from the period start, not released before it), like every crawl.
A day is the server day (UTC) of the row's `first_seen_at`, i.e. when the crawl first stored it.

- Songs found: songs first stored that day. Songs uploaded: songs whose first YouTube upload is that day (usually
  the same day; a backfill or a new label puts older uploads on the day they were found).
- Links view ("links"): per platform, the songs whose first link on that platform was stored that day (a song linked
  twice on one platform counts once, on its first day). Linked = review state auto / approved / song; Spotify matches
  still waiting in Review are counted apart. YouTube uploads = every upload of the period's songs first stored that day.
- Songs view ("songs"): for the songs found each day, how many are on each platform now (coverage so far).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import config

PLATFORMS = [("spotify", "Spotify"), ("ytmusic", "YouTube Music"), ("jiosaavn", "JioSaavn"), ("deezer", "Deezer"),
             ("apple", "Apple Music"), ("musicbrainz", "MusicBrainz"), ("instagram", "Instagram audio")]
LINKED = ("auto", "approved", "song")


def _songs(conn, cfg, label):
    """{song id: (label, day found, day first uploaded)} for the period's songs (optionally one label)."""
    since, _ = config.window(cfg)
    labels = config.channel_labels(cfg)
    out = {}
    for tid, seen, up, ch in conn.execute(
            """SELECT t.id, substr(t.first_seen_at,1,10), substr(t.first_published,1,10),
                      (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1)
               FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND t.old_release IS NULL""", (since,)):
        lb = labels.get(ch, config.FIRST_LABEL) if ch else "Other"
        if not label or lb == label:
            out[tid] = (lb, seen, up)
    return out


def report(conn, cfg, days=30, label=None):
    days = max(1, min(int(days or 30), 366))
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(days)]  # newest first
    songs = _songs(conn, cfg, label)

    found, uploaded = defaultdict(int), defaultdict(int)
    for _, seen, up in songs.values():
        found[seen] += 1
        uploaded[up] += 1

    # first link per (platform, song), and where each song is now
    links = defaultdict(lambda: defaultdict(int))     # day -> platform -> songs first linked that day
    has = defaultdict(set)                            # platform -> songs linked now
    for plat, tid, first in conn.execute(
            f"""SELECT platform, track_id, min(substr(first_seen_at,1,10)) FROM platform_item
                WHERE track_id IS NOT NULL AND review_state IN ({",".join("?" * len(LINKED))})
                  AND platform IN ({",".join("?" * len(PLATFORMS))})
                GROUP BY platform, track_id""", (*LINKED, *[p for p, _ in PLATFORMS])):
        if tid in songs:
            has[plat].add(tid)
            if first >= start:
                links[first][plat] += 1
    review = defaultdict(int)  # Spotify matches waiting in Review, by the day they were found
    for tid, first in conn.execute("""SELECT track_id, min(substr(first_seen_at,1,10)) FROM platform_item
                                      WHERE platform='spotify' AND review_state='pending' AND track_id IS NOT NULL GROUP BY track_id"""):
        if tid in songs and first >= start:
            review[first] += 1
    uploads = defaultdict(int)  # every YouTube upload of the period's songs, by the day it was stored
    for tid, d, n in conn.execute("""SELECT track_id, substr(first_seen_at,1,10) d, count(*) FROM platform_item
                                     WHERE platform='youtube' AND track_id IS NOT NULL AND first_seen_at >= ? GROUP BY track_id, d""", (start,)):
        if tid in songs:
            uploads[d] += n

    # songs view: the songs found each day, and how many of them are on each platform now
    now_on = defaultdict(lambda: defaultdict(int))
    for tid, (_, seen, _) in songs.items():
        if seen >= start:
            for plat, _ in PLATFORMS:
                if tid in has[plat]:
                    now_on[seen][plat] += 1

    rows = [{"day": d, "found": found.get(d, 0), "uploaded": uploaded.get(d, 0), "uploads": uploads.get(d, 0),
             "links": {p: links[d].get(p, 0) for p, _ in PLATFORMS}, "review": review.get(d, 0),
             "now_on": {p: now_on[d].get(p, 0) for p, _ in PLATFORMS}} for d in day_list]
    full = [r for r in rows if r["day"] != today.isoformat()]  # completed days only, for averages
    last7 = full[:7]
    return {
        "today": today.isoformat(), "since": config.window(cfg)[0], "days": days, "label": label or "",
        "labels": sorted({lb for lb, _, _ in songs.values()} | {l["name"] for l in cfg["youtube"].get("labels", []) if l.get("enabled", True)}),
        "platforms": [{"key": p, "name": n} for p, n in PLATFORMS],
        "rows": rows,
        "totals": {"found": sum(r["found"] for r in rows), "uploaded": sum(r["uploaded"] for r in rows),
                   "uploads": sum(r["uploads"] for r in rows), "review": sum(r["review"] for r in rows),
                   "links": {p: sum(r["links"][p] for r in rows) for p, _ in PLATFORMS},
                   "now_on": {p: sum(r["now_on"][p] for r in rows) for p, _ in PLATFORMS}},
        "today_so_far": rows[0]["found"], "yesterday": full[0]["found"] if full else 0,
        "avg7": round(sum(r["found"] for r in last7) / len(last7), 1) if last7 else 0,
        "links_yesterday": sum(full[0]["links"].values()) if full else 0,
        "period_songs": len(songs), "on_now": {p: len(has[p]) for p, _ in PLATFORMS},
    }
