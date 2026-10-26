"""Spotify Charts (charts.spotify.com): India Top 200, daily and weekly.

Two ways in, both stored in chart_entry (one row per chart, date and rank):
  * fetch   - downloads chart JSON with a charts.spotify.com session token (SPOTIFY_CHARTS_TOKEN in .env,
              copied from a logged-in browser; it expires after about an hour, rerun with a fresh one to resume).
              Raw responses are kept in data/spotify_charts/<chart>/<date>.json.
  * import  - loads those JSON files, and also CSV files saved with the site's "Download CSV" button
              (regional-in-daily-2026-10-20.csv etc.; the CSV carries a streams column, the JSON does not).

Entries link to our songs through the Spotify track id (platform_item.platform_id) at query time.

  python3 -m lens.spotify_charts fetch  [--since 2026-01-01] [--charts regional-in-daily,regional-in-weekly]
  python3 -m lens.spotify_charts import [folder]
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, db

BASE = "https://charts-spotify-com-service.spotify.com/auth/v0/charts/"
CHARTS = ("regional-in-daily", "regional-in-weekly")
STEP = {"daily": 1, "weekly": 7}
PACE = 5  # seconds between requests (about 40 quick requests trigger HTTP 429)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_entry (
    chart          TEXT NOT NULL,     -- regional-in-daily / regional-in-weekly
    chart_date     TEXT NOT NULL,
    rank           INTEGER NOT NULL,
    spotify_id     TEXT NOT NULL,
    track_name     TEXT,
    artists        TEXT,
    label          TEXT,
    release_date   TEXT,
    previous_rank  INTEGER,
    peak_rank      INTEGER,
    peak_date      TEXT,
    appearances    INTEGER,          -- days (or weeks) on the chart so far
    entry_date     TEXT,
    streams        INTEGER,          -- only from CSV downloads
    source         TEXT,             -- json / csv
    PRIMARY KEY (chart, chart_date, rank)
);
CREATE INDEX IF NOT EXISTS chart_entry_track ON chart_entry(spotify_id, chart, chart_date);
"""


def data_dir():
    return config.DB_FILE.parent / "spotify_charts"


def ensure(conn):
    conn.executescript(SCHEMA)


def _token():
    tok = os.environ.get("SPOTIFY_CHARTS_TOKEN") or config._read_env_file().get("SPOTIFY_CHARTS_TOKEN", "")
    tok = tok.strip().removeprefix("Bearer ").strip()
    if not tok:
        sys.exit("SPOTIFY_CHARTS_TOKEN is not set in .env (copy it from a logged-in charts.spotify.com session)")
    return tok


def fetch(since, charts=CHARTS, out=None, log=print):
    """Download chart JSON from the latest date back to `since`; existing files are skipped (resumable)."""
    out, tok = Path(out or data_dir()), _token()

    def get(chart, date):
        p = out / chart / f"{date}.json"
        if p.exists():
            return json.loads(p.read_text())
        req = urllib.request.Request(BASE + f"{chart}/{date}", headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
        for attempt in range(6):
            try:
                d = json.load(urllib.request.urlopen(req, timeout=30))
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise SystemExit(f"token rejected ({e.code}) at {chart} {date}: paste a fresh SPOTIFY_CHARTS_TOKEN and rerun to resume")
                if e.code == 404:
                    return None
                wait = int(e.headers.get("Retry-After") or 0) or 60 * (attempt + 1)  # 429: the service rate-limits bursts
                log(f"  {e.code} at {chart} {date}; waiting {wait}s")
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError):
                time.sleep(15)
        else:
            raise SystemExit(f"giving up at {chart} {date} after repeated errors; rerun later to resume")
        if date != "latest":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d))
        time.sleep(PACE)
        return d

    for chart in charts:
        latest = get(chart, "latest")
        last = dt.date.fromisoformat(latest["displayChart"]["chartMetadata"]["dimensions"]["latestDate"])
        step = dt.timedelta(days=STEP[chart.rsplit("-", 1)[-1]])
        d, n = last, 0
        while d >= since:
            get(chart, d.isoformat())
            n += 1
            d -= step
            if n % 25 == 0:
                log(f"  {chart}: {n} charts, at {d}")
        log(f"{chart}: {n} charts {d + step} .. {last}")


def _rows_json(path, chart, date):
    d = json.loads(path.read_text())
    for e in d.get("entries", []):
        c, m = e["chartEntryData"], e["trackMetadata"]
        yield dict(chart=chart, chart_date=date, rank=c["currentRank"], spotify_id=m["trackUri"].rsplit(":", 1)[-1],
                   track_name=m.get("trackName"), artists=", ".join(a["name"] for a in m.get("artists", [])),
                   label=", ".join(l["name"] for l in m.get("labels", [])), release_date=m.get("releaseDate"),
                   previous_rank=c.get("previousRank") if (c.get("previousRank") or 0) > 0 else None,
                   peak_rank=c.get("peakRank"), peak_date=c.get("peakDate"), appearances=c.get("appearancesOnChart"),
                   entry_date=c.get("entryDate"), streams=None, source="json")


def _rows_csv(path, chart, date):
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            num = lambda k: int(r[k]) if (r.get(k) or "").strip().lstrip("-").isdigit() and int(r[k]) > 0 else None
            yield dict(chart=chart, chart_date=date, rank=int(r["rank"]), spotify_id=r["uri"].rsplit(":", 1)[-1],
                       track_name=r.get("track_name"), artists=r.get("artist_names"), label=r.get("source"), release_date=None,
                       previous_rank=num("previous_rank"), peak_rank=num("peak_rank"), peak_date=None,
                       appearances=num("days_on_chart") or num("weeks_on_chart"), entry_date=None, streams=num("streams"), source="csv")


CSV_NAME = re.compile(r"(regional-[a-z]+-(?:daily|weekly))-(\d{4}-\d{2}-\d{2})\.csv$")


def import_files(conn, folder=None, log=print):
    """Load every chart file under `folder`. A CSV for the same chart and date wins (it has streams)."""
    ensure(conn)
    folder = Path(folder or data_dir())
    files = []
    for p in folder.rglob("*.json"):
        if p.parent.name.startswith("regional-") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
            files.append((p.parent.name, p.stem, p, _rows_json))
    for p in folder.rglob("*.csv"):
        m = CSV_NAME.search(p.name)
        if m:
            files.append((m.group(1), m.group(2), p, _rows_csv))
    files.sort(key=lambda f: f[3] is _rows_csv)  # JSON first, then CSV overwrites
    n = 0
    for chart, date, path, reader in files:
        rows = list(reader(path, chart, date))
        if not rows:
            continue
        old = {r["rank"]: r for r in conn.execute("SELECT rank, streams, release_date, peak_date, entry_date FROM chart_entry WHERE chart=? AND chart_date=?", (chart, date))}
        for r in rows:  # keep what the other format knew
            o = old.get(r["rank"])
            if o:
                for k in ("streams", "release_date", "peak_date", "entry_date"):
                    r[k] = r[k] if r[k] is not None else o[k]
        conn.executemany(f"INSERT OR REPLACE INTO chart_entry ({', '.join(rows[0])}) VALUES ({', '.join(':' + k for k in rows[0])})", rows)
        n += len(rows)
    conn.commit()
    s = conn.execute("SELECT chart, count(DISTINCT chart_date) days, min(chart_date) a, max(chart_date) b FROM chart_entry GROUP BY chart").fetchall()
    log(f"Spotify Charts: {n} rows from {len(files)} files; " + "; ".join(f"{r['chart']} {r['days']} charts {r['a']}..{r['b']}" for r in s))
    return n


LINK_SQL = """
SELECT ce.*, pi.track_id FROM chart_entry ce
JOIN platform_item pi ON pi.platform='spotify' AND pi.platform_id=ce.spotify_id AND pi.track_id IS NOT NULL
  AND pi.review_state IN ('auto','approved')
"""


def song_chart(conn, track_id, charts=CHARTS):
    """Chart history of one song (all its Spotify versions): the daily chart, else the weekly one."""
    ensure(conn)
    for chart in charts:
        rows = [dict(r) for r in conn.execute(f"SELECT chart_date, min(rank) rank, max(streams) streams FROM ({LINK_SQL}) WHERE track_id=? AND chart=? GROUP BY chart_date ORDER BY chart_date", (track_id, chart))]
        if rows:
            break
    else:
        return None
    best = min(rows, key=lambda r: r["rank"])
    return {"chart": chart, "days": len(rows), "best_rank": best["rank"], "best_date": best["chart_date"],
            "first": rows[0]["chart_date"], "last": rows[-1]["chart_date"], "history": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lens.spotify_charts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--since", default="2026-01-01")
    f.add_argument("--charts", default=",".join(CHARTS))
    i = sub.add_parser("import")
    i.add_argument("folder", nargs="?")
    a = ap.parse_args(argv)
    conn = db.connect(str(config.DB_FILE))
    if a.cmd == "fetch":
        fetch(dt.date.fromisoformat(a.since), a.charts.split(","))
    import_files(conn, getattr(a, "folder", None))


if __name__ == "__main__":
    main()
