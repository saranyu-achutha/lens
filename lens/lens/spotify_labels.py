"""Spotify label sweep: find the labels' new releases on Spotify, then link their tracks to the YouTube songs.

The first step of Spotify ID finding (user rule, 2026-11-04); the per-song search only looks for songs still without a
link after the day's sweep. For each label in config (youtube.labels, switched on):

  1. new releases:  album search  label:"<name>" tag:new   (Spotify: released in the last two weeks), every page, daily
  2. backfill:      album search  label:"<name>" year:<y>  from the period start's year on, page by page across runs
                    (Spotify shows at most 1,000 results a query); started again weekly
  3. each album released from the period start, once: /albums/{id}/tracks (one call), every track scored against that
     label's songs of the period with the usual rules (songs of different labels never match)

A confident match is linked like a search hit (and one /tracks/{id} call fetches its ISRC); an uncertain one goes to
Review. Guards from the 2026-11-04 test: a track links automatically only if its own title (without "From ...",
"feat.") shares a word with the song's title, and a song that would get several different tracks in one sweep sends
them all to Review. Calls come from the same per-account budget as re-discovery (primary above its reserve first,
then account 2), at most songs.spotify_label_sweep_per_run a run, one a second.
"""
import json
from datetime import date, datetime, timedelta, timezone

from . import config, db, match, songs
from .spotify import BudgetExhausted, DailyLimitReached, Spotify, SpotifyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_label_album (
    album_id      TEXT PRIMARY KEY,
    label         TEXT,
    name          TEXT,
    release_date  TEXT,
    total_tracks  INTEGER,
    seen_at       TEXT,
    fetched_at    TEXT,       -- its tracks were read and matched
    tracks        INTEGER,
    linked        INTEGER,    -- tracks linked automatically
    review        INTEGER     -- tracks sent to Review
);
"""
JOB = "spotify_label_sweep"
DEFAULT_QUERIES = {"Sony Music India": ["Sony Music Entertainment India", "Sony Music India"]}
BACKFILL_EVERY_DAYS = 7


def ensure(conn):
    conn.executescript(SCHEMA)


def _state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set(conn, key, value):
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def queries(cfg):
    """{label: [Spotify label phrases]} for the switched-on labels."""
    extra = cfg["songs"].get("spotify_label_queries") or {}
    return {lb["name"]: extra.get(lb["name"]) or DEFAULT_QUERIES.get(lb["name"]) or [lb["name"]]
            for lb in cfg["youtube"]["labels"] if lb.get("enabled", True)}


def today():
    return datetime.now(timezone.utc).date().isoformat()


def done_today(conn, cfg):
    """True when today's new-release pass is done for every label and every album it found has been read."""
    ensure(conn)
    for lb in queries(cfg):
        if _state(conn, f"spotify_label_new:{lb}", {}).get("day") != today():
            return False
    since = config.window(cfg)[0]
    return not conn.execute("SELECT 1 FROM spotify_label_album WHERE fetched_at IS NULL AND release_date >= ? LIMIT 1",
                            (since,)).fetchone()


def due(conn, cfg):
    """Work left: today's new-release pass, albums not read yet, or backfill pages."""
    if done_today(conn, cfg):
        return any(not _backfill(conn, cfg, lb).get("complete") for lb in queries(cfg))
    return True


def _backfill(conn, cfg, lb):
    """Backfill position for a label: {phrase, year, offset, complete, started}; restarted every BACKFILL_EVERY_DAYS."""
    since = config.window(cfg)[0]
    b = _state(conn, f"spotify_label_backfill:{lb}") or {}
    started = b.get("started") or ""
    if not b or started < (date.today() - timedelta(days=BACKFILL_EVERY_DAYS)).isoformat():
        b = {"phrase": 0, "year": int(since[:4]), "offset": 0, "complete": False, "started": today()}
        _set(conn, f"spotify_label_backfill:{lb}", b)
    return b


def account(conn, cfg, secrets):
    """The account to sweep with: the primary while it has calls above its reserve, else account 2."""
    for acct, ok in (("primary", secrets.get("SPOTIFY_CLIENT_ID") and secrets.get("SPOTIFY_CLIENT_SECRET")),
                     ("second", secrets.get("SPOTIFY_CLIENT_ID_2") and secrets.get("SPOTIFY_CLIENT_SECRET_2"))):
        if ok and not songs.rediscover_blocked(conn, acct) and songs.rediscover_budget(conn, cfg, acct)[2] > 0:
            return acct
    return None


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    ensure(conn)
    since, until = config.window(cfg)
    ts = db.now_iso()
    acct = account(conn, cfg, secrets)
    if not acct:
        log("Spotify label sweep: no account has calls to spare now (or both are waiting out a block)")
        return {"status": "ok", "label_sweep_calls": 0}
    budget = min(int(cfg["songs"].get("spotify_label_sweep_per_run", 200)), songs.rediscover_budget(conn, cfg, acct)[2])
    factory = Spotify.second_account_from_secrets if acct == "second" else Spotify.from_secrets
    sp = factory(secrets, market=cfg["discovery"]["market"], max_calls=budget, stop_on_rate_limit=True)
    log(f"Spotify label sweep on the {songs.ACCOUNT_NAME[acct]}: up to {budget} calls")

    # The period's songs by label, with their videos, for the match rules.
    lab_of = config.channel_labels(cfg)
    by_label = {}
    for t in conn.execute("""SELECT t.id, coalesce(t.yt_title, t.title) title,
                                    EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify'
                                            AND p.review_state IN ('auto','approved')) linked
                             FROM track t WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?""",
                          (since, until)):
        vids = conn.execute("""SELECT title, description, published_at, duration_ms, channel FROM platform_item
                               WHERE track_id=? AND platform='youtube' AND review_state IN ('auto','approved','song')
                               ORDER BY asset_type IN ('audio','lyric') DESC, published_at LIMIT 3""", (t["id"],)).fetchall()
        if vids:
            by_label.setdefault(lab_of.get(vids[0]["channel"], config.FIRST_LABEL), []).append(
                {"id": t["id"], "title": t["title"], "linked": t["linked"], "keys": match.block_keys(t["title"]), "videos": vids})
    rejected = {(r[0], r[1]) for r in conn.execute(
        "SELECT p.platform_id, m.track_id FROM match_rejection m JOIN platform_item p ON p.id=m.platform_item_id WHERE p.platform='spotify'")}
    known_tracks = {r[0] for r in conn.execute("SELECT platform_id FROM platform_item WHERE platform='spotify'")}
    st = {"label_sweep_calls": 0, "label_sweep_albums": 0, "label_sweep_tracks": 0, "label_sweep_linked": 0,
          "label_sweep_review": 0, "label_sweep_no_song": 0}
    stop_reason = None

    def search(q, offset):
        page = sp.get("/search", {"q": q, "type": "album", "market": sp.market, "limit": 10, "offset": offset})
        return [a for a in page["albums"]["items"] if a]

    def remember(lb, albums):
        for a in albums:
            if (a.get("release_date") or "") >= since:
                conn.execute("""INSERT INTO spotify_label_album (album_id, label, name, release_date, total_tracks, seen_at)
                                VALUES (?,?,?,?,?,?) ON CONFLICT(album_id) DO NOTHING""",
                             (a["id"], lb, a["name"], a.get("release_date"), a.get("total_tracks"), ts))
        conn.commit()

    try:
        # 1. today's new releases, every label
        for lb, phrases in queries(cfg).items():
            if should_stop():
                raise songs.Cancelled()
            s = _state(conn, f"spotify_label_new:{lb}", {})
            if s.get("day") == today():
                continue
            for phrase in phrases:
                offset = 0
                while offset < 1000:
                    try:
                        items = search(f'label:"{phrase}" tag:new', offset)
                    except SpotifyError as e:
                        if e.status in (400, 404):  # the filter is not accepted: the backfill covers this label
                            items = []
                        else:
                            raise
                    remember(lb, items)
                    if len(items) < 10:
                        break
                    offset += 10
            _set(conn, f"spotify_label_new:{lb}", {"day": today()})
            conn.commit()
        # 2. albums not read yet: their tracks, matched and linked
        pending = conn.execute("SELECT album_id, label, name, release_date FROM spotify_label_album WHERE fetched_at IS NULL "
                               "AND release_date >= ? ORDER BY release_date DESC", (since,)).fetchall()
        for a in pending:
            if should_stop():
                raise songs.Cancelled()
            _read_album(conn, sp, dict(a), by_label.get(a["label"], []), rejected, known_tracks, since, ts, st, log)
        # 3. backfill pages with what is left of the budget, label by label
        for lb, phrases in queries(cfg).items():
            b = _backfill(conn, cfg, lb)
            while not b["complete"]:
                if should_stop():
                    raise songs.Cancelled()
                items = search(f'label:"{phrases[b["phrase"]]}" year:{b["year"]}', b["offset"])
                remember(lb, items)
                for a in items:
                    row = conn.execute("SELECT fetched_at FROM spotify_label_album WHERE album_id=?", (a["id"],)).fetchone()
                    if row and not row[0]:
                        _read_album(conn, sp, {"album_id": a["id"], "label": lb, "name": a["name"], "release_date": a.get("release_date")},
                                    by_label.get(lb, []), rejected, known_tracks, since, ts, st, log)
                b["offset"] += 10
                if len(items) < 10 or b["offset"] >= 1000:  # this query is used up: next year, then next phrase
                    b["offset"] = 0
                    if b["year"] < int(until[:4]):
                        b["year"] += 1
                    else:
                        b["year"], b["phrase"] = int(since[:4]), b["phrase"] + 1
                        b["complete"] = b["phrase"] >= len(phrases)
                _set(conn, f"spotify_label_backfill:{lb}", b)
                conn.commit()
    except DailyLimitReached as e:
        stop_reason = str(e)
    except BudgetExhausted:
        stop_reason = f"run budget of {budget} calls used; the next run continues"
    except SpotifyError as e:
        if e.status != 429:
            raise
        stop_reason = f"Spotify {songs.ACCOUNT_NAME[acct]} rate limited; retry after {e.retry_after or 'the provider window'} s"
        until_ts = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after or 3600)
        _set_raw(conn, f"spotify_{acct}_blocked_until", until_ts.isoformat())
    conn.commit()
    st["label_sweep_calls"] = sp.calls
    st["label_sweep_account"] = acct
    log(f"Spotify label sweep: {st['label_sweep_albums']} albums read, {st['label_sweep_tracks']} tracks, "
        f"{st['label_sweep_linked']} linked, {st['label_sweep_review']} to Review, {st['label_sweep_no_song']} with no YouTube song "
        f"({sp.calls} calls)" + (f"; {stop_reason}" if stop_reason else ""))
    return dict(st, status="ok", **({"stop_reason": stop_reason} if stop_reason else {}))


def _set_raw(conn, key, value):
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _read_album(conn, sp, a, label_songs, rejected, known_tracks, since, ts, st, log):
    """One album's tracks: score each against the label's songs, link the confident ones, park the rest for Review."""
    res = sp.get(f"/albums/{a['album_id']}/tracks", {"limit": 50, "market": sp.market})
    album = {"id": a["album_id"], "name": a["name"], "release_date": a["release_date"]}
    hits = {}  # song id -> [(total, detail, track)]
    n = linked = review = 0
    for t in res.get("items") or []:
        n += 1
        if t["id"] in known_tracks:
            continue
        track = {"title": t["name"], "artists": [r["name"] for r in t["artists"]], "release_title": a["name"],
                 "release_date": a["release_date"], "duration_ms": t.get("duration_ms")}
        core = match.parse_spotify_title(t["name"])[0]
        keys = match.block_keys(t["name"])
        best = None
        for song in label_songs:
            if not keys & song["keys"] or (t["id"], song["id"]) in rejected:
                continue
            for v in song["videos"]:
                total, detail = match.score(track, {"title": v["title"], "description": v["description"],
                                                    "published_at": v["published_at"], "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                if best is None or total > best[0]:
                    best = (total, detail, song)
        decision = match.decide(best[0], best[1]) if best else None
        if not decision:
            st["label_sweep_no_song"] += 1
            continue
        total, detail, song = best
        # guard: the track's own title must share a word with the song (not only its "From <film>" or a singer's name)
        if decision == "auto" and not (match.block_keys(core) & song["keys"]):
            decision = "pending"
        hits.setdefault(song["id"], []).append((total, dict(detail, via="label_sweep", decision=decision), dict(t, album=album)))
    for sid, found in hits.items():
        song = next(s for s in label_songs if s["id"] == sid)
        autos = [h for h in found if h[1]["decision"] == "auto"]
        # guard: several different tracks for one song in one sweep is suspicious unless they are one recording
        if len({h[2]["id"] for h in autos}) > 1 and not all(songs.same_recording(autos[0][2], h[2]) for h in autos[1:]):
            for h in autos:
                h[1]["decision"] = "pending"
        for total, detail, t in sorted(found, key=lambda h: -h[0]):
            decision = detail.pop("decision")
            if song["linked"]:
                continue  # the song already has its Spotify track; nothing to add from the sweep
            if decision == "auto":
                full = sp.get(f"/tracks/{t['id']}", {"market": sp.market})  # its ISRC (album tracks leave it out)
                t = dict(t, external_ids=full.get("external_ids") or {}, explicit=full.get("explicit"))
                songs.attach_spotify(conn, sid, t, "auto", detail, since, ts)
                song["linked"] = True
                linked += 1
            else:
                songs.attach_spotify(conn, sid, t, "pending", detail, since, ts)
                review += 1
            known_tracks.add(t["id"])
    st["label_sweep_linked"] += linked
    st["label_sweep_review"] += review
    conn.execute("UPDATE spotify_label_album SET fetched_at=?, tracks=?, linked=?, review=? WHERE album_id=?",
                 (ts, n, linked, review, a["album_id"]))
    conn.commit()
    st["label_sweep_albums"] += 1
    st["label_sweep_tracks"] += n
