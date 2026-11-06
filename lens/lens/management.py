"""Management summary (POC, 2026-11-06): top songs and new releases, top artists, top labels, language-wise and genre-wise
performance, ranked by Spotify (not YouTube).

Week = the last 7 completed days (ending on the last finished day), compared with the 7 days before. Growth, not totals:
each item's gain is its reading minus its own previous reading, counted on the day of the newer reading, so late Spotify
counts land on the next reading and nothing is lost (as in the label report). Songs of the period only.

Attribution: a song with several artists counts in full for each (artist charts); a song with several genres follows the
chosen method: split (1/k to each, totals stay 100%), primary (the first only) or full (each in full, totals above 100%).
Genre sources: our category (every song, one value) and Apple Music's genre (about 44% of songs, one value; Spotify gives
no genres to development-mode apps).
"""
from collections import defaultdict
from datetime import date, timedelta

from . import config

KEYS = (("sp", "spotify", "plays"), ("yt", "youtube", "views"), ("ytm", "ytmusic", "plays"), ("js", "jiosaavn", "plays"))
NEW_RELEASE_DAYS = 30
GENRE_METHODS = {"split": "Split equally", "primary": "Primary only", "full": "Full credit to each"}


def _song_gains(conn, ids, start, end):
    """{song: {key: {day: gain}}} for days start..end, from each item's previous reading."""
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    first = (date.fromisoformat(start) - timedelta(days=60)).isoformat()
    for key, plat, metric in KEYS:
        for t, d, g in conn.execute("""
            WITH s AS (SELECT p.track_id t, s.capture_date d, s.value v,
                              lag(s.value) OVER (PARTITION BY s.platform_item_id ORDER BY s.capture_date) pv
                       FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                       WHERE s.metric=? AND p.platform=? AND p.review_state IN ('auto','approved','song') AND p.track_id IS NOT NULL
                         AND s.capture_date >= ? AND s.capture_date <= ?)
            SELECT t, d, sum(v - pv) FROM s WHERE d >= ? AND pv IS NOT NULL GROUP BY t, d""", (metric, plat, first, end, start)):
            if t in ids:
                out[t][key][d] += int(round(g or 0))
    return out


def compute(conn, genre_source="category", method="split", cfg=None):
    cfg = cfg or config.load()
    since, until = config.window(cfg)
    labels_of = config.channel_labels(cfg)
    today = date.today().isoformat()
    end = conn.execute("SELECT max(day) FROM song_daily WHERE day < ?", (today,)).fetchone()[0] or today
    e = date.fromisoformat(end)
    wk = ((e - timedelta(days=6)).isoformat(), end)
    pw = ((e - timedelta(days=13)).isoformat(), (e - timedelta(days=7)).isoformat())
    span = ((e - timedelta(days=27)).isoformat(), end)  # 4 weeks for the language trend
    songs = {}
    for r in conn.execute("""SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) d,
                                    (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) ch,
                                    (SELECT platform_id FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
                                       AND p.review_state IN ('auto','approved','song') ORDER BY published_at LIMIT 1) vid
                             FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                               AND t.old_release IS NULL""", (since, until)):
        lab = labels_of.get(r["ch"]) if r["ch"] else None
        if lab:
            songs[r["id"]] = {"id": r["id"], "title": r["title"], "label": lab, "release": r["d"], "yt_video_id": r["vid"]}
    meta = defaultdict(lambda: defaultdict(list))
    for tid, field, value in conn.execute("""SELECT track_id, field, value FROM song_meta WHERE field IN
                                             ('language', 'category', 'genre', 'spotify_artist', 'singer') ORDER BY track_id, field, position"""):
        if tid in songs and value not in meta[tid][field]:
            meta[tid][field].append(value)
    gains = _song_gains(conn, set(songs), span[0], end)
    in_ = lambda days, a, b: sum(v for d, v in days.items() if a <= d <= b)
    for sid, s in songs.items():
        g = gains.get(sid, {})
        for key, _, _ in KEYS:
            s[key + "_week"] = in_(g.get(key, {}), *wk)
            s[key + "_prev"] = in_(g.get(key, {}), *pw)
        s["language"] = (meta[sid].get("language") or ["Unknown"])[0]
        s["artists"] = meta[sid].get("spotify_artist") or meta[sid].get("singer") or []
        s["on_spotify"] = bool(g.get("sp")) or conn.execute(
            "SELECT 1 FROM platform_item WHERE track_id=? AND platform='spotify' AND review_state IN ('auto','approved') LIMIT 1", (sid,)).fetchone() is not None
    rows = list(songs.values())
    pick = ("id", "title", "label", "release", "language", "yt_video_id", "sp_week", "sp_prev", "yt_week", "ytm_week", "js_week")
    top = sorted((s for s in rows if s["sp_week"] > 0), key=lambda s: -s["sp_week"])
    new_from = (e - timedelta(days=NEW_RELEASE_DAYS - 1)).isoformat()
    releases = [s for s in top if s["release"] >= new_from]

    def group(key_of, weight=lambda s, n: 1.0, primary=False):
        acc = defaultdict(lambda: {"sp_week": 0.0, "sp_prev": 0.0, "yt_week": 0.0, "songs": 0})
        for s in rows:
            ks = key_of(s)
            for i, k in enumerate(ks):
                w = weight(s, len(ks)) if i == 0 or not primary else 0
                if not w:
                    continue
                a = acc[k]
                a["sp_week"] += s["sp_week"] * w
                a["sp_prev"] += s["sp_prev"] * w
                a["yt_week"] += s["yt_week"] * w
                a["songs"] += 1
        tot = sum(a["sp_week"] for a in acc.values()) or 0
        out = []
        for k, a in acc.items():
            out.append({"name": k, "songs": a["songs"], "sp_week": round(a["sp_week"]), "sp_prev": round(a["sp_prev"]),
                        "yt_week": round(a["yt_week"]), "share": round(100 * a["sp_week"] / tot, 1) if tot else None,
                        "change": round(100 * (a["sp_week"] - a["sp_prev"]) / a["sp_prev"], 1) if a["sp_prev"] else None})
        if not full_prev:
            for x in out:
                x["change"] = None  # the week before started before Spotify readings did: no fair comparison yet
        out.sort(key=lambda x: -x["sp_week"])
        for i, x in enumerate(out, 1):
            x["rank"] = i
        return out

    from .market_share import data_started
    started = data_started(conn)
    full_prev = bool(started.get("spotify")) and started["spotify"] <= pw[0]  # the week before is fully read
    artists = group(lambda s: s["artists"])  # full credit to each credited artist
    labels = group(lambda s: [s["label"]])
    languages = group(lambda s: [s["language"]])
    gsrc = "genre" if genre_source == "apple" else "category"
    weight = (lambda s, n: 1.0 / n) if method == "split" else (lambda s, n: 1.0)
    genres = group(lambda s: meta[s["id"]].get(gsrc) or ["Unknown"], weight, primary=method == "primary")
    # language trend: Spotify plays gained per day, top 6 languages
    lang_days = defaultdict(lambda: defaultdict(int))
    for sid, s in songs.items():
        for d, v in gains.get(sid, {}).get("sp", {}).items():
            lang_days[s["language"]][d] += v
    trend = [{"name": l["name"], "points": [{"x": d, "y": lang_days[l["name"]][d]} for d in sorted(lang_days[l["name"]])]}
             for l in languages[:6]]
    # the genre investigation: how many songs have several values, and coverage of each source
    multi = {src: sum(1 for sid in songs if len(meta[sid].get(src) or []) > 1) for src in ("category", "genre", "spotify_artist", "singer")}
    cover = {src: sum(1 for sid in songs if meta[sid].get(src)) for src in ("category", "genre", "language", "spotify_artist")}
    sp_week, sp_prev = sum(s["sp_week"] for s in rows), sum(s["sp_prev"] for s in rows)
    return {"week": wk, "prev_week": pw, "report_day": end, "songs": len(rows), "on_spotify": sum(s["on_spotify"] for s in rows),
            "sp_week": sp_week, "sp_prev": sp_prev, "yt_week": sum(s["yt_week"] for s in rows),
            "change": round(100 * (sp_week - sp_prev) / sp_prev, 1) if sp_prev and full_prev else None, "full_prev": full_prev, "started": started,
            "releases_week": sum(1 for s in rows if wk[0] <= s["release"] <= wk[1]),
            "top_songs": [{k: s[k] for k in pick} for s in top[:15]], "top_releases": [{k: s[k] for k in pick} for s in releases[:15]],
            "new_release_days": NEW_RELEASE_DAYS, "top_artists": artists[:15], "labels": labels, "languages": languages,
            "language_trend": trend, "genres": genres, "genre_source": genre_source, "genre_method": method, "genre_methods": GENRE_METHODS,
            "genre_check": {"songs": len(rows), "multi": multi, "coverage": cover,
                            "apple_genres_that_are_languages": sorted({g for sid in songs for g in meta[sid].get("genre", [])
                                                                       if g in ("Telugu", "Tamil", "Malayalam", "Kannada", "Bengali", "Punjabi", "Marathi", "Gujarati")})}}
