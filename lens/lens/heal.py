"""Self-healing page readers (2026-11-10): every value read from a public page has several ways to be found, and a
reader that stops working never marks items as read.

1. Strategies. Each reader tries its strategies in order (the first is the page's usual place; the next ones look
   for the same value another way). Which one found the value is counted per day (`extract_stat`), so the Console
   shows when a page changed and a fallback took over ("healed").
2. Learned hints. When a fallback finds the value at a new marker (e.g. a renamed data-testid), the marker is kept
   (`state` heal_hint:<reader>) once it was seen on several items, and tried first from then on.
3. Guards. "No number" outcomes (Spotify's hidden counts, a page that is gone, an upload without a sound page) are
   only trusted while the reader is provably working: if items known to show a number come back empty, or the share
   of "no number" jumps far above its usual level, the reader is held for the day (`guard`): nothing is marked, the
   watchdog keeps retrying, and an alert opens.
4. Value checks. A cumulative count (plays, Shorts, Reels) that falls sharply or jumps implausibly against the item's
   last reading is kept apart in `reading_suspect`, not stored as a reading.
5. Samples. The first page a reader could not read each day is kept (data/heal_samples/<reader>/<day>.html.gz, 7 days),
   so a changed layout can be fixed from the page itself.
Rule-based only (no AI or ML), standard library only.
"""
import gzip
import json
from datetime import date, datetime, timedelta, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS extract_stat (
    day       TEXT,
    reader    TEXT,      -- spotify_plays, shorts, instagram, spotify_embed, jiosaavn
    strategy  TEXT,      -- the strategy that found the value; 'fail' = none did; 'guard' = outcomes held by a guard
    n         INTEGER,
    PRIMARY KEY (day, reader, strategy)
);
CREATE TABLE IF NOT EXISTS reading_suspect (
    id               INTEGER PRIMARY KEY,
    platform_item_id INTEGER,
    metric           TEXT,
    value            REAL,
    last_value       REAL,
    reason           TEXT,
    captured_at      TEXT
);
CREATE INDEX IF NOT EXISTS reading_suspect_at ON reading_suspect (captured_at);
"""
READERS = {
    "spotify_plays": {"name": "Spotify play counts (track pages)", "primary": "testid"},
    "shorts": {"name": "YouTube Shorts (sound pages)", "primary": "header"},
    "instagram": {"name": "Instagram Reels (audio pages)", "primary": "audio-label"},
    "spotify_embed": {"name": "Spotify track details (embed pages)", "primary": "next-data"},
    "jiosaavn": {"name": "JioSaavn play counts (API)", "primary": "play_count"},
}
HINT_MIN_SEEN = 3          # a learned marker is kept once it found the value on this many items in one run
SAMPLE_DAYS = 7
# Value checks per metric: (largest allowed fall as a share of the last value, largest allowed growth factor for a big count)
LIMITS = {"plays": (0.10, 30), "shorts": (0.50, 50), "reels": (0.50, 50)}


_READY = set()  # database files whose tables exist (executescript commits, so it must not run on every record)


def ensure(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if path not in _READY:
        conn.executescript(SCHEMA)
        _READY.add(path)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def record(conn, reader, strategy, n=1, day=None):
    """Count `n` values found by `strategy` (or 'fail' / 'guard')."""
    ensure(conn)
    conn.execute("""INSERT INTO extract_stat (day, reader, strategy, n) VALUES (?,?,?,?)
                    ON CONFLICT(day, reader, strategy) DO UPDATE SET n = n + excluded.n""", (day or _today(), reader, strategy, n))


import threading

_TALLY, _TALLY_LOCK = {}, threading.Lock()


def tally(reader, strategy, n=1):
    """Count a value found without a database at hand (e.g. the Spotify embed reader); flush() stores the counts."""
    with _TALLY_LOCK:
        _TALLY[(reader, strategy)] = _TALLY.get((reader, strategy), 0) + n


def flush(conn):
    """Store the counts tallied in memory (called by the job runner after every job; never raises)."""
    with _TALLY_LOCK:
        items = list(_TALLY.items())
        _TALLY.clear()
    try:
        for (reader, strategy), n in items:
            record(conn, reader, strategy, n)
        conn.commit()
    except Exception:
        with _TALLY_LOCK:  # keep them for the next flush
            for k, n in items:
                _TALLY[k] = _TALLY.get(k, 0) + n


def hint(conn, reader):
    """The learned marker for a reader, or None."""
    r = conn.execute("SELECT value FROM state WHERE key=?", (f"heal_hint:{reader}",)).fetchone()
    try:
        return json.loads(r[0]).get("value") if r else None
    except (TypeError, ValueError):
        return None


def learn(conn, reader, seen):
    """seen: {marker: items it found the value on, this run}. The most used marker seen on enough items is kept."""
    if not seen:
        return None
    marker, n = max(seen.items(), key=lambda kv: kv[1])
    if not marker or n < HINT_MIN_SEEN:
        return None
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                 (f"heal_hint:{reader}", json.dumps({"value": marker, "seen": n, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})))
    return marker


def sample(reader, page, day=None):
    """Keep the first unreadable page of the day for this reader (gzipped), and drop samples older than a week."""
    try:
        folder = config.DB_FILE.parent / "heal_samples" / reader
        folder.mkdir(parents=True, exist_ok=True)
        day = day or _today()
        f = folder / f"{day}.html.gz"
        if not f.exists() and page:
            f.write_bytes(gzip.compress(page.encode("utf-8", "replace") if isinstance(page, str) else page))
        cut = (date.fromisoformat(day) - timedelta(days=SAMPLE_DAYS)).isoformat()
        for old in folder.glob("*.html.gz"):
            if old.name[:10] < cut:
                old.unlink()
        return str(f)
    except OSError:
        return None


# ---------- guards ----------

def baseline(conn, reader, key):
    """The usual share of an outcome (e.g. 'nosound') for a reader: a slow moving average, or None before the first run."""
    r = conn.execute("SELECT value FROM state WHERE key=?", (f"heal_base:{reader}:{key}",)).fetchone()
    try:
        return float(r[0]) if r else None
    except (TypeError, ValueError):
        return None


def mass_guard(conn, reader, key, outcomes, total, min_total=20):
    """True when `outcomes` of `total` is far above the usual share: the page probably changed, so those outcomes
    must not be trusted. The usual share is updated only from runs that pass (so a broken day cannot become normal)."""
    if total < min_total:
        return False
    share = outcomes / total
    base = baseline(conn, reader, key)
    limit = max(0.30, 3 * base + 0.10) if base is not None else 0.60
    if share > limit:
        record(conn, reader, "guard", outcomes)
        return True
    new = share if base is None else round(0.8 * base + 0.2 * share, 4)
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"heal_base:{reader}:{key}", str(new)))
    return False


def known_guard(conn, reader, known_total, known_empty, min_known=5, max_share=0.5):
    """True when items known to show a number (they did on their last reading) mostly came back empty."""
    if known_total >= min_known and known_empty / known_total >= max_share:
        record(conn, reader, "guard", known_empty)
        return True
    return False


# ---------- value checks ----------

def check_value(last, value, metric):
    """'ok', or why a new cumulative value does not fit the last one ('fell', 'jumped')."""
    if value is None or last is None or last <= 0:
        return "ok"
    fall, grow = LIMITS.get(metric, (0.10, 30))
    if value < last * (1 - fall):
        return "fell"
    if value > last * grow and value - last > 1_000_000:
        return "jumped"
    return "ok"


def confirmed(conn, item_id, metric, value, last, reader, times=3):
    """True when the same 'suspicious' value has now been read `times` times (this read included) against the same last
    stored reading: a value that every fresh read gives is the page's real number (e.g. Shorts deleted, a sound
    reassigned, a count corrected by the platform), so it is stored instead of being re-read forever (2026-11-12: 13
    Shorts sounds held since the early morning made the Shorts job run 83 times in a day). A one-off misread does not
    repeat exactly. No clock involved: the anchor is the last stored value."""
    ensure(conn)
    n = conn.execute("""SELECT count(*) FROM reading_suspect WHERE platform_item_id=? AND metric=? AND value=? AND last_value IS ?""",
                     (item_id, metric, value, last)).fetchone()[0]
    if n + 1 >= times:
        record(conn, reader, "confirmed")
        return True
    return False


def suspect(conn, item_id, metric, value, last, reason, at=None):
    ensure(conn)
    conn.execute("INSERT INTO reading_suspect (platform_item_id, metric, value, last_value, reason, captured_at) VALUES (?,?,?,?,?,?)",
                 (item_id, metric, value, last, reason, at or datetime.now(timezone.utc).isoformat(timespec="seconds")))


def last_snapshot(conn, item_id, metric, before_day):
    r = conn.execute("""SELECT value FROM snapshot WHERE platform_item_id=? AND metric=? AND capture_date < ?
                        ORDER BY capture_date DESC LIMIT 1""", (item_id, metric, before_day)).fetchone()
    return r[0] if r else None


def last_sound(conn, item_id, before_day):
    r = conn.execute("SELECT shorts FROM sound_use WHERE platform_item_id=? AND day < ? ORDER BY day DESC LIMIT 1",
                     (item_id, before_day)).fetchone()
    return r[0] if r else None


# ---------- the Console's view ----------

def summary(conn, days=7):
    """Per reader: today's and the week's counts by strategy, its state, learned marker, newest sample, suspects."""
    ensure(conn)
    today = _today()
    since = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute("SELECT day, reader, strategy, n FROM extract_stat WHERE day >= ?", (since,)).fetchall()
    sus = {r[0]: r[1] for r in conn.execute("""SELECT p.platform, count(*) FROM reading_suspect s JOIN platform_item p ON p.id=s.platform_item_id
                                                WHERE s.captured_at >= ? GROUP BY 1""", (today,)).fetchall()}
    out = []
    for key, info in READERS.items():
        mine = [r for r in rows if r[1] == key]
        def tally(rs):
            t = {"primary": 0, "fallback": 0, "fail": 0, "guard": 0, "by": {}}
            for _, _, s, n in rs:
                t["by"][s] = t["by"].get(s, 0) + n
                if s == "confirmed":  # suspicious values stored after repeating (not a way of reading the page)
                    continue
                t["fail" if s == "fail" else "guard" if s == "guard" else "primary" if s == info["primary"] else "fallback"] += n
            return t
        td, wk = tally([r for r in mine if r[0] == today]), tally(mine)
        read = td["primary"] + td["fallback"]
        # held now: Spotify plays keeps a "held" flag that clears once the day's evidence passes (2026-11-12: the reader
        # recovered but stayed "broken" all day because of the morning's holds); the others: holds outnumber reads today
        held_now = (bool(conn.execute("SELECT 1 FROM state WHERE key=?", (f"heal_held:{key}:{today}",)).fetchone())
                    if key == "spotify_plays" else td["guard"] > 0 and td["guard"] >= read)
        state = ("idle" if not (read or td["fail"] or td["guard"]) else
                 "broken" if held_now or (td["fail"] >= 10 and td["fail"] > read) else
                 "healed" if td["fallback"] > td["primary"] else "ok")
        folder = config.DB_FILE.parent / "heal_samples" / key
        samples = sorted(p.name[:10] for p in folder.glob("*.html.gz")) if folder.exists() else []
        platform = {"spotify_plays": "spotify", "spotify_embed": "spotify", "shorts": "youtube"}.get(key, key)
        out.append({"reader": key, "name": info["name"], "primary": info["primary"], "state": state, "today": td, "week": wk,
                    "hint": hint(conn, key), "last_sample": samples[-1] if samples else None,
                    "suspects_today": sus.get(platform, 0) if key in ("spotify_plays", "shorts", "instagram", "jiosaavn") else 0})
    recent = conn.execute("""SELECT s.captured_at, p.platform, p.title, s.metric, s.value, s.last_value, s.reason FROM reading_suspect s
                             JOIN platform_item p ON p.id=s.platform_item_id ORDER BY s.id DESC LIMIT 50""").fetchall()
    return {"readers": out, "today": today,
            "suspects": [dict(zip(("at", "platform", "title", "metric", "value", "last", "reason"), r)) for r in recent]}


def alert_conditions(conn):
    """Conditions for lens.alerts: a reader on a fallback (the page changed, readings continue), a reader held or failing
    (readings stop until fixed), many suspicious values."""
    out = []
    try:
        s = summary(conn, days=1)
    except Exception:  # never let the health view break the alerts
        return out
    for r in s["readers"]:
        td = r["today"]
        if r["state"] == "broken":
            out.append({"key": f"heal:{r['reader']}", "severity": "critical", "action": "open:missed",
                        "title": f"{r['name']}: the page seems to have changed; readings are held",
                        "detail": f"Today {td['fail']} items could not be read and {td['guard']} outcomes were held by the guard "
                                  f"(nothing was marked as read; the jobs keep retrying). A sample of the page is kept"
                                  f"{' (' + r['last_sample'] + ')' if r['last_sample'] else ''} under data/heal_samples/{r['reader']}/."})
        elif r["state"] == "healed":
            fb = ", ".join(f"{k} {v}" for k, v in td["by"].items() if k not in (r["primary"], "fail", "guard"))
            out.append({"key": f"heal:{r['reader']}", "severity": "warning", "action": "open:missed",
                        "title": f"{r['name']}: the page changed; a fallback is reading it",
                        "detail": f"Readings continue (today: {fb}; usual way {td['by'].get(r['primary'], 0)}). "
                                  f"Update the reader's usual way when convenient."})
        if r["suspects_today"] >= 20:
            out.append({"key": f"suspect:{r['reader']}", "severity": "warning", "action": "open:missed",
                        "title": f"{r['name']}: {r['suspects_today']} values held as suspicious today",
                        "detail": "They fell sharply or jumped against the last reading and were not stored. See Missed readings → Page readers."})
    return out
