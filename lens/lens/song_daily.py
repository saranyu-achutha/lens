"""song_daily: one row per song per day with that day's totals on every platform (kept indefinitely).

Built from the per-upload readings (snapshot) and the Spotify chart, summed over a song's linked items:
  yt_views / yt_likes / yt_comments   YouTube uploads read that day (yt_uploads_read says how many)
  ytm_plays                           YouTube Music audio tracks
  sp_plays                            Spotify play counts (public track pages)
  dz_rank                             Deezer rank (highest of the song's ISRC matches)
  chart_rank                          best position on the Spotify India daily chart
  *_gain                              growth since the previous calendar day, over items read on both days
                                      (a new upload's first reading is not growth; a missed day leaves it empty)
Rebuilt at the end of every run for the days that have readings (so merges and review decisions are reflected),
and each run writes the latest day's rows to out/market_lens_song_daily_<day>.csv.
"""
import csv

from . import config, db, spotify_charts

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
    dz_rank          INTEGER,
    chart_rank       INTEGER,
    yt_views_gain    INTEGER,   -- growth since the previous day, over uploads read on both days
    yt_likes_gain    INTEGER,
    yt_comments_gain INTEGER,
    ytm_plays_gain   INTEGER,
    sp_plays_gain    INTEGER,
    built_at         TEXT,
    PRIMARY KEY (song_id, day)
);
CREATE INDEX IF NOT EXISTS song_daily_day ON song_daily(day);
"""
COLS = ("song_id", "day", "yt_views", "yt_likes", "yt_comments", "yt_uploads_read", "ytm_plays", "sp_plays", "dz_rank", "chart_rank",
        "yt_views_gain", "yt_likes_gain", "yt_comments_gain", "ytm_plays_gain", "sp_plays_gain")
GAINS = (("yt_views", "youtube", "views"), ("yt_likes", "youtube", "likes"), ("yt_comments", "youtube", "comments"),
         ("ytm_plays", "ytmusic", "plays"), ("sp_plays", "spotify", "plays"))


def build(conn, days=None, log=print):
    """(Re)build song_daily for `days` (default: every day with readings). Returns rows written."""
    conn.executescript(SCHEMA)
    spotify_charts.ensure(conn)
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
                      sum(CASE WHEN p.platform='spotify' AND s.metric='plays' THEN s.value END) sp_plays,
                      max(CASE WHEN p.platform='deezer' AND s.metric='rank' THEN s.value END) dz_rank
               FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
               WHERE s.capture_date=? AND p.track_id IS NOT NULL
                 AND ((p.platform='youtube' AND p.review_state IN ('auto','approved','song'))
                      OR (p.platform='spotify' AND p.review_state IN ('auto','approved')) OR p.platform IN ('ytmusic','deezer'))
               GROUP BY p.track_id""", (day, day)).fetchall()
        chart = dict(conn.execute(f"""SELECT track_id, min(rank) FROM ({spotify_charts.LINK_SQL})
                                      WHERE chart='regional-in-daily' AND chart_date=? GROUP BY track_id""", (day,)).fetchall())
        # Real daily growth: each item's reading today minus its reading on the previous calendar day (both must exist).
        gains = {}
        for tid, plat, metric, g in conn.execute(
                """SELECT p.track_id, p.platform, s.metric, sum(s.value - y.value) FROM snapshot s
                   JOIN snapshot y ON y.platform_item_id=s.platform_item_id AND y.metric=s.metric AND y.capture_date=date(?, '-1 day')
                   JOIN platform_item p ON p.id=s.platform_item_id
                   WHERE s.capture_date=? AND p.track_id IS NOT NULL AND p.platform IN ('youtube','ytmusic','spotify')
                     AND (p.platform != 'youtube' OR p.review_state IN ('auto','approved','song'))
                     AND (p.platform != 'spotify' OR p.review_state IN ('auto','approved'))
                   GROUP BY 1, 2, 3""", (day, day)):
            gains[(tid, plat, metric)] = g
        conn.execute("DELETE FROM song_daily WHERE day=?", (day,))
        conn.executemany(f"INSERT INTO song_daily ({', '.join(COLS)}, built_at) VALUES ({', '.join('?' * len(COLS))}, ?)",
                         [tuple(r) + (chart.get(r[0]),) + tuple(gains.get((r[0], plat, m)) for _, plat, m in GAINS) + (ts,) for r in rows])
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


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    """Pipeline step: rebuild every day (cheap: one grouped query per day) and save today's CSV."""
    n = build(conn, log=log)
    last = conn.execute("SELECT max(day) FROM song_daily").fetchone()[0]
    stats = {"status": "ok", "song_daily_rows": n}
    if last:
        out = write_day_csv(conn, last, run_id)
        log(f"wrote {last} song totals -> {out.name}")
        stats["csv"] = out.name
    return stats


if __name__ == "__main__":
    build(db.connect(str(config.DB_FILE)))
