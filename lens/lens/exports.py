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


def _songs(conn, day):
    from . import songs
    rows = songs.export_rows(conn, *config.window(config.load()))
    return (list(rows[0].keys()) if rows else []), (tuple(r.values()) for r in rows)


def _sql(sql, params=lambda day: ()):
    def fn(conn, day):
        cur = conn.execute(sql, params(day))
        return [d[0] for d in cur.description], (tuple(r) for r in cur)
    return fn


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
                                          JOIN track t ON t.id=m.track_id WHERE t.first_published >= ? AND substr(t.first_published,1,10) <= ?
                                          ORDER BY m.track_id, m.field, m.position""", _window)},
        "youtube_videos": {"name": "YouTube videos", "what": "every stored upload with its match and latest views, likes and comments", "rows": _sql(YT_EXPORT_SQL)},
        "spotify_label_crawl": {"name": "Spotify label crawl", "what": "tracks from the manual Spotify label crawl", "rows": _sql(EXPORT_SQL)},
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
    w = csv.writer(buf)
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
        return fh.read(), name[:-3]
