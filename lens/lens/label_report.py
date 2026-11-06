"""Label-wise report (2026-11-06): release counts and platform-wise views / streams by label, day by day for a month
and as monthly summaries for completed months, ranked by performance.

Growth, not lifetime totals: an item's gain is its reading minus its own previous reading (whenever that was), counted on
the day of the newer reading. So a platform read late (Spotify play counts often come a day later) is not lost: the
missed day shows no gain and the next reading carries it, and a month's sum is exact (the readings telescope). A newly
linked item's first reading adds nothing (it is a lifetime total, not growth). YouTube views and audio streams
(Spotify + YouTube Music + JioSaavn) are kept apart. Songs of the period only (not released before it).
"""
from collections import defaultdict
from datetime import date, timedelta

from . import config

PLATFORMS = (("youtube", "views", "yt"), ("spotify", "plays", "sp"), ("ytmusic", "plays", "ytm"), ("jiosaavn", "plays", "js"))
METRICS = {"releases": "Releases", "yt": "YouTube views gained", "audio": "Audio streams gained", "sp": "Spotify plays gained",
           "ytm": "YouTube Music plays gained", "js": "JioSaavn plays gained"}
RECENT_DAYS = 2  # the newest days can still grow as late readings (Spotify) come in


def _songs(conn, cfg):
    """{song id: (label, first upload day)} for the period's songs."""
    since, until = config.window(cfg)
    labels = config.channel_labels(cfg)
    out = {}
    for r in conn.execute("""SELECT t.id, substr(t.first_published,1,10) d,
                                    (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) ch
                             FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                               AND t.old_release IS NULL""", (since, until)):
        out[r[0]] = (labels.get(r[2], config.FIRST_LABEL) if r[2] else "Other", r[1])
    return out


def _gains(conn, songs, start, end):
    """{(label, day, key): gain} and {(day, key): items read} for days start..end, from each item's previous reading."""
    since = min(d for _, d in songs.values()) if songs else start
    g, read = defaultdict(int), defaultdict(int)
    for plat, metric, key in PLATFORMS:
        rows = conn.execute(f"""
            WITH s AS (SELECT p.track_id t, s.capture_date d, s.value v,
                              lag(s.value) OVER (PARTITION BY s.platform_item_id ORDER BY s.capture_date) pv
                       FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                       WHERE s.metric=? AND p.platform=? AND p.review_state IN ('auto','approved','song') AND p.track_id IS NOT NULL
                         AND s.capture_date >= ? AND s.capture_date <= ?)
            SELECT t, d, sum(v - pv), count(*) FROM s WHERE d >= ? AND pv IS NOT NULL GROUP BY t, d""",
                            (metric, plat, since, end, start)).fetchall()
        for t, d, gain, n in rows:
            s = songs.get(t)
            if s:
                g[(s[0], d, key)] += int(round(gain or 0))
                read[(d, key)] += n
    return g, read


def _counted(conn, songs, plat, metric, start, end):
    """Items of the period's songs with a reading on that platform between start and end: the base for the share read
    each day (Spotify hides counts under 1,000 plays, so those tracks never give a reading)."""
    ids = set(songs)
    rows = conn.execute("""SELECT DISTINCT p.id, p.track_id FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                           WHERE p.platform=? AND s.metric=? AND s.capture_date BETWEEN ? AND ?
                             AND p.review_state IN ('auto','approved','song')""", (plat, metric, start, end)).fetchall()
    return sum(1 for _, t in rows if t in ids)


def _row(label, gains, releases, keys_days):
    r = {"label": label, "releases": releases}
    for _, _, key in PLATFORMS:
        r[key] = sum(gains.get((label, d, key), 0) for d in keys_days)
    r["audio"] = r["sp"] + r["ytm"] + r["js"]
    return r


def _rank(rows, by):
    rows.sort(key=lambda r: -(r.get(by) or 0))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def daily(conn, month=None, by="yt", cfg=None):
    """One month day by day (default: this month): the labels ranked over the month so far, and each day per label."""
    cfg = cfg or config.load()
    today = date.today()
    month = month or today.strftime("%Y-%m")
    first = date.fromisoformat(month + "-01")
    last = min((first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), today)
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    songs = _songs(conn, cfg)
    gains, read = _gains(conn, songs, days[0], days[-1])
    rel = defaultdict(int)
    for lab, d in songs.values():
        if days[0] <= d <= days[-1]:
            rel[(lab, d)] += 1
    labels = sorted({lab for lab, _ in songs.values() if lab != "Other"})
    ranking = _rank([_row(l, gains, sum(rel[(l, d)] for d in days), days) for l in labels], by)
    by_day = []
    linked = {key: _counted(conn, songs, plat, metric, days[0], days[-1]) for plat, metric, key in PLATFORMS}
    for d in reversed(days):
        row = {"day": d, "labels": {}, "recent": d > (today - timedelta(days=RECENT_DAYS)).isoformat(),
               "read": {key: round(100 * read.get((d, key), 0) / linked[key]) if linked[key] else None for _, _, key in PLATFORMS}}
        for l in labels:
            row["labels"][l] = _row(l, gains, rel[(l, d)], [d])
        row["all"] = {k: sum(row["labels"][l][k] for l in labels) for k in ("releases", "yt", "sp", "ytm", "js", "audio")}
        by_day.append(row)
    months = sorted({d[:7] for _, d in songs.values()} | {today.strftime("%Y-%m")}, reverse=True)
    from .market_share import data_started
    return {"started": data_started(conn), "month": month, "by": by, "metrics": METRICS, "labels": labels, "ranking": ranking, "days": by_day,
            "months": months, "today": today.isoformat(), "since": config.window(cfg)[0]}


def monthly(conn, by="yt", cfg=None):
    """Completed months: each label's releases and growth over the month, ranked; the month still running is left out."""
    cfg = cfg or config.load()
    today = date.today()
    songs = _songs(conn, cfg)
    this = today.strftime("%Y-%m")
    since = config.window(cfg)[0]
    gains, read = _gains(conn, songs, since, (today.replace(day=1) - timedelta(days=1)).isoformat())
    labels = sorted({lab for lab, _ in songs.values() if lab != "Other"})
    by_month = defaultdict(lambda: defaultdict(int))  # month -> (label, key) -> gain
    days_with = defaultdict(set)
    for (lab, d, key), v in gains.items():
        by_month[d[:7]][(lab, key)] += v
        days_with[d[:7]].add(d)
    rel = defaultdict(int)
    for lab, d in songs.values():
        rel[(lab, d[:7])] += 1
    months = sorted({d[:7] for _, d in songs.values() if d[:7] < this}, reverse=True)
    out = []
    for m in months:
        rows = []
        for l in labels:
            r = {"label": l, "releases": rel[(l, m)]}
            for _, _, key in PLATFORMS:
                r[key] = by_month[m].get((l, key), 0)
            r["audio"] = r["sp"] + r["ytm"] + r["js"]
            rows.append(r)
        readings = len(days_with[m])
        out.append({"month": m, "ranking": _rank(rows, by if readings else "releases"), "days_with_readings": readings,
                    "ranked_by": by if readings else "releases",
                    "total": {k: sum(r[k] for r in rows) for k in ("releases", "yt", "sp", "ytm", "js", "audio")}})
    from .market_share import data_started
    return {"started": data_started(conn), "by": by, "metrics": METRICS, "months": out, "today": today.isoformat(), "since": since}


def csv_text(conn, view, month=None, by="yt"):
    """The report as CSV (no IDs): daily = one row per day and label; monthly = one row per completed month and label."""
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["releases", "yt", "audio", "sp", "ytm", "js"]
    names = ["Releases", "YouTube views gained", "Audio streams gained", "Spotify plays gained", "YouTube Music plays gained", "JioSaavn plays gained"]
    if view == "monthly":
        d = monthly(conn, by)
        w.writerow(["Month", "Rank", "Label"] + names)
        for m in d["months"]:
            for r in m["ranking"]:
                w.writerow([m["month"], r["rank"], r["label"]] + [r[c] for c in cols])
        return buf.getvalue(), "labels_monthly"
    d = daily(conn, month, by)
    w.writerow(["Day", "Label"] + names + ["Spotify tracks read that day %"])
    for day in d["days"]:
        for l in d["labels"]:
            r = day["labels"][l]
            w.writerow([day["day"], l] + [r[c] for c in cols] + [day["read"].get("sp")])
    return buf.getvalue(), f"labels_daily_{d['month']}"
