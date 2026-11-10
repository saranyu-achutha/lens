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
import bisect
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
    platform_view = _platform_view(conn, songs, has, start, day_list[::-1], today.isoformat())
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
        "pv": platform_view,
    }


def _pct(n, d):
    return round(100 * n / d, 1) if d else 0


def _quantile(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


def _platform_view(conn, songs, has, start, days_asc, today):
    """Songs found on each platform per day, for the platform charts: YouTube (the song found) and every linked platform.
    series[p][i] = {new: songs first linked that day, total: songs linked by the end of that day, coverage: % of the
    songs found by then}; lag = days from the first YouTube upload to the first link there (songs first uploaded in the range);
    overlap = songs on both / only A / only B, for every pair, over all the period's songs and over the range's songs."""
    first = {"youtube": {tid: seen for tid, (_, seen, _) in songs.items()}}
    for plat, tid, d in conn.execute(
            f"""SELECT platform, track_id, min(substr(first_seen_at,1,10)) FROM platform_item
                WHERE track_id IS NOT NULL AND review_state IN ({",".join("?" * len(LINKED))})
                  AND platform IN ({",".join("?" * len(PLATFORMS))}) GROUP BY platform, track_id""", (*LINKED, *[p for p, _ in PLATFORMS])):
        if tid in songs:
            first.setdefault(plat, {})[tid] = d
    keys = ["youtube"] + [p for p, _ in PLATFORMS]
    names = {"youtube": "YouTube", **dict(PLATFORMS)}
    found_by = sorted(seen for _, seen, _ in songs.values())
    series, lag, summary = {}, {}, {}
    for p in keys:
        fd = first.get(p, {})
        dates = sorted(fd.values())
        out = []
        for d in days_asc:
            total = bisect.bisect_right(dates, d)
            out.append({"day": d, "new": bisect.bisect_right(dates, d) - bisect.bisect_left(dates, d), "total": total,
                        "coverage": _pct(total, bisect.bisect_right(found_by, d))})
        series[p] = out
        # lag: the song's first YouTube upload day -> its first link on this platform, for songs first uploaded in the
        # range (older songs were found by the late-October backfill: their lag says nothing about the crawl's speed)
        lags = []
        for tid, d in fd.items():
            if d >= start:
                up = songs[tid][2]
                if up and up >= start:
                    try:
                        lags.append(max(0, (date.fromisoformat(d) - date.fromisoformat(up)).days))
                    except ValueError:
                        pass
        lag[p] = {"median": _quantile(lags, .5), "p75": _quantile(lags, .75), "n": len(lags),
                  "within1": _pct(sum(1 for x in lags if x <= 1), len(lags)), "within7": _pct(sum(1 for x in lags if x <= 7), len(lags))}
        done = [r for r in out if r["day"] != today]
        summary[p] = {"range": sum(r["new"] for r in out), "yesterday": done[-1]["new"] if done else 0,
                      "avg7": round(sum(r["new"] for r in done[-7:]) / len(done[-7:]), 1) if done else 0,
                      "on_now": len(fd), "coverage": _pct(len(fd), len(songs))}
    in_range = {tid for tid, (_, seen, _) in songs.items() if seen >= start}
    vs_youtube = _vs_youtube(songs, first, keys, days_asc, today)
    sets = {p: set(first.get(p, {})) for p in keys}
    overlap = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            A, B = sets[a], sets[b]
            overlap[f"{a}|{b}"] = {
                "all": {"both": len(A & B), "a_only": len(A - B), "b_only": len(B - A), "neither": len(songs) - len(A | B)},
                "range": {"both": len(A & B & in_range), "a_only": len((A - B) & in_range), "b_only": len((B - A) & in_range),
                          "neither": len(in_range - (A | B))}}
    return {"keys": keys, "names": names, "series": series, "lag": lag, "summary": summary, "overlap": overlap,
            "songs": len(songs), "songs_in_range": len(in_range), "vs_youtube": vs_youtube}


def _vs_youtube(songs, first, keys, days_asc, today):
    """YouTube vs the other platforms, day by day: of the songs YouTube gave us that day (found that day), how many each
    platform has now ("now"), had by the next day ("d1") and within a week ("d7"). A measure whose wait has not passed
    yet (d1 for today, d7 for the last 7 days) is None, so a young day is not shown as a low share."""
    td = date.fromisoformat(today)
    by_day = {}
    for tid, (_, seen, _) in songs.items():
        by_day.setdefault(seen, []).append(tid)
    others = [p for p in keys if p != "youtube"]
    rows, tot = [], {p: {"now": 0, "d1": 0, "d7": 0, "n1": 0, "n7": 0} for p in others}
    found_total = 0
    for d in days_asc:
        tids = by_day.get(d, [])
        age = (td - date.fromisoformat(d)).days
        row = {"day": d, "found": len(tids), "d1_ready": age >= 1, "d7_ready": age >= 7, "p": {}}
        found_total += len(tids)
        for p in others:
            fd = first.get(p, {})
            now = d1 = d7 = 0
            for t in tids:
                f = fd.get(t)
                if f:
                    now += 1
                    lag = (date.fromisoformat(f) - date.fromisoformat(d)).days
                    d1 += lag <= 1
                    d7 += lag <= 7
            row["p"][p] = {"now": now, "d1": d1 if age >= 1 else None, "d7": d7 if age >= 7 else None}
            tot[p]["now"] += now
            if age >= 1:
                tot[p]["d1"] += d1
                tot[p]["n1"] += len(tids)
            if age >= 7:
                tot[p]["d7"] += d7
                tot[p]["n7"] += len(tids)
        rows.append(row)
    overall = {p: {"now": _pct(t["now"], found_total), "d1": _pct(t["d1"], t["n1"]) if t["n1"] else None,
                   "d7": _pct(t["d7"], t["n7"]) if t["n7"] else None, "now_n": t["now"]} for p, t in tot.items()}
    return {"rows": rows, "overall": overall, "found": found_total, "platforms": others}
