"""Market share by label (2026-11-06): each song tracked daily from its release through day N of its life (N configurable:
config.json market_share.days, default 30; any number works), its performance by day N as the market-share KPI.

Formula (shown on the page for review):
  day n of song s           = its first YouTube upload date + (n - 1)              (upload day = day 1)
  P(s, p, n)                = the song's views / plays on platform p at the end of day n (song_daily): a song starts
                              at 0, so this is its growth over its first n days. No reading that day (e.g. Spotify read a
                              day late): linear interpolation between the readings before and after.
  final                     = P(s, p, N) once the song has reached day N
  provisional               = P(s, p, k) for a younger song, k = its latest completed day (turns final on day N)
  not measurable            = the song's first reading came after its day N (would include later growth): left out
  Share(L, p)               = sum of P over label L's songs / sum of P over all songs in the cohort (the tracked labels)
YouTube views and audio streams (Spotify + YouTube Music + JioSaavn) get separate shares, never one number.
"""
from collections import defaultdict
from datetime import date, timedelta

from . import config

MEASURES = (("yt", "YouTube views", "yt_views"), ("audio", "Audio streams", None), ("sp", "Spotify plays", "sp_plays"),
            ("ytm", "YouTube Music plays", "ytm_plays"), ("js", "JioSaavn plays", "js_plays"))
AUDIO = ("sp", "ytm", "js")
PRESETS = (7, 14, 30, 60, 90)


def settings(cfg=None):
    m = (cfg or config.load()).get("market_share") or {}
    return {"days": max(1, int(m.get("days", 30))), "focus_label": m.get("focus_label", "Times Music")}


def _at(points, target):
    """Value at a date from sorted [(day, value)]: the reading that day, else interpolated between the readings before
    and after; the last reading before when none comes after; None when the first reading is after the target (the
    song was not read by then, so its value would include later growth)."""
    if not points or points[0][0] > target:
        return None
    before = max((p for p in points if p[0] <= target), key=lambda p: p[0])
    after = next((p for p in points if p[0] > target), None)
    if before[0] == target or after is None:
        return before[1]
    d0, d1 = date.fromisoformat(before[0]), date.fromisoformat(after[0])
    t = (date.fromisoformat(target) - d0).days / max(1, (d1 - d0).days)
    return before[1] + (after[1] - before[1]) * t


def compute(conn, days=None, since=None, until=None, cfg=None):
    cfg = cfg or config.load()
    s = settings(cfg)
    N = max(1, int(days or s["days"]))
    p_since, p_until = config.window(cfg)
    since, until = since or p_since, until or p_until
    labels_of = config.channel_labels(cfg)
    today = date.today().isoformat()
    report_day = conn.execute("SELECT max(day) FROM song_daily WHERE day < ?", (today,)).fetchone()[0] or today
    songs = {}
    for r in conn.execute("""SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) d,
                                    (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) ch
                             FROM track t WHERE t.source='youtube' AND t.old_release IS NULL
                               AND substr(t.first_published,1,10) >= ? AND substr(t.first_published,1,10) <= ?
                               AND t.first_published >= ?""", (since, until, p_since)):
        lab = labels_of.get(r["ch"]) if r["ch"] else None
        if lab:
            songs[r["id"]] = {"id": r["id"], "title": r["title"], "label": lab, "release": r["d"]}
    cols = [c for _, _, c in MEASURES if c]
    pts = defaultdict(lambda: defaultdict(list))  # song -> col -> [(day, value)]
    if songs:
        for row in conn.execute(f"SELECT song_id, day, {', '.join(cols)} FROM song_daily WHERE day <= ? ORDER BY day", (report_day,)):
            if row[0] in songs:
                for i, c in enumerate(cols):
                    if row[2 + i] is not None:
                        pts[row[0]][c].append((row[1], row[2 + i]))
    keyof = {c: k for k, _, c in MEASURES if c}
    out_songs, curve = [], defaultdict(lambda: defaultdict(lambda: [0.0, 0]))  # (label, measure) -> day n -> [sum, count]
    for sid, sg in songs.items():
        rel = date.fromisoformat(sg["release"])
        age = (date.fromisoformat(report_day) - rel).days + 1  # days of life completed
        target = (rel + timedelta(days=N - 1)).isoformat()
        final = age >= N
        rec = dict(sg, age=age, status="final" if final else "provisional", day=N if final else max(age, 0),
                   measured_on=target if final else None)  # the date the figure is from (provisional: its newest reading)
        any_value = False
        for c in cols:
            p = pts[sid].get(c) or []
            v = _at(p, target) if final else (p[-1][1] if p else None)
            if not final and p and (rec["measured_on"] or "") < p[-1][0]:
                rec["measured_on"] = p[-1][0]  # until day N: the highest date with data
            rec[keyof[c]] = round(v) if v is not None else None
            any_value |= v is not None
            for d, val in p:  # the life curve: value on each day of life up to N (readings only)
                n = (date.fromisoformat(d) - rel).days + 1
                if 1 <= n <= N:
                    cur = curve[(sg["label"], keyof[c])][n]
                    cur[0] += val
                    cur[1] += 1
        if final and not any_value:
            rec["status"] = "not measurable"
        rec["audio"] = sum(rec[k] or 0 for k in AUDIO) if any(rec[k] is not None for k in AUDIO) else None
        out_songs.append(rec)
    measured = [r for r in out_songs if r["status"] != "not measurable"]
    labels = sorted({r["label"] for r in out_songs})
    total = {k: sum(r[k] or 0 for r in measured) for k, _, _ in MEASURES}
    total_songs = {k: sum(1 for r in measured if r[k] is not None) for k, _, _ in MEASURES}
    by_label = []
    for lab in labels:
        rs = [r for r in measured if r["label"] == lab]
        row = {"label": lab, "songs": len(rs), "final": sum(r["status"] == "final" for r in rs),
               "provisional": sum(r["status"] == "provisional" for r in rs),
               "not_measurable": sum(1 for r in out_songs if r["label"] == lab and r["status"] == "not measurable")}
        for k, _, _ in MEASURES:
            row[k] = sum(r[k] or 0 for r in rs)
            row[k + "_songs"] = sum(1 for r in rs if r[k] is not None)  # songs with a value on that measure (coverage)
            row[k + "_share"] = round(100 * row[k] / total[k], 2) if total[k] else None
        by_label.append(row)
    for k, _, _ in MEASURES:  # rank per measure
        for i, row in enumerate(sorted(by_label, key=lambda x: -(x[k] or 0)), 1):
            row[k + "_rank"] = i
    curves = {}
    for (lab, k), byn in curve.items():
        curves.setdefault(k, {})[lab] = [{"x": n, "y": round(v[0] / v[1]), "songs": v[1]} for n, v in sorted(byn.items())]
    for lab in labels:  # audio curve: the sum of the three platforms' averages
        a = defaultdict(float)
        for k in AUDIO:
            for p in curves.get(k, {}).get(lab, []):
                a[p["x"]] += p["y"]
        if a:
            curves.setdefault("audio", {})[lab] = [{"x": n, "y": round(v)} for n, v in sorted(a.items())]
    out_songs.sort(key=lambda r: -(r["yt"] or 0))
    months = sorted({r["release"][:7] for r in out_songs}, reverse=True) if since == p_since and until == p_until else None
    return {"days": N, "default_days": s["days"], "presets": PRESETS, "focus_label": s["focus_label"],
            "since": since, "until": until, "period": [p_since, p_until], "report_day": report_day,
            "measures": [{"key": k, "name": n} for k, n, _ in MEASURES], "total": total, "total_songs": total_songs, "labels": by_label,
            "counts": {"songs": len(out_songs), "final": sum(r["status"] == "final" for r in out_songs),
                       "provisional": sum(r["status"] == "provisional" for r in out_songs),
                       "not_measurable": sum(r["status"] == "not measurable" for r in out_songs)},
            "curves": curves, "songs": out_songs[:400], "months": months}


def save_days(n):
    """Developer: the default number of days (config.json market_share.days)."""
    n = int(n)
    if n < 1 or n > 365:
        raise ValueError("days must be between 1 and 365")
    cfg = config.load()
    cfg.setdefault("market_share", {})["days"] = n
    config.save(cfg)
    return settings(cfg)
