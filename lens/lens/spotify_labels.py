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
them all to Review. Both accounts work at once (user rule, 2026-11-04), each in its own thread at one call a second,
within the same per-account budget as re-discovery (the primary above its reserve), at most
songs.spotify_label_sweep_per_run calls per account a run. The threads only make HTTP calls; every database write and
all matching happen in the main thread. Every track read is kept in spotify_label_track (Outputs: Spotify label songs).
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
-- Every track of the labels' releases from the period start (user request, 2026-11-04: "all songs from our
-- shortlisted labels" on Spotify, to merge with YouTube later), with the song it matched, if any.
CREATE TABLE IF NOT EXISTS spotify_label_track (
    track_id      TEXT PRIMARY KEY,   -- Spotify track ID
    label         TEXT,
    title         TEXT,
    artists       TEXT,
    album_id      TEXT,
    album         TEXT,
    release_date  TEXT,
    duration_ms   INTEGER,
    song_id       INTEGER,            -- the YouTube song it matched (linked or sent to Review), else NULL
    match         TEXT,               -- auto | pending | none | already (the song had its Spotify track already)
    score         REAL,
    seen_at       TEXT
);
CREATE INDEX IF NOT EXISTS spotify_label_track_label ON spotify_label_track(label, release_date);
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


def accounts(conn, cfg, secrets):
    """Accounts with calls to spare now (not waiting out a block): the primary above its reserve, and account 2."""
    out = []
    for acct, ok in (("primary", secrets.get("SPOTIFY_CLIENT_ID") and secrets.get("SPOTIFY_CLIENT_SECRET")),
                     ("second", secrets.get("SPOTIFY_CLIENT_ID_2") and secrets.get("SPOTIFY_CLIENT_SECRET_2"))):
        if ok and not songs.rediscover_blocked(conn, acct) and songs.rediscover_budget(conn, cfg, acct)[2] > 0:
            out.append(acct)
    return out


class _Lane:
    """One account working in its own thread: only HTTP calls (one a second, its own budget); results go back to the
    main thread, which does every database write and all matching."""
    def __init__(self, conn, cfg, secrets, acct):
        self.acct = acct
        budget = min(int(cfg["songs"].get("spotify_label_sweep_per_run", 200)), songs.rediscover_budget(conn, cfg, acct)[2])
        factory = Spotify.second_account_from_secrets if acct == "second" else Spotify.from_secrets
        self.sp = factory(secrets, market=cfg["discovery"]["market"], max_calls=budget, stop_on_rate_limit=True)
        self.budget, self.stop, self.retry_after = budget, None, None

    def call(self, path, params):
        """The reply, or None once this account has stopped (budget, daily limit, rate limit)."""
        if self.stop:
            return None
        try:
            return self.sp.get(path, dict(params, market=self.sp.market))
        except DailyLimitReached as e:
            self.stop = str(e)
        except BudgetExhausted:
            self.stop = f"run budget of {self.budget} calls used"
        except SpotifyError as e:
            if e.status == 429:
                self.stop, self.retry_after = f"rate limited (retry after {e.retry_after or '?'} s)", e.retry_after or 3600
            elif e.status in (400, 404):
                return {}
            else:
                self.stop = f"HTTP {e.status}"
        return None


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    from concurrent.futures import ThreadPoolExecutor
    ensure(conn)
    since, until = config.window(cfg)
    ts = db.now_iso()
    accts = accounts(conn, cfg, secrets)
    if not accts:
        log("Spotify label sweep: no account has calls to spare now (or both are waiting out a block)")
        return {"status": "ok", "label_sweep_calls": 0}
    lanes = [_Lane(conn, cfg, secrets, a) for a in accts]
    log("Spotify label sweep on " + " and ".join(f"{songs.ACCOUNT_NAME[l.acct]} (up to {l.budget} calls)" for l in lanes)
        + ", one call a second each, in parallel")

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
    labels = queries(cfg)

    def parallel(work, items):
        """Spread items over the lanes (round robin); each lane runs work(lane, its items) in its own thread."""
        shares = [items[i::len(lanes)] for i in range(len(lanes))]
        with ThreadPoolExecutor(len(lanes)) as ex:
            return [f.result() for f in [ex.submit(work, lane, share) for lane, share in zip(lanes, shares)]]

    def remember(lb, albums):
        for a in albums:
            if (a.get("release_date") or "") >= since:
                conn.execute("""INSERT INTO spotify_label_album (album_id, label, name, release_date, total_tracks, seen_at)
                                VALUES (?,?,?,?,?,?) ON CONFLICT(album_id) DO NOTHING""",
                             (a["id"], lb, a["name"], a.get("release_date"), a.get("total_tracks"), ts))

    def pages(lane, q, offset=0, until_short=True, max_pages=100):
        """Album search pages from offset on; returns (albums, next offset or None when the query is used up)."""
        out = []
        for _ in range(max_pages):
            if should_stop():
                break
            page = lane.call("/search", {"q": q, "type": "album", "limit": 10, "offset": offset})
            if page is None:
                return out, offset  # stopped: continue from here next run
            items = [a for a in (page.get("albums") or {}).get("items", []) if a]
            out += items
            offset += 10
            if len(items) < 10 or offset >= 1000:
                return out, None
        return out, offset

    # 1. today's new releases (tag:new), labels shared between the accounts
    todo_new = [lb for lb in labels if _state(conn, f"spotify_label_new:{lb}", {}).get("day") != today()]

    def new_pass(lane, lbs):
        res = {}
        for lb in lbs:
            albums, complete = [], True
            for phrase in labels[lb]:
                got, nxt = pages(lane, f'label:"{phrase}" tag:new')
                albums += got
                complete = complete and nxt is None
            res[lb] = (albums, complete and not lane.stop)
        return res
    for res in parallel(new_pass, todo_new) if todo_new else []:
        for lb, (albums, complete) in res.items():
            remember(lb, albums)
            if complete:
                _set(conn, f"spotify_label_new:{lb}", {"day": today()})
    conn.commit()

    # 2. albums not read yet: their track lists, fetched by both accounts; matching and linking here
    def fetch_tracks(lane, albums):
        out = []
        for a in albums:
            if should_stop():
                break
            res = lane.call(f"/albums/{a['album_id']}/tracks", {"limit": 50})
            if res is None:
                break
            out.append((a, res))
        return out

    def read_pending():
        pending = [dict(r) for r in conn.execute(
            "SELECT album_id, label, name, release_date FROM spotify_label_album WHERE fetched_at IS NULL AND release_date >= ? "
            "ORDER BY release_date DESC", (since,))]
        if not pending or all(l.stop for l in lanes):
            return
        for got in parallel(fetch_tracks, pending):
            for a, res in got:
                _read_album(conn, _isrc_lane(lanes), a, res, by_label.get(a["label"], []), rejected, known_tracks, since, ts, st)
    read_pending()

    # 3. backfill (label:"..." year:y), page by page across runs; new albums found are read straight away
    def backfill(lane, lbs):
        res = {}
        for lb in lbs:
            b, albums = dict(states[lb]), []  # a copy: the threads never touch the database
            while not b["complete"] and not lane.stop and not should_stop():
                got, nxt = pages(lane, f'label:"{labels[lb][b["phrase"]]}" year:{b["year"]}', b["offset"], max_pages=5)
                albums += got
                if nxt is None:
                    b["offset"] = 0
                    if b["year"] < int(until[:4]):
                        b["year"] += 1
                    else:
                        b["year"], b["phrase"] = int(since[:4]), b["phrase"] + 1
                        b["complete"] = b["phrase"] >= len(labels[lb])
                else:
                    b["offset"] = nxt
            res[lb] = (albums, b)
        return res
    states = {lb: _backfill(conn, cfg, lb) for lb in labels}  # read here, before the threads start
    conn.commit()
    todo_back = [lb for lb in labels if not states[lb].get("complete")]
    if todo_back and not all(l.stop for l in lanes):
        for res in parallel(backfill, todo_back):
            for lb, (albums, b) in res.items():
                remember(lb, albums)
                _set(conn, f"spotify_label_backfill:{lb}", b)
        conn.commit()
        read_pending()  # the albums the backfill found

    for lane in lanes:
        if lane.retry_after:
            until_ts = datetime.now(timezone.utc) + timedelta(seconds=lane.retry_after)
            _set_raw(conn, f"spotify_{lane.acct}_blocked_until", until_ts.isoformat())
    conn.commit()
    if should_stop():
        raise songs.Cancelled()
    st["label_sweep_calls"] = sum(l.sp.calls for l in lanes)
    st["label_sweep_calls_by_account"] = {l.acct: l.sp.calls for l in lanes}
    stops = [f"{songs.ACCOUNT_NAME[l.acct]}: {l.stop}" for l in lanes if l.stop]
    log(f"Spotify label sweep: {st['label_sweep_albums']} albums read, {st['label_sweep_tracks']} tracks, "
        f"{st['label_sweep_linked']} linked, {st['label_sweep_review']} to Review, {st['label_sweep_no_song']} with no YouTube song "
        f"({st['label_sweep_calls']} calls: " + ", ".join(f"{k} {v}" for k, v in st["label_sweep_calls_by_account"].items()) + ")"
        + (f"; stopped: {'; '.join(stops)}" if stops else ""))
    return dict(st, status="ok", **({"stop_reason": "; ".join(stops)} if stops else {}))


def _isrc_lane(lanes):
    """A lane that can still make a call, for the ISRC lookups (main thread, after the threads have finished)."""
    return next((l for l in lanes if not l.stop), None)


def _set_raw(conn, key, value):
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _read_album(conn, lane, a, res, label_songs, rejected, known_tracks, since, ts, st):
    """One album's track list (fetched by a lane): every track is stored, scored against the label's songs, the
    confident matches linked (with one call for the ISRC, while a lane can still call), the rest parked for Review."""
    album = {"id": a["album_id"], "name": a["name"], "release_date": a["release_date"]}
    hits = {}  # song id -> [(total, detail, track)]
    n = linked = review = 0
    lb = a.get("label")
    keep = lambda t, song_id=None, how="none", score=None: conn.execute(
        """INSERT INTO spotify_label_track (track_id, label, title, artists, album_id, album, release_date, duration_ms,
                                            song_id, match, score, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(track_id) DO UPDATE SET song_id=COALESCE(excluded.song_id, song_id),
               match=CASE WHEN excluded.match='none' THEN match ELSE excluded.match END, score=COALESCE(excluded.score, score)""",
        (t["id"], lb, t["name"], ", ".join(r["name"] for r in t.get("artists") or []), a["album_id"], a["name"],
         a["release_date"], t.get("duration_ms"), song_id, how, score, ts))
    for t in res.get("items") or []:
        n += 1
        keep(t)
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
                keep(t, sid, "already", total)
                continue  # the song already has its Spotify track; nothing to add from the sweep
            keep(t, sid, decision, total)
            if decision == "auto":
                full = (lane.call(f"/tracks/{t['id']}", {}) if lane else None) or {}  # its ISRC (album tracks leave it out)
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
