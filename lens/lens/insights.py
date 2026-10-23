"""Insights computed by fixed rules from public YouTube data (no AI here; lens/ai.py only explains these numbers).

Every song is measured against comparable songs, so the biggest label or the oldest upload doesn't win by default:
  velocity        views per day since the song's first upload (all its versions)
  index           velocity / median velocity of songs of the same age band (1.0 = typical; 3.0 = 3x typical)
  recent rate     views per day between a video's last two readings (needs two readings a few hours apart)
  acceleration    recent rate / the rate between the two readings before (needs three readings)
  engagement      likes per 1,000 views

Growth, acceleration and anomaly signals sharpen as daily readings accumulate; panels say when history is thin.
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from . import config

AGE_BANDS = [(7, "0-7 days"), (30, "8-30 days"), (90, "31-90 days"), (10 ** 6, "90+ days")]


def _band(age):
    return next(label for limit, label in AGE_BANDS if age <= limit)


def _pct(value, population):
    """Share of the population (0-100) at or below value."""
    if value is None or not population:
        return None
    return round(100 * sum(1 for p in population if p <= value) / len(population))


def _days(a, b):
    return (b - a).total_seconds() / 86400


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def song_facts(conn, cfg=None):
    """Per-song facts for the song window. Returns (facts list, meta dict)."""
    cfg = cfg or config.load()
    since, until = config.window(cfg)
    now = datetime.now(timezone.utc)
    songs = {r["id"]: dict(r) for r in conn.execute(
        """SELECT t.id, t.title, t.first_published, t.release_status, r.release_date, r.title release_title
           FROM track t LEFT JOIN release r ON r.id=t.release_id
           WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?""", (since, until))}
    if not songs:
        return [], {"since": since, "until": until, "songs": 0}
    ids = list(songs)
    ph = ",".join("?" * len(ids))
    meta = defaultdict(dict)
    for tid, field, value in conn.execute(
            f"""SELECT track_id, field, group_concat(value, ', ') FROM (SELECT * FROM song_meta WHERE track_id IN ({ph})
                AND field IN ('label','language','category','singer','music_director','film','channel','genre')
                ORDER BY position) GROUP BY track_id, field""", ids):
        meta[tid][field] = value
    apple_dates = dict(conn.execute(
        f"""SELECT track_id, max(json_extract(match_detail, '$.release_date')) FROM platform_item
            WHERE platform='apple' AND track_id IN ({ph}) GROUP BY track_id""", ids))
    # Every view reading of every linked video, oldest first.
    points = defaultdict(list)
    likes, comments, vids, vid_track = defaultdict(int), defaultdict(int), defaultdict(set), {}
    for item, tid, metric, value, at in conn.execute(
            f"""SELECT p.id, p.track_id, r.metric, r.value, r.captured_at FROM reading r JOIN platform_item p ON p.id=r.platform_item_id
                WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song') AND p.track_id IN ({ph})
                ORDER BY r.captured_at""", ids):
        vid_track[item] = tid
        if metric == "views":
            points[item].append((_ts(at), value))
    for item, tid, metric, value in conn.execute(
            f"""SELECT p.id, p.track_id, s.metric, s.value FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                WHERE p.platform='youtube' AND p.review_state IN ('auto','approved','song') AND p.track_id IN ({ph})
                AND s.capture_date=(SELECT max(capture_date) FROM snapshot s2 WHERE s2.platform_item_id=p.id AND s2.metric=s.metric)
                AND s.metric IN ('likes','comments')""", ids):
        (likes if metric == "likes" else comments)[tid] += value
    agg = defaultdict(lambda: {"views": 0, "recent": 0.0, "recent_n": 0, "before": 0.0, "before_n": 0, "videos": 0,
                               "last_read": None, "readings": 0})
    for item, pts in points.items():
        a = agg[vid_track[item]]
        a["views"] += pts[-1][1]
        a["videos"] += 1
        a["readings"] = max(a["readings"], len(pts))
        a["last_read"] = max(a["last_read"] or pts[-1][0], pts[-1][0])
        # Rates need readings at least ~6 hours apart to mean anything.
        spaced = [pts[-1]]
        for p in reversed(pts[:-1]):
            if _days(p[0], spaced[-1][0]) >= 0.25:
                spaced.append(p)
            if len(spaced) == 3:
                break
        if len(spaced) >= 2:
            (t1, v1), (t0, v0) = spaced[0], spaced[1]
            a["recent"] += max(v1 - v0, 0) / _days(t0, t1)
            a["recent_n"] += 1
            if len(spaced) == 3:
                tb, vb = spaced[2]
                a["before"] += max(v0 - vb, 0) / _days(tb, t0)
                a["before_n"] += 1
    facts = []
    for tid, s in songs.items():
        a = agg.get(tid)
        if not a or not a["views"]:
            continue
        first = _ts(s["first_published"])
        age = max(_days(first, now), 0.5)
        released = s["release_date"] or apple_dates.get(tid)
        m = meta.get(tid, {})
        facts.append({
            "id": tid, "title": s["title"], "label": m.get("label") or "", "language": m.get("language") or "",
            "category": m.get("category") or "", "singer": m.get("singer") or "", "music_director": m.get("music_director") or "",
            "film": m.get("film") or "", "channel": m.get("channel") or "", "genre": m.get("genre") or "",
            "first_upload": s["first_published"][:10], "age_days": round(age, 1), "age_band": _band(age),
            "released": released, "release_status": s["release_status"],
            "years_since_release": round((first.date() - datetime.fromisoformat(released[:10]).date()).days / 365, 1)
            if released and len(released) >= 10 else None,
            "views": a["views"], "videos": a["videos"], "velocity": round(a["views"] / age),
            "recent_rate": round(a["recent"]) if a["recent_n"] else None,
            "acceleration": round(a["recent"] / a["before"], 2) if a["before_n"] and a["before"] > 0 else None,
            "likes": likes.get(tid, 0), "comments": comments.get(tid, 0),
            "likes_per_1k": round(1000 * likes.get(tid, 0) / a["views"], 1) if a["views"] else None,
            "readings": a["readings"],
        })
    # Age-fair index: velocity against the median of the same age band.
    by_band = defaultdict(list)
    for f in facts:
        by_band[f["age_band"]].append(f["velocity"])
    band_median = {b: statistics.median(v) for b, v in by_band.items()}
    for f in facts:
        med = band_median[f["age_band"]] or 1
        f["index"] = round(f["velocity"] / med, 2)
        f["band_percentile"] = _pct(f["velocity"], by_band[f["age_band"]])
        # Daily growth relative to the song's own size: how fast it is still climbing, not how big it already is.
        f["growth_pct"] = round(100 * f["recent_rate"] / f["views"], 2) if f["recent_rate"] is not None else None
    top_cut = sorted(x["views"] for x in facts)[int(len(facts) * 0.95)] if len(facts) >= 20 else float("inf")
    for f in facts:
        f["already_hit"] = f["views"] >= top_cut
    with_recent = sum(1 for f in facts if f["recent_rate"] is not None)
    with_accel = sum(1 for f in facts if f["acceleration"] is not None)
    return facts, {"since": since, "until": until, "songs": len(facts), "with_recent_rate": with_recent,
                   "with_acceleration": with_accel, "band_median_velocity": {b: round(v) for b, v in band_median.items()},
                   "history_note": _history_note(conn)}


def _history_note(conn):
    days = [r[0] for r in conn.execute("SELECT DISTINCT capture_date FROM snapshot WHERE metric='views' ORDER BY 1")]
    if len(days) < 4:
        return (f"Daily readings exist for {len(days)} day(s) ({', '.join(days)}). Growth, acceleration and anomaly signals "
                "become reliable after 4-7 days of daily readings; until then rankings lean on age-adjusted views per day.")
    return f"Daily readings for {len(days)} days ({days[0]} to {days[-1]})."


def emerging_hits(facts, limit=25):
    """Breakout score 0-100 for songs not yet among the top 5% by views: age-fair speed, growth relative to their
    own size, and acceleration, each as a percentile against comparable songs."""
    by_band = defaultdict(list)
    for f in facts:
        by_band[f["age_band"]].append(f)
    growths = [c["growth_pct"] for c in facts if c["growth_pct"] is not None]
    accels = [c["acceleration"] for c in facts if c["acceleration"] is not None]
    out = []
    for f in facts:
        if f["already_hit"]:
            continue
        p_speed = f["band_percentile"]
        p_growth = _pct(f["growth_pct"], growths) if f["growth_pct"] is not None else None
        p_accel = _pct(f["acceleration"], accels) if f["acceleration"] is not None else None
        have = [(p, w) for p, w in ((p_speed, 0.4), (p_growth, 0.35), (p_accel, 0.25)) if p is not None]
        score = round(sum(p * w for p, w in have) / sum(w for _, w in have))
        evidence = [f"{f['velocity']:,} views/day since its first upload {f['age_days']:.0f} days ago - faster than "
                    f"{p_speed}% of songs aged {f['age_band']}"]
        if f["recent_rate"] is not None:
            evidence.append(f"still adding {f['recent_rate']:,} views/day ({f['growth_pct']}% of its total per day)")
        if f["acceleration"] is not None:
            evidence.append(f"growth {'accelerated' if f['acceleration'] > 1 else 'slowed'} {f['acceleration']}x vs the reading before")
        rate = f["recent_rate"] if f["recent_rate"] is not None else f["velocity"]
        out.append(dict(f, score=score, evidence=evidence,
                        signal="growth + acceleration" if p_accel is not None else "recent growth" if p_growth is not None
                        else "age-adjusted speed only (early signal)",
                        projection_7d=[round(rate * 7 * 0.7), round(rate * 7 * 1.3)]))
    out.sort(key=lambda r: (-r["score"], -(r["growth_pct"] or 0), -r["velocity"]))
    return out[:limit]


def cross_label(facts):
    """Labels (and label x language) compared by age-fair index, not totals."""
    def summarise(rows):
        idx = [r["index"] for r in rows]
        eng = [r["likes_per_1k"] for r in rows if r["likes_per_1k"] is not None]
        return {"songs": len(rows), "median_index": round(statistics.median(idx), 2),
                "median_percentile": round(statistics.median(r["band_percentile"] for r in rows)),
                "share_above_typical": round(100 * sum(1 for i in idx if i >= 1) / len(idx)),
                "median_velocity": round(statistics.median(r["velocity"] for r in rows)),
                "median_likes_per_1k": round(statistics.median(eng), 1) if eng else None,
                "total_views": sum(r["views"] for r in rows),
                "top_song": max(rows, key=lambda r: r["index"])["title"]}
    by_label, by_pair, by_lang = defaultdict(list), defaultdict(list), defaultdict(list)
    for f in facts:
        by_label[f["label"]].append(f)
        by_lang[f["language"] or "Unknown"].append(f)
        if f["language"]:
            by_pair[(f["label"], f["language"])].append(f)
    labels = sorted(({"label": k, **summarise(v)} for k, v in by_label.items() if k), key=lambda r: -r["median_percentile"])
    languages = sorted(({"language": k, **summarise(v)} for k, v in by_lang.items() if len(v) >= 5), key=lambda r: -r["median_percentile"])
    pairs = sorted(({"label": k[0], "language": k[1], **summarise(v)} for k, v in by_pair.items() if len(v) >= 5),
                   key=lambda r: -r["median_percentile"])
    return {"labels": labels, "languages": languages, "label_language": pairs[:40]}


def catalog_opportunities(facts, limit=25):
    """Older songs (released a year or more before this upload) that are drawing above-typical views."""
    rows = [f for f in facts if (f["years_since_release"] or 0) >= 1]
    rows.sort(key=lambda r: (-r["index"], -(r["recent_rate"] or 0)))
    out = []
    for f in rows[:limit]:
        why = [f"first released {f['released'] or 'earlier'}" + (f" ({f['years_since_release']} years before this upload)"
                                                                  if f["years_since_release"] else ""),
               f"{f['velocity']:,} views/day since the upload on {f['first_upload']} - faster than {f['band_percentile']}% "
               f"of songs aged {f['age_band']}"]
        out.append(dict(f, evidence=why))
    return out


def artist_portfolio(facts, field="singer", min_songs=3, limit=40):
    """Breadth vs dependence on one hit, per singer or music director."""
    by = defaultdict(list)
    for f in facts:
        for name in [n.strip() for n in (f.get(field) or "").split(",") if n.strip()]:
            by[name].append(f)
    out = []
    for name, rows in by.items():
        if len(rows) < min_songs:
            continue
        total = sum(r["views"] for r in rows)
        top = max(rows, key=lambda r: r["views"])
        share = top["views"] / total if total else 0
        above = sum(1 for r in rows if r["index"] >= 1) / len(rows)
        momentum = sum(r["recent_rate"] or 0 for r in rows)
        kind = ("One-hit dependent" if share >= 0.7 else "Consistent" if above >= 0.6 and share < 0.5
                else "Mixed")
        from .metadata import name_key
        out.append({"name": name, "key": name_key(name), "songs": len(rows), "total_views": total, "top_song": top["title"],
                    "top_song_share": round(100 * share), "share_above_typical": round(100 * above),
                    "median_index": round(statistics.median(r["index"] for r in rows), 2),
                    "median_percentile": round(statistics.median(r["band_percentile"] for r in rows)),
                    "recent_views_per_day": round(momentum), "profile": kind,
                    "labels": sorted({r["label"] for r in rows if r["label"]})})
    out.sort(key=lambda r: (-r["median_percentile"], -r["songs"]))
    return out[:limit]


def anomalies(facts, limit=25):
    """Songs whose latest growth rate departs sharply from their own previous rate (needs three readings)."""
    out = []
    for f in facts:
        a = f["acceleration"]
        if a is None or (f["recent_rate"] or 0) < 500:
            continue
        if a >= 2.5 or a <= 0.4:
            out.append(dict(f, direction="spike" if a >= 2.5 else "drop",
                            evidence=[f"{f['recent_rate']:,} views/day now vs about {round(f['recent_rate'] / a):,}/day before ({a}x)"]))
    out.sort(key=lambda r: -abs(r["acceleration"] - 1))
    return out[:limit]


def benchmark(facts, song_id):
    """How one song compares with comparable songs (same age band; same language and band; same label)."""
    f = next((x for x in facts if x["id"] == song_id), None)
    if not f:
        return None
    def vs(rows, what):
        rows = [r for r in rows if r["id"] != song_id]
        if len(rows) < 3:
            return None
        return {"compared_with": what, "songs": len(rows), "percentile": _pct(f["index"], [r["index"] for r in rows])}
    band = [r for r in facts if r["age_band"] == f["age_band"]]
    return {"song": f, "comparisons": [c for c in (
        vs(band, f"all songs of {f['age_band']}"),
        vs([r for r in band if r["language"] == f["language"]], f"{f['language']} songs of {f['age_band']}") if f["language"] else None,
        vs([r for r in band if r["label"] == f["label"]], f"{f['label']} songs of {f['age_band']}"),
        vs([r for r in band if r["category"] == f["category"]], f"{f['category']} songs of {f['age_band']}") if f["category"] else None,
    ) if c]}


PANELS = {"emerging": emerging_hits, "cross_label": cross_label, "catalog": catalog_opportunities,
          "artists": artist_portfolio, "anomalies": anomalies}


def ai_assist():
    """The AI-assist notes (data/ai_assist.json): a written assessment prepared from these panels. Not live."""
    path = config.DB_FILE.parent / "ai_assist.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def panel(conn, name, **kw):
    facts, meta = song_facts(conn)
    if name not in PANELS:
        raise ValueError("unknown panel")
    if name == "artists":
        data = {"rows": artist_portfolio(facts, field=kw.get("field") or "singer")}
    else:
        data = PANELS[name](facts)
        data = data if isinstance(data, dict) else {"rows": data}
    notes = ai_assist() or {}
    key = f"artists:{kw.get('field') or 'singer'}" if name == "artists" else name
    note = (notes.get("panels") or {}).get(key)
    return {"meta": meta, **data, "ai_assist": {"text": note, "prepared_by": notes.get("prepared_by"),
                                               "prepared_on": notes.get("prepared_on"), "data_as_of": notes.get("data_as_of"),
                                               "note": notes.get("note")} if note else None}


def compact(rows, keep=("id", "title", "label", "language", "category", "singer", "music_director", "film", "first_upload",
                        "age_days", "views", "velocity", "index", "recent_rate", "acceleration", "likes_per_1k", "score",
                        "evidence", "projection_7d", "released", "years_since_release", "direction", "band_percentile",
                        "growth_pct", "age_band")):
    """Slim rows for the AI (fewer tokens)."""
    return [{k: r[k] for k in keep if k in r and r[k] not in (None, "", [])} for r in rows]
