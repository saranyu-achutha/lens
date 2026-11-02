"""song_daily: one row per song per day with that day's totals on every platform (kept indefinitely).

Built from the per-upload readings (snapshot) and the Spotify chart, summed over a song's linked items:
  yt_views / yt_likes / yt_comments   YouTube uploads read that day (yt_uploads_read says how many)
  ytm_plays                           YouTube Music audio tracks
  sp_plays                            Spotify play count (public track page); the highest of the song's copies,
                                      because Spotify shows one recording's count on all its copies (single, album, compilations)
  js_plays                            JioSaavn play counts
  dz_rank                             Deezer rank (highest of the song's ISRC matches)
  chart_rank                          best position on the Spotify India daily chart
  yt_shorts                           YouTube Shorts made with the song's official audio (label-owned sounds, shorts_use)
  ig_reels                            Instagram Reels made with the song's official audio (linked audio pages, instagram_audio)
  *_gain                              growth since the previous calendar day, over items read on both days
                                      (a new upload's first reading is not growth; a missed day leaves it empty)
Weekly, monthly and yearly views (song_weekly, song_monthly, song_yearly): each song's totals on the period's last day
read, and every *_gain summed over the period.
Rebuilt at the end of every run for the days that have readings (so merges and review decisions are reflected),
and each run writes the latest day's rows to out/market_lens_song_daily_<day>.csv.
"""
import csv
import re
from datetime import date, timedelta

from . import config, db, shorts_use, spotify_charts

SCHEMA = """
CREATE TABLE IF NOT EXISTS song_daily (
    song_id          INTEGER NOT NULL,
    day              TEXT NOT NULL,
    yt_views         INTEGER,
    yt_likes         INTEGER,
    yt_comments      INTEGER,
    yt_uploads_read  INTEGER,
    ytm_plays        INTEGER,
    sp_plays         INTEGER,
    js_plays         INTEGER,
    dz_rank          INTEGER,
    chart_rank       INTEGER,
    yt_views_gain    INTEGER,   -- growth since the previous day, over uploads read on both days
    yt_likes_gain    INTEGER,
    yt_comments_gain INTEGER,
    ytm_plays_gain   INTEGER,
    sp_plays_gain    INTEGER,
    js_plays_gain    INTEGER,
    yt_shorts        INTEGER,   -- Shorts made with the song's official (label-owned) sounds
    yt_shorts_gain   INTEGER,
    ig_reels         INTEGER,   -- Instagram Reels made with the song's official audio
    ig_reels_gain    INTEGER,
    built_at         TEXT,
    PRIMARY KEY (song_id, day)
);
CREATE INDEX IF NOT EXISTS song_daily_day ON song_daily(day);
"""
COLS = ("song_id", "day", "yt_views", "yt_likes", "yt_comments", "yt_uploads_read", "ytm_plays", "sp_plays", "js_plays", "dz_rank",
        "chart_rank", "yt_views_gain", "yt_likes_gain", "yt_comments_gain", "ytm_plays_gain", "sp_plays_gain", "js_plays_gain", "yt_shorts", "yt_shorts_gain", "ig_reels", "ig_reels_gain")
GAINS = (("yt_views", "youtube", "views"), ("yt_likes", "youtube", "likes"), ("yt_comments", "youtube", "comments"),
         ("ytm_plays", "ytmusic", "plays"), ("sp_plays", "spotify", "plays"), ("js_plays", "jiosaavn", "plays"))


def ensure(conn):
    """Create the table, and add columns introduced later (e.g. JioSaavn) to an existing one."""
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(song_daily)")}
    for col in COLS + ("built_at",):
        if col not in have:
            conn.execute(f"ALTER TABLE song_daily ADD COLUMN {col} {'TEXT' if col in ('day', 'built_at') else 'INTEGER'}")
    shorts_use.ensure(conn)  # Shorts / Reels tables (and columns added later) before anything reads them
    periods(conn)
    conn.commit()


PERIODS = {"weekly": "strftime('%Y-W%W', day)", "monthly": "substr(day,1,7)", "yearly": "substr(day,1,4)"}
TOTALS = ("yt_views", "yt_likes", "yt_comments", "ytm_plays", "sp_plays", "js_plays", "dz_rank", "yt_shorts", "ig_reels")


def periods(conn):
    """song_weekly / song_monthly / song_yearly: totals on the period's last day read and the gains summed (views, so
    they always agree with song_daily and need no rebuilding)."""
    gains = [c for c in COLS if c.endswith("_gain")]
    for name, expr in PERIODS.items():
        have = conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (f"song_{name}",)).fetchone()
        if have and all(c in have[0] for c in TOTALS + tuple(gains)):  # up to date: ensure() runs on every page load
            continue
        conn.execute(f"DROP VIEW IF EXISTS song_{name}")
        conn.execute(f"""CREATE VIEW song_{name} AS
            SELECT g.song_id, g.period, g.first_day, g.last_day, g.days, {", ".join("l." + c for c in TOTALS)}, {", ".join("g." + c for c in gains)}
            FROM (SELECT song_id, {expr} period, min(day) first_day, max(day) last_day, count(*) days,
                         {", ".join(f"sum({c}) {c}" for c in gains)}
                  FROM song_daily GROUP BY 1, 2) g
            JOIN song_daily l ON l.song_id=g.song_id AND l.day=g.last_day""")


def build(conn, days=None, log=print):
    """(Re)build song_daily for `days` (default: every day with readings). Returns rows written."""
    ensure(conn)
    spotify_charts.ensure(conn)
    shorts_use.ensure(conn)
    if days is None:
        days = [r[0] for r in conn.execute("SELECT DISTINCT capture_date FROM snapshot ORDER BY 1")]
    ts = db.now_iso()
    n = 0
    for day in days:
        rows = conn.execute(
            """SELECT p.track_id song_id, ? day,
                      sum(CASE WHEN p.platform='youtube' AND s.metric='views' THEN s.value END) yt_views,
                      sum(CASE WHEN p.platform='youtube' AND s.metric='likes' THEN s.value END) yt_likes,
                      sum(CASE WHEN p.platform='youtube' AND s.metric='comments' THEN s.value END) yt_comments,
                      count(DISTINCT CASE WHEN p.platform='youtube' AND s.metric='views' THEN p.id END) yt_uploads_read,
                      sum(CASE WHEN p.platform='ytmusic' AND s.metric='plays' THEN s.value END) ytm_plays,
                      max(CASE WHEN p.platform='spotify' AND s.metric='plays' THEN s.value END) sp_plays,  -- copies of one recording share a count
                      sum(CASE WHEN p.platform='jiosaavn' AND s.metric='plays' THEN s.value END) js_plays,
                      max(CASE WHEN p.platform='deezer' AND s.metric='rank' THEN s.value END) dz_rank
               FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
               WHERE s.capture_date=? AND p.track_id IS NOT NULL
                 AND ((p.platform='youtube' AND p.review_state IN ('auto','approved','song'))
                      OR (p.platform='spotify' AND p.review_state IN ('auto','approved')) OR p.platform IN ('ytmusic','deezer','jiosaavn'))
               GROUP BY p.track_id""", (day, day)).fetchall()
        chart = dict(conn.execute(f"""SELECT track_id, min(rank) FROM ({spotify_charts.LINK_SQL})
                                      WHERE chart='regional-in-daily' AND chart_date=? GROUP BY track_id""", (day,)).fetchall())
        # Real daily growth: each item's reading today minus its reading on the previous calendar day (both must exist).
        gains = {}
        for tid, plat, metric, g in conn.execute(
                """SELECT p.track_id, p.platform, s.metric,
                          CASE WHEN p.platform='spotify' THEN max(s.value) - max(y.value) ELSE sum(s.value - y.value) END
                   FROM snapshot s
                   JOIN snapshot y ON y.platform_item_id=s.platform_item_id AND y.metric=s.metric AND y.capture_date=date(?, '-1 day')
                   JOIN platform_item p ON p.id=s.platform_item_id
                   WHERE s.capture_date=? AND p.track_id IS NOT NULL AND p.platform IN ('youtube','ytmusic','spotify','jiosaavn')
                     AND (p.platform != 'youtube' OR p.review_state IN ('auto','approved','song'))
                     AND (p.platform != 'spotify' OR p.review_state IN ('auto','approved'))
                   GROUP BY 1, 2, 3""", (day, day)):
            gains[(tid, plat, metric)] = g
        # Shorts / Reels made with the song's official sounds; growth over sounds read on both days.
        use = {}
        for plat in ("youtube", "instagram"):
            use[plat] = dict(conn.execute("""SELECT p.track_id, sum(u.shorts) FROM sound_use u JOIN platform_item p ON p.id=u.platform_item_id
                                             WHERE u.day=? AND u.platform=? AND u.official=1 AND p.track_id IS NOT NULL
                                               AND p.review_state IN ('auto','approved','song') GROUP BY 1""", (day, plat)).fetchall())
            use[plat + "_gain"] = dict(conn.execute("""SELECT p.track_id, sum(u.shorts - y.shorts) FROM sound_use u
                                                  JOIN sound_use y ON y.platform_item_id=u.platform_item_id AND y.day=date(?, '-1 day') AND y.official=1
                                                  JOIN platform_item p ON p.id=u.platform_item_id
                                                  WHERE u.day=? AND u.platform=? AND u.official=1 AND p.track_id IS NOT NULL
                                                    AND p.review_state IN ('auto','approved','song') GROUP BY 1""", (day, day, plat)).fetchall())
        conn.execute("DELETE FROM song_daily WHERE day=?", (day,))
        conn.executemany(f"INSERT INTO song_daily ({', '.join(COLS)}, built_at) VALUES ({', '.join('?' * len(COLS))}, ?)",
                         [tuple(r) + (chart.get(r[0]),) + tuple(gains.get((r[0], plat, m)) for _, plat, m in GAINS)
                          + tuple(use[k].get(r[0]) for k in ("youtube", "youtube_gain", "instagram", "instagram_gain")) + (ts,) for r in rows])
        n += len(rows)
    conn.commit()
    log(f"song_daily: {n:,} song-day rows for {len(days)} day(s)")
    return n


def write_day_csv(conn, day, run_id=None):
    out = config.OUT_DIR / f"market_lens_song_daily_{day}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("song_id", "song", "label_channel", "first_upload") + COLS[1:])
        w.writerows(conn.execute(f"""SELECT d.song_id, t.title,
                                        (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1),
                                        substr(t.first_published,1,10), {', '.join('d.' + c for c in COLS[1:])}
                                     FROM song_daily d JOIN track t ON t.id=d.song_id WHERE d.day=? ORDER BY d.yt_views DESC""", (day,)))
    return out


OUT_NAME = re.compile(r"^(?P<kind>.+?)_(?P<day>\d{4}-\d{2}-\d{2})(?:_run(?P<run>\d+))?\.csv$")


def prune_outputs(keep_days, log=print):
    """Keep the newest CSV of each kind per day for `keep_days` days; delete older and repeated ones. Returns files removed."""
    if not keep_days or not config.OUT_DIR.exists():
        return 0
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    groups = {}
    for f in config.OUT_DIR.glob("*.csv"):
        m = OUT_NAME.match(f.name)
        if m:
            groups.setdefault((m["kind"], m["day"]), []).append((int(m["run"] or 0), f.stat().st_mtime, f))
    removed = 0
    for (kind, day), files in groups.items():
        files.sort(reverse=True)
        for i, (_, _, f) in enumerate(files):
            if day < cutoff or i > 0:
                f.unlink(missing_ok=True)
                removed += 1
    if removed:
        log(f"outputs: {removed} older CSV files removed (keeping {keep_days} days, newest per day)")
    return removed


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    """Pipeline step: rebuild the last 3 days (jobs run often), every day once a day (so merges and review decisions
    reach the whole history), and save today's CSV."""
    today = date.today().isoformat()
    done = conn.execute("SELECT value FROM state WHERE key='song_daily_full'").fetchone()
    if done and done[0] == today:
        days = [r[0] for r in conn.execute("SELECT DISTINCT capture_date FROM snapshot WHERE capture_date >= date(?, '-2 day') ORDER BY 1", (today,))]
        n = build(conn, days, log=log)
    else:
        n = build(conn, log=log)
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('song_daily_full', ?)", (today,))
        conn.commit()
    last = conn.execute("SELECT max(day) FROM song_daily").fetchone()[0]
    stats = {"status": "ok", "song_daily_rows": n, "outputs_removed": prune_outputs(cfg.get("outputs", {}).get("keep_days", 0), log)}
    if last:
        out = write_day_csv(conn, last, run_id)
        log(f"wrote {last} song totals -> {out.name}")
        stats["csv"] = out.name
    return stats


if __name__ == "__main__":
    build(db.connect(str(config.DB_FILE)))
