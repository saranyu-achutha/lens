"""Exports (Developer: Outputs page): the datasets read straight from the database, a page at a time, and turned into
CSV only when someone downloads one (the CSV is built in memory, sent and not kept). Runs no longer write CSV files
(user rule, 2026-11-03): the database is the one store; it is compact (SQLite pages, indexed) and fast to read.
CSVs written before that day are kept gzipped in out/archive/ and can still be downloaded (unpacked on the fly).
"""
import csv
import gzip
import io
import re
from datetime import date

from . import config

ARCHIVE_RE = re.compile(r"[\w.-]+\.csv\.gz")
# CSV downloads carry no internal or platform IDs (user rule, 2026-11-04): songs are named by title, with all their
# other details. Left out: any column ending in _id, _ids or url (song_id, item_id, platform_id, video_id,
# spotify_track_id, all_youtube_video_ids, youtube_url, url, ...). Industry codes (ISRC, UPC) stay.
ID_COLUMN = re.compile(r"(^|_)(ids?|url)$", re.I)


class IdFreeWriter:
    """csv.writer that drops ID and link columns: the first row written is the header and decides which go."""
    def __init__(self, f):
        self._w, self._keep = csv.writer(f), None

    def writerow(self, row):
        row = list(row)
        if self._keep is None:
            self._keep = [i for i, h in enumerate(row) if not ID_COLUMN.search(str(h).strip())]
        self._w.writerow([row[i] for i in self._keep if i < len(row)])

    def writerows(self, rows):
        for r in rows:
            self.writerow(r)


def slug(text):
    """A file-name-safe version of a song title."""
    return re.sub(r"[^\w]+", "_", text or "").strip("_")[:60] or "song"


def _songs(conn, day):
    from . import songs
    rows = songs.export_rows(conn, *config.window(config.load()))
    return (list(rows[0].keys()) if rows else []), (tuple(r.values()) for r in rows)


def _sql(sql, params=lambda day: ()):
    def fn(conn, day):
        cur = conn.execute(sql, params(day))
        return [d[0] for d in cur.description], (tuple(r) for r in cur)
    return fn


def _label_tracks(conn, day):
    from .spotify_labels import ensure
    ensure(conn)
    cur = conn.execute("""SELECT x.track_id AS spotify_song_id, x.title, x.artists, x.label, x.album, x.release_date,
                                 round(x.duration_ms / 1000.0) AS duration_s,
                                 coalesce(t.yt_title, t.title) AS youtube_song,
                                 CASE WHEN p.review_state IN ('auto','approved') THEN 'linked'
                                      WHEN p.review_state = 'pending' THEN 'in review'
                                      WHEN x.match = 'already' THEN 'song has another Spotify track'
                                      ELSE 'no YouTube song yet' END AS status
                          FROM spotify_label_track x
                          LEFT JOIN platform_item p ON p.platform='spotify' AND p.platform_id=x.track_id
                          LEFT JOIN track t ON t.id = coalesce(p.track_id, x.song_id)
                          ORDER BY x.label, x.release_date DESC, x.title""")
    return [d[0] for d in cur.description], (tuple(r) for r in cur)


def _archived(conn, day):
    from .songs import ARCHIVE_SCHEMA
    conn.executescript(ARCHIVE_SCHEMA)
    cur = conn.execute("""SELECT title AS song, first_upload, label, channel, isrc, youtube_videos, spotify_tracks, last_day,
                                 last_yt_views, reason, archived_at FROM song_archive ORDER BY first_upload DESC, title""")
    return [d[0] for d in cur.description], (tuple(r) for r in cur)


def _browser_found(conn, day):
    from .spotify_found import ensure
    ensure(conn)
    cur = conn.execute("""SELECT yt_title AS song, yt_first AS first_upload, label, channel, sp_title AS spotify_song,
                                 sp_artists AS spotify_artists, sp_release AS spotify_release, title_match, outcome, score, checked_at
                          FROM spotify_found ORDER BY outcome, yt_first DESC, yt_title""")
    return [d[0] for d in cur.description], (tuple(r) for r in cur)


def _new_links(conn, day):
    """Songs of the period that got their Spotify track in the last 7 days (linked, or approved in Review)."""
    labels = config.channel_labels(config.load())
    cur = conn.execute("""
        SELECT substr(p.first_seen_at,1,10) AS linked_on, coalesce(t.yt_title, t.title) AS song, substr(t.first_published,1,10) AS first_upload,
               (SELECT channel FROM platform_item y WHERE y.track_id=t.id AND y.platform='youtube' ORDER BY published_at LIMIT 1) AS channel,
               p.title AS spotify_title,
               (SELECT group_concat(a.name, ', ') FROM track_artist ta JOIN artist a ON a.id=ta.artist_id WHERE ta.track_id=t.id) AS artists,
               substr(coalesce(r.release_date, p.published_at),1,10) AS spotify_release,
               CASE WHEN substr(coalesce(r.release_date, p.published_at),1,10) < date(substr(t.first_published,1,10), '-30 day')
                    THEN 'older release' ELSE 'new release' END AS release_type,
               CASE WHEN t.old_release IS NOT NULL THEN 'stored only (released before the period)' ELSE 'crawled daily' END AS crawl,
               CASE WHEN p.match_detail LIKE '%"via": "browser_search%' THEN 'browser search (no API)'
                    WHEN p.match_detail LIKE '%"via": "label_sweep"%' THEN 'label sweep'
                    WHEN p.match_detail LIKE '%"via": "artist_albums"%' THEN 'artist-first'
                    ELSE 'API search' END AS found_by,
               CASE WHEN p.review_state='approved' THEN 'approved in Review' ELSE 'automatic' END AS decision,
               round(p.match_score, 2) AS score, t.isrc,
               (SELECT value FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='plays' ORDER BY capture_date DESC LIMIT 1) AS spotify_plays,
               (SELECT max(capture_date) FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='plays') AS plays_day
        FROM platform_item p JOIN track t ON t.id=p.track_id LEFT JOIN release r ON r.id=t.release_id
        WHERE p.platform='spotify' AND p.review_state IN ('auto','approved') AND t.source='youtube'
          AND t.first_published >= ? AND (p.first_seen_at >= date('now', '-7 day')
               OR EXISTS (SELECT 1 FROM review_decision d WHERE d.item_id=p.id AND d.action='approve' AND d.undone_at IS NULL
                          AND d.decided_at >= date('now', '-7 day')))
        ORDER BY p.first_seen_at DESC, t.first_published DESC""", (config.window(config.load())[0],))
    cols = [d[0] for d in cur.description]
    i = cols.index("channel")
    out = cols[:i + 1] + ["label"] + cols[i + 1:]
    return out, (tuple(r)[:i + 1] + (labels.get(r[i], ""),) + tuple(r)[i + 1:] for r in cur)


def _window(day):
    return config.window(config.load())


def _song_daily_sql():
    from .song_daily import COLS
    return f"""SELECT d.song_id, t.title AS song,
                      (SELECT channel FROM platform_item p WHERE p.track_id=t.id AND p.platform='youtube' ORDER BY published_at LIMIT 1) AS label_channel,
                      substr(t.first_published,1,10) AS first_upload, {', '.join('d.' + c for c in COLS[1:])}
               FROM song_daily d JOIN track t ON t.id=d.song_id WHERE d.day=? ORDER BY d.yt_views DESC"""


def _datasets():
    from .discover_spotify import EXPORT_SQL
    from .sync_youtube import YT_EXPORT_SQL
    return {
        "song_daily": {"name": "Song daily totals", "what": "one row per song for the chosen day: every platform's numbers and the day's gains",
                       "by_day": True, "rows": _sql(_song_daily_sql(), lambda day: (day,))},
        "songs": {"name": "Songs", "what": "every song in the window with its uploads, links, latest numbers and metadata", "rows": _songs},
        "song_metadata": {"name": "Song metadata", "what": "one row per song, field and value (credits, language, category)",
                          "rows": _sql("""SELECT m.track_id AS song_id, t.title AS song, m.field, m.value, m.value_key, m.source FROM song_meta m
                                          JOIN track t ON t.id=m.track_id WHERE t.first_published >= ? AND t.old_release IS NULL AND substr(t.first_published,1,10) <= ?
                                          ORDER BY m.track_id, m.field, m.position""", _window)},
        "youtube_videos": {"name": "YouTube videos", "what": "every stored upload with its match and latest views, likes and comments", "rows": _sql(YT_EXPORT_SQL)},
        "spotify_label_crawl": {"name": "Spotify label crawl", "what": "tracks from the manual Spotify label crawl", "rows": _sql(EXPORT_SQL)},
        "spotify_label_songs": {"name": "Spotify label songs", "keep_ids": True,
                                "what": "every Spotify track of the shortlisted labels released from the period start, with its Spotify ID "
                                        "and the YouTube song it matched (filled by the label sweep, growing daily)", "rows": _label_tracks},
        "archived_songs": {"name": "Archived songs", "what": "songs first uploaded before the period start: kept for reference, not crawled",
                           "rows": _archived},
        "spotify_new_links": {"name": "New Spotify links", "what": "songs of the period that got their Spotify track in the last 7 days: "
                              "how it was found, the decision, new or older release, latest Spotify plays", "rows": _new_links},
        "spotify_browser_found": {"name": "Spotify browser search", "what": "Spotify tracks found by the logged-out browser search (laptop list and the "
                                  "daily spotify_browser_search job) for songs the API had not found: new releases are checked and linked (or sent to Review); older releases are "
                                  "kept here for reference only and cost no crawl", "rows": _browser_found},
    }


def _days(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT day FROM song_daily ORDER BY day DESC")]


def listing(conn):
    days = _days(conn)
    sets = [{"key": k, "name": d["name"], "what": d["what"], "by_day": d.get("by_day", False)} for k, d in _datasets().items()]
    folder = config.OUT_DIR / "archive"
    archived = sorted(({"name": f.name, "size": f.stat().st_size} for f in folder.glob("*.csv.gz")), key=lambda f: f["name"], reverse=True) \
        if folder.exists() else []
    return {"datasets": sets, "days": days, "archived": archived}


def _open(conn, key, day):
    ds = _datasets().get(key)
    if not ds:
        raise ValueError("unknown dataset")
    if ds.get("by_day"):
        day = day or (_days(conn) or [date.today().isoformat()])[0]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("bad day")
    header, rows = ds["rows"](conn, day)
    return ds, day, header, rows


def _match(q):
    needle = (q or "").lower()
    return (lambda r: any(needle in str(c).lower() for c in r if c is not None)) if needle else None


def view(conn, qs):
    """One page of a dataset (optionally filtered by text in any column)."""
    ds, day, header, rows = _open(conn, qs.get("ds"), qs.get("day"))
    offset, limit = max(int(qs.get("offset", 0)), 0), min(max(int(qs.get("limit", 100)), 1), 500)
    keep, total, page = _match(qs.get("q")), 0, []
    for r in rows:
        if keep and not keep(r):
            continue
        if offset <= total < offset + limit:
            page.append(r)
        total += 1
    return {"ds": qs.get("ds"), "name": ds["name"], "day": day, "header": header, "rows": page, "total": total, "offset": offset, "limit": limit}


def download(conn, qs):
    """The dataset (filtered like the view) as CSV bytes and a file name. Built in memory; nothing is written to disk."""
    ds, day, header, rows = _open(conn, qs.get("ds"), qs.get("day"))
    keep = _match(qs.get("q"))
    buf = io.StringIO()
    # downloads carry no IDs, except the one list asked for with them (Spotify label songs, for merging later)
    w = csv.writer(buf) if ds.get("keep_ids") else IdFreeWriter(buf)
    w.writerow(header)
    w.writerows(r for r in rows if not keep or keep(r))
    name = f"market_lens_{qs.get('ds')}_{day or date.today().isoformat()}.csv"
    return buf.getvalue().encode("utf-8-sig"), name


def archived(name):
    """An archived CSV (from before 3 Nov 2026), unpacked for download."""
    if not name or not ARCHIVE_RE.fullmatch(name):
        raise ValueError("bad file name")
    f = config.OUT_DIR / "archive" / name
    if not f.is_file():
        raise ValueError("file not found")
    with gzip.open(f, "rb") as fh:
        text = fh.read().decode("utf-8-sig")
    rows = csv.reader(io.StringIO(text))
    buf = io.StringIO()
    IdFreeWriter(buf).writerows(rows)
    return buf.getvalue().encode("utf-8-sig"), name[:-3]
