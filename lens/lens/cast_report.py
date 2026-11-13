"""Cast & featuring (2026-11-13): how the actors and featured artists credited on songs perform on every platform.

Client and developer (page #cast, /api/cast, /api/cast/detail). Cast comes from the credits in the YouTube descriptions
(song_meta field 'cast', "Cast / featuring"); a song with several cast members counts in full for each of them.

Numbers come from song_daily (one row per song per day), songs of the period only (first YouTube upload from the period
start, not released before it), as on the Dashboard and Management pages:
  * gain  = the sum of a platform's daily gains over the window (each reading minus the song's previous reading), so a
            late reading lands on the next day and nothing is counted twice;
  * total = the song's latest reading on or before the window's last day;
  * window = the last N completed days (the last finished server day back; today is still being read).
Platforms: YouTube views, Spotify / YouTube Music / JioSaavn plays (audio streams = their sum, never added to YouTube),
YouTube Shorts and Instagram Reels made with the song's official audio, and the best Deezer rank. Instagram keys start
with "ig_" so client replies drop them (studio.no_instagram).

Insights are rule-based (no AI): fixed rules over these numbers, each with the figures it rests on.
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from . import config

# key, song_daily column (total), gain column, name
PLATFORMS = (("yt", "yt_views", "yt_views_gain", "YouTube views"),
             ("sp", "sp_plays", "sp_plays_gain", "Spotify plays"),
             ("ytm", "ytm_plays", "ytm_plays_gain", "YouTube Music plays"),
             ("js", "js_plays", "js_plays_gain", "JioSaavn plays"),
             ("shorts", "yt_shorts", "yt_shorts_gain", "YouTube Shorts made"),
             ("ig_reels", "ig_reels", "ig_reels_gain", "Instagram Reels made"))
AUDIO = ("sp", "ytm", "js")
SORTS = {"yt", "sp", "ytm", "js", "audio", "shorts", "ig_reels", "songs", "momentum", "per_song", "audio_per_100", "name"}
DAY_CHOICES = (7, 14, 30, 60, 90)
MIN_SONGS_RANKED = 2          # rules that compare people need at least this many songs (one song is luck, not a pattern)


def _end_day(conn):
    today = date.today().isoformat()
    return conn.execute("SELECT max(day) FROM song_daily WHERE day < ?", (today,)).fetchone()[0] or today


def _period_songs(conn, cfg):
    since, until = config.window(cfg)
    rows = conn.execute("""SELECT t.id, coalesce(t.yt_title, t.title) title, substr(t.first_published,1,10) d,
                                  (SELECT platform_id FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube'
                                     AND p.review_state IN ('auto','approved','song') ORDER BY published_at LIMIT 1) vid
                           FROM track t WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                             AND t.old_release IS NULL""", (since, until)).fetchall()
    return {r[0]: {"id": r[0], "title": r[1], "release": r[2], "yt_video_id": r[3]} for r in rows}, since


def _meta(conn, ids, fields):
    """{field: {song: [(key, value), ...]}} for the songs."""
    out = {f: defaultdict(list) for f in fields}
    if not ids:
        return out
    ph = ",".join("?" * len(fields))
    for t, f, k, v in conn.execute(f"SELECT track_id, field, value_key, value FROM song_meta WHERE field IN ({ph})", fields):
        if t in ids:
            out[f][t].append((k, v))
    return out


def _daily(conn, ids, start, end):
    """Per song: gains per platform per day (start..end) and the latest totals on or before end."""
    gains = defaultdict(lambda: defaultdict(dict))   # song -> key -> {day: gain}
    totals = defaultdict(dict)                        # song -> key -> total
    best_dz = {}
    cols = ", ".join(f"{c}, {g}" for _, c, g, _ in PLATFORMS)
    for row in conn.execute(f"SELECT song_id, day, dz_rank, {cols} FROM song_daily WHERE day <= ? ORDER BY day", (end,)):
        sid, day = row[0], row[1]
        if sid not in ids:
            continue
        if row[2] and day == end:
            best_dz[sid] = row[2]
        for i, (k, _, _, _) in enumerate(PLATFORMS):
            tot, g = row[3 + 2 * i], row[4 + 2 * i]
            if tot is not None:
                totals[sid][k] = tot           # rows come in day order: the last one wins
            if g is not None and day >= start:
                gains[sid][k][day] = g
    return gains, totals, best_dz


def _days(start, end):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


def _build(conn, days, cfg):
    cfg = cfg or config.load()
    days = max(1, min(int(days or 30), 365))
    end = _end_day(conn)
    start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    songs, since = _period_songs(conn, cfg)
    ids = set(songs)
    meta = _meta(conn, ids, ("cast", "label", "language", "film", "singer", "music_director"))
    # read at least 14 days so "this week vs the week before" works for a 7-day window too; window sums use start..end
    gains, totals, dz = _daily(conn, ids, min(start, (date.fromisoformat(end) - timedelta(days=13)).isoformat()), end)
    span = _days(start, end)
    mid = (date.fromisoformat(end) - timedelta(days=6)).isoformat()             # last 7 days
    prev = ((date.fromisoformat(end) - timedelta(days=13)).isoformat(), (date.fromisoformat(end) - timedelta(days=7)).isoformat())
    for sid, s in songs.items():
        s["label"] = (meta["label"].get(sid) or [(None, None)])[0][1]
        s["language"] = (meta["language"].get(sid) or [(None, None)])[0][1]
        s["film"] = (meta["film"].get(sid) or [(None, None)])[0][1]
        g = gains.get(sid, {})
        s["gain"] = {k: sum(v for d_, v in g.get(k, {}).items() if d_ >= start) for k, *_ in PLATFORMS}
        s["gain"]["audio"] = sum(s["gain"][k] for k in AUDIO)
        s["total"] = {k: totals.get(sid, {}).get(k) for k, *_ in PLATFORMS}
        s["dz_rank"] = dz.get(sid)
        s["has_cast"] = sid in meta["cast"]
        s["new"] = s["release"] >= start
        # a 7-day window needs 14 days of data for the week before: read from the full gains when the window is shorter
        s["wk"] = {k: sum(v for d_, v in g.get(k, {}).items() if d_ >= mid) for k in ("yt",) + AUDIO}
        s["pwk"] = {k: sum(v for d_, v in g.get(k, {}).items() if prev[0] <= d_ <= prev[1]) for k in ("yt",) + AUDIO}
    return {"cfg": cfg, "days": days, "start": start, "end": end, "since": since, "span": span, "songs": songs,
            "meta": meta, "gains": gains}


def _people(B, label=None, language=None):
    """One row per cast member over the window (songs filtered by label / language)."""
    songs, meta = B["songs"], B["meta"]
    people = {}
    for sid, cast in meta["cast"].items():
        s = songs.get(sid)
        if not s or (label and s["label"] != label) or (language and s["language"] != language):
            continue
        for key, name in cast:
            p = people.setdefault(key, {"key": key, "name": name, "song_ids": [], "names": defaultdict(int)})
            p["song_ids"].append(sid)
            p["names"][name] += 1
    rows = []
    for key, p in people.items():
        ss = [songs[i] for i in p["song_ids"]]
        r = {"key": key, "name": max(p["names"], key=p["names"].get), "songs": len(ss), "new": sum(s["new"] for s in ss)}
        for k, *_ in PLATFORMS:
            r[k] = sum(s["gain"][k] for s in ss)
            r[f"{k}_total"] = sum(s["total"][k] or 0 for s in ss)
        r["audio"] = sum(r[k] for k in AUDIO)
        r["audio_total"] = sum(r[f"{k}_total"] for k in AUDIO)
        r["per_song"] = round(r["yt"] / len(ss)) if ss else 0
        r["audio_per_song"] = round(r["audio"] / len(ss)) if ss else 0
        r["audio_per_100"] = round(100 * r["audio"] / r["yt"], 1) if r["yt"] > 0 else None
        wk, pwk = sum(s["wk"]["yt"] for s in ss), sum(s["pwk"]["yt"] for s in ss)
        awk, apwk = sum(s["wk"][k] for s in ss for k in AUDIO), sum(s["pwk"][k] for s in ss for k in AUDIO)
        r["momentum"] = round(100 * (wk - pwk) / pwk) if pwk > 0 else None
        r["audio_momentum"] = round(100 * (awk - apwk) / apwk) if apwk > 0 else None
        r["week_yt"], r["week_audio"], r["prev_yt"], r["prev_audio"] = wk, awk, pwk, apwk
        labels = defaultdict(int)
        for s in ss:
            labels[s["label"] or "Unknown"] += 1
        r["labels"] = sorted(labels, key=labels.get, reverse=True)
        langs = defaultdict(int)
        for s in ss:
            if s["language"]:
                langs[s["language"]] += 1
        r["languages"] = sorted(langs, key=langs.get, reverse=True)[:3]
        films = {s["film"] for s in ss if s["film"]}
        r["films"] = sorted(films)[:3]
        top = max(ss, key=lambda s: (s["gain"]["yt"], s["gain"]["audio"]))
        r["top_song"] = {"id": top["id"], "title": top["title"], "yt_video_id": top["yt_video_id"], "yt": top["gain"]["yt"]}
        r["top_share"] = round(100 * top["gain"]["yt"] / r["yt"]) if r["yt"] > 0 else None
        dzs = [s["dz_rank"] for s in ss if s["dz_rank"]]
        r["dz_best"] = min(dzs) if dzs else None
        r["song_ids"] = p["song_ids"]
        rows.append(r)
    return rows


def _series(B, ids, keys):
    """Daily gains per platform key over the window for a set of songs: {key: [{x, y}]}."""
    out = {}
    for k in keys:
        acc = defaultdict(int)
        plats = AUDIO if k == "audio" else (k,)
        for sid in ids:
            for pk in plats:
                for d_, v in B["gains"].get(sid, {}).get(pk, {}).items():
                    acc[d_] += v
        out[k] = [{"x": d_, "y": acc.get(d_, 0)} for d_ in B["span"]]
    return out


def _insights(B, rows, focus_label):
    """Rule-based findings for the client, each with the numbers it rests on."""
    out = []
    songs = B["songs"]
    ranked = [r for r in rows if r["songs"] >= MIN_SONGS_RANKED]

    def pick(xs, n=3):
        """The first n people with different song sets: cast credited on exactly the same songs show once ("+2 more")."""
        seen, res = {}, []
        for r in xs:
            sig = frozenset(r["song_ids"])
            if sig in seen:
                seen[sig]["also"].append(r["name"])
                continue
            if len(res) < n:
                item = dict(r, also=[])
                seen[sig] = item
                res.append(item)
        return res

    def nm(r):
        return r["name"] + (f" (+{len(r['also'])} more on the same songs)" if r.get("also") else "")

    def pct(v):  # +45% ... +280%, then 4.2× (a percentage in the thousands reads as noise)
        return f"{v:+d}%" if v < 300 else f"{1 + v / 100:.1f}×"

    def add(kind, title, text, people=(), tone="info"):
        out.append({"kind": kind, "title": title, "text": text, "tone": tone,
                    "people": [{"key": r["key"], "name": r["name"], "also": r.get("also", [])} for r in people]})

    # 1. who leads on video vs on audio (they are often not the same people)
    yt_lead = pick(sorted(rows, key=lambda r: r["yt"], reverse=True))
    au_lead = pick(sorted(rows, key=lambda r: r["audio"], reverse=True))
    if yt_lead and yt_lead[0]["yt"] > 0:
        same = {r["key"] for r in yt_lead} & {r["key"] for r in au_lead}
        add("leaders", "Video leaders and audio leaders",
            f"{nm(yt_lead[0])} leads YouTube (+{yt_lead[0]['yt']:,} views); "
            f"{nm(au_lead[0])} leads audio streams (+{au_lead[0]['audio']:,}). "
            + ("Leading both: " + ", ".join(r["name"] for r in yt_lead if r["key"] in same) + "."
               if same else "No one is in both top threes: video fame and listening are different audiences, so plan them separately."),
            yt_lead[:1] + au_lead[:1], "good")
    # 2. rising: biggest week-on-week growth, only on a real base (the week before had at least the median weekly views)
    prevs = sorted(r["prev_yt"] for r in ranked if r["prev_yt"] > 0)
    floor = max(prevs[len(prevs) // 2] if prevs else 0, 10000)
    rising = pick(sorted([r for r in ranked if r["momentum"] is not None and r["prev_yt"] >= floor and r["momentum"] > 0],
                         key=lambda r: r["momentum"], reverse=True))
    if rising:
        add("rising", "Rising this week",
            "; ".join(f"{nm(r)}: YouTube views {pct(r['momentum'])} vs the week before (+{r['week_yt']:,} this week)" for r in rising)
            + f". Only people with at least {floor:,} views the week before are compared. Good candidates for a promotion push now.",
            rising, "good")
    # 3. audio-first: most audio streams per 100 YouTube views, with real volume on both
    tot_yt = sum(s["gain"]["yt"] for s in songs.values() if s["has_cast"])
    tot_au = sum(s["gain"]["audio"] for s in songs.values() if s["has_cast"])
    avg100 = round(100 * tot_au / tot_yt, 1) if tot_yt > 0 else None
    af = pick(sorted([r for r in ranked if r["audio_per_100"] is not None and r["audio"] >= 100000 and r["yt"] >= 100000],
                     key=lambda r: r["audio_per_100"], reverse=True))
    if af:
        add("audio_first", "Audio-first stars",
            "; ".join(f"{nm(r)}: {r['audio_per_100']} streams per 100 views (+{r['audio']:,} streams)" for r in af)
            + (f". Cast average {avg100}." if avg100 is not None else ".")
            + " Their songs are listened to, not only watched: prioritise them for streaming playlists and audio campaigns.", af, "good")
    # 4. consistent: best median YouTube gain per song, at least 3 songs
    cons = []
    for r in rows:
        if r["songs"] >= 3:
            med = median(songs[i]["gain"]["yt"] for i in r["song_ids"])
            if med > 0:
                cons.append(dict(r, med=med))
    cons = pick(sorted(cons, key=lambda r: r["med"], reverse=True))
    if cons:
        add("consistent", "Consistent performers",
            "; ".join(f"{nm(r)}: median +{int(r['med']):,} views a song over {r['songs']} songs" for r in cons)
            + ". A high median means most of their songs do well, not just one hit: lower-risk casting.", cons, "good")
    # 5. one-hit dependency
    dep = pick(sorted([r for r in ranked if r["top_share"] is not None and r["top_share"] >= 80 and r["yt"] >= 100000],
                      key=lambda r: r["yt"], reverse=True))
    if dep:
        add("dependency", "Carried by one song",
            "; ".join(f"{nm(r)}: {r['top_share']}% of {r['songs']} songs' views from \"{r['top_song']['title'][:40]}\"" for r in dep)
            + ". Their totals rest on one hit, so judge them by the next release.", dep, "warn")
    # 6. client focus label: its cast vs the market, and strong cast it has not worked with
    if focus_label:
        mine = [r for r in rows if focus_label in r["labels"]]
        f_songs = [s for s in songs.values() if s["has_cast"] and s["label"] == focus_label]
        all_cast_songs = [s for s in songs.values() if s["has_cast"]]
        if f_songs and all_cast_songs and sum(s["gain"]["yt"] + s["gain"]["audio"] for s in all_cast_songs) > 0:
            f_med = median([s["gain"]["yt"] for s in f_songs])
            m_med = median([s["gain"]["yt"] for s in all_cast_songs])
            fa_med = median([s["gain"]["audio"] for s in f_songs]) if f_songs else 0
            ma_med = median([s["gain"]["audio"] for s in all_cast_songs])
            best = pick(sorted(mine, key=lambda r: r["yt"] + r["audio"], reverse=True))
            add("focus", f"{focus_label}: cast on its songs",
                f"{len(f_songs)} {focus_label} songs credit a cast ({len(mine)} people). Median per song +{int(f_med):,} views "
                f"(all labels +{int(m_med):,}) and +{int(fa_med):,} audio streams (all labels +{int(ma_med):,})."
                + (" Strongest: " + "; ".join(f"{nm(r)} +{r['yt']:,} views, +{r['audio']:,} streams" for r in best) + "." if best else ""),
                best, "good" if f_med >= m_med else "warn")
            others = pick(sorted([r for r in ranked if focus_label not in r["labels"] and r["yt"] + r["audio"] > 0],
                                 key=lambda r: r["yt"] + r["audio"], reverse=True), 5)
            if others:
                add("talent", f"Cast to watch for {focus_label}",
                    "Performing strongly with other labels and not yet on a " + focus_label + " song: "
                    + "; ".join(f"{nm(r)} +{r['yt']:,} views, +{r['audio']:,} streams ({', '.join(r['labels'][:2])})" for r in others)
                    + ".", others, "info")
    # 7. songs with a credited cast vs songs without
    w = [s for s in songs.values() if s["has_cast"]]
    wo = [s for s in songs.values() if not s["has_cast"]]
    if w and wo and sum(s["gain"]["yt"] for s in songs.values()) > 0:
        mw, mwo = median(s["gain"]["yt"] for s in w), median(s["gain"]["yt"] for s in wo)
        aw, awo = median(s["gain"]["audio"] for s in w), median(s["gain"]["audio"] for s in wo)
        ratio = (mw / mwo) if mwo > 0 else None
        add("benchmark", "Songs with a credited cast vs without",
            f"{len(w):,} of {len(songs):,} songs ({round(100 * len(w) / len(songs))}%) credit a cast. Median per song: "
            f"+{int(mw):,} vs +{int(mwo):,} YouTube views"
            + (f" ({ratio:.1f}×)" if ratio else "") + f", +{int(aw):,} vs +{int(awo):,} audio streams. "
            "Cast credits are mostly film songs, so this compares film songs with non-film releases as much as cast itself.", (), "info")
    # 8. cross-label demand
    cross = pick(sorted([r for r in rows if len([l for l in r["labels"] if l != "Unknown"]) >= 2 and r["yt"] + r["audio"] > 0],
                        key=lambda r: r["yt"], reverse=True))
    if cross:
        add("cross_label", "In demand across labels",
            "; ".join(f"{nm(r)} ({', '.join(r['labels'][:3])})" for r in cross)
            + ". Several labels release their songs: proven, but competed for.", cross, "info")
    return out


def compute(conn, days=30, label=None, language=None, sort="yt", order="desc", q=None, limit=50, offset=0,
            platform="yt", cfg=None):
    B = _build(conn, days, cfg)
    cfg = B["cfg"]
    rows = _people(B, label, language)
    songs = B["songs"]
    focus_label = (cfg.get("market_share") or {}).get("focus_label", "Times Music")  # the client's label, as on Market share
    cast_ids = {sid for sid, s in songs.items() if s["has_cast"]
                and (not label or s["label"] == label) and (not language or s["language"] == language)}
    all_ids = {sid for sid, s in songs.items() if (not label or s["label"] == label) and (not language or s["language"] == language)}

    def tot(ids, k):
        return sum(songs[i]["gain"][k] for i in ids)
    kpi = {"people": len(rows), "people_active": sum(1 for r in rows if r["yt"] > 0 or r["audio"] > 0),
           "songs": len(cast_ids), "period_songs": len(all_ids)}
    for k, *_ in PLATFORMS:
        kpi[k], kpi[f"{k}_all"] = tot(cast_ids, k), tot(all_ids, k)
    kpi["audio"], kpi["audio_all"] = sum(kpi[k] for k in AUDIO), sum(kpi[f"{k}_all"] for k in AUDIO)
    kpi["audio_per_100"] = round(100 * kpi["audio"] / kpi["yt"], 1) if kpi["yt"] > 0 else None

    plat = platform if platform in {"yt", "sp", "ytm", "js", "audio", "shorts", "ig_reels"} else "yt"
    top = sorted(rows, key=lambda r: r[plat], reverse=True)[:12]
    top5 = top[:5]
    trend = [{"key": r["key"], "name": r["name"], "points": _series(B, r["song_ids"], [plat])[plat]} for r in top5]
    # heatmap: top 15 by the platform, last 14 days of daily gains
    hm_days = B["span"][-14:]
    heat = []
    for r in top[:12]:
        s_ = _series(B, r["song_ids"], [plat])[plat]
        heat.append({"key": r["key"], "name": r["name"], "days": {p["x"]: p["y"] for p in s_ if p["x"] in hm_days}})
    mix = [{"key": r["key"], "name": r["name"], "yt": r["yt"], "sp": r["sp"], "ytm": r["ytm"], "js": r["js"]}
           for r in sorted(rows, key=lambda r: r["yt"] + r["audio"], reverse=True)[:10]]
    market_mix = {k: kpi[k] for k in ("yt", "sp", "ytm", "js")}

    ins = _insights(B, rows, focus_label) if not q else []
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["name"].lower() or any(ql in (f or "").lower() for f in r["films"])]
    key = sort if sort in SORTS else "yt"
    rev = order != "asc"
    if key == "name":
        rows.sort(key=lambda r: r["name"].lower(), reverse=rev)
    else:
        rows.sort(key=lambda r: (r[key] is not None, r[key] or 0, r["yt"]), reverse=rev)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    total = len(rows)
    lim, off = max(1, min(int(limit or 50), 200)), max(0, int(offset or 0))
    page = [{k: v for k, v in r.items() if k != "song_ids"} for r in rows[off:off + lim]]
    strip = lambda r: {k: v for k, v in r.items() if k != "song_ids"}
    labels = sorted({s["label"] for s in songs.values() if s["label"]})
    langs = defaultdict(int)
    for s in songs.values():
        if s["has_cast"] and s["language"]:
            langs[s["language"]] += 1
    return {"days": B["days"], "start": B["start"], "end": B["end"], "since": B["since"], "day_choices": DAY_CHOICES,
            "label": label, "language": language, "platform": plat, "focus_label": focus_label,
            "platforms": [{"key": k, "name": n} for k, _, _, n in PLATFORMS],
            "labels": labels, "languages": sorted(langs, key=langs.get, reverse=True),
            "kpi": kpi, "top": [strip(r) for r in top], "trend": trend, "heat": heat, "heat_days": hm_days,
            "mix": mix, "market_mix": market_mix, "insights": ins,
            "total": total, "offset": off, "limit": lim, "sort": key, "order": "desc" if rev else "asc", "rows": page}


def detail(conn, key, days=30, cfg=None):
    B = _build(conn, days, cfg)
    songs, meta = B["songs"], B["meta"]
    all_rows = _people(B)
    me = next((r for r in all_rows if r["key"] == key), None)
    if not me:
        raise ValueError("no songs credit this cast member in the period")
    ids = me["song_ids"]
    # rank among everyone, per platform
    ranks = {}
    for k in ("yt", "audio", "sp", "ytm", "js", "shorts", "ig_reels"):
        order = sorted(all_rows, key=lambda r: r[k], reverse=True)
        ranks[k] = next(i + 1 for i, r in enumerate(order) if r["key"] == key) if me[k] > 0 else None
    series = _series(B, ids, ["yt", "sp", "ytm", "js", "shorts", "ig_reels"])
    song_rows = []
    for sid in sorted(ids, key=lambda i: (songs[i]["gain"]["yt"], songs[i]["gain"]["audio"]), reverse=True):
        s = songs[sid]
        song_rows.append({"id": sid, "title": s["title"], "release": s["release"], "label": s["label"], "language": s["language"],
                          "film": s["film"], "yt_video_id": s["yt_video_id"], "new": s["new"], "dz_rank": s["dz_rank"],
                          **{k: s["gain"][k] for k in [p[0] for p in PLATFORMS] + ["audio"]},
                          **{f"{k}_total": s["total"][k] for k, *_ in PLATFORMS}})
    # collaborators on their songs: co-stars, singers, music directors (song count and the views those shared songs gained)
    collab = {}
    for f in ("cast", "singer", "music_director"):
        acc = {}
        for sid in ids:
            for k_, v_ in meta[f].get(sid, []):
                if f == "cast" and k_ == key:
                    continue
                a = acc.setdefault(k_, {"key": k_, "name": v_, "songs": 0, "yt": 0, "audio": 0})
                a["songs"] += 1
                a["yt"] += songs[sid]["gain"]["yt"]
                a["audio"] += songs[sid]["gain"]["audio"]
        collab[f] = sorted(acc.values(), key=lambda a: (a["songs"], a["yt"]), reverse=True)[:8]
    labels = defaultdict(lambda: {"songs": 0, "yt": 0, "audio": 0})
    for sid in ids:
        L = labels[songs[sid]["label"] or "Unknown"]
        L["songs"] += 1
        L["yt"] += songs[sid]["gain"]["yt"]
        L["audio"] += songs[sid]["gain"]["audio"]
    # platform mix vs the whole market's (songs of the period): index 100 = same share as the market
    mk = {k: sum(s["gain"][k] for s in songs.values()) for k in ("yt", "sp", "ytm", "js")}
    mk_t, me_t = sum(mk.values()), sum(me[k] for k in ("yt", "sp", "ytm", "js"))
    mix = [{"key": k, "value": me[k], "share": round(100 * me[k] / me_t, 1) if me_t else 0,
            "market_share": round(100 * mk[k] / mk_t, 1) if mk_t else 0,
            "index": round(100 * (me[k] / me_t) / (mk[k] / mk_t)) if me_t and mk_t and mk[k] else None}
           for k in ("yt", "sp", "ytm", "js")]
    avg = {k: round(sum(r[k] for r in all_rows) / len(all_rows)) for k in ("yt", "audio")} if all_rows else {"yt": 0, "audio": 0}
    facts = []
    if ranks["yt"]:
        facts.append(f"#{ranks['yt']} of {len(all_rows):,} cast members on YouTube views gained" + (f", #{ranks['audio']} on audio streams." if ranks["audio"] else "."))
    if avg["yt"]:
        facts.append(f"{me['yt'] / avg['yt']:.1f}× the average cast member's YouTube gain" + (f" and {me['audio'] / avg['audio']:.1f}× their audio gain." if avg["audio"] else "."))
    over = [m for m in mix if m["index"] and m["index"] >= 130 and m["value"] > 0]
    names = {"yt": "YouTube", "sp": "Spotify", "ytm": "YouTube Music", "js": "JioSaavn"}
    if over:
        facts.append("Over-indexes on " + ", ".join(f"{names[m['key']]} ({m['index'] / 100:.1f}× the market's share)" for m in over) + ".")
    if me["momentum"] is not None:
        facts.append(f"YouTube views {me['momentum']:+d}% this week vs the week before" + (f"; audio {me['audio_momentum']:+d}%." if me["audio_momentum"] is not None else "."))
    if me["top_share"] is not None and me["songs"] > 1:
        facts.append(f"Top song brings {me['top_share']}% of the YouTube gain.")
    return {"key": key, "name": me["name"], "days": B["days"], "start": B["start"], "end": B["end"],
            "person": {k: v for k, v in me.items() if k != "song_ids"}, "ranks": ranks, "people": len(all_rows),
            "series": series, "songs": song_rows, "collaborators": collab,
            "labels": [{"name": n, **v} for n, v in sorted(labels.items(), key=lambda x: x[1]["yt"], reverse=True)],
            "mix": mix, "facts": facts}
