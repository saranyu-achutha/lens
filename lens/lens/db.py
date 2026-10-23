"""SQLite store. Schema mirrors the planned Postgres model so it can be ported as-is."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_capture (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      INTEGER,
    captured_at TEXT NOT NULL,
    body        TEXT
);

-- Canonical release (our own id; Spotify album id kept as a source reference)
CREATE TABLE IF NOT EXISTS release (
    id                     INTEGER PRIMARY KEY,
    title                  TEXT NOT NULL,
    upc                    TEXT,
    release_date           TEXT,
    release_date_precision TEXT,
    album_type             TEXT,
    label                  TEXT,
    copyrights             TEXT,
    total_tracks           INTEGER,
    spotify_album_id       TEXT UNIQUE,
    discovered_via         TEXT,   -- comma list: label_search, track_search, artist_seed
    label_match            TEXT,   -- label | copyright
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artist (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    spotify_artist_id TEXT UNIQUE
);

-- Canonical recording, keyed by ISRC where available
CREATE TABLE IF NOT EXISTS track (
    id            INTEGER PRIMARY KEY,
    isrc          TEXT UNIQUE,
    title         TEXT NOT NULL,
    duration_ms   INTEGER,
    explicit      INTEGER,
    release_id    INTEGER REFERENCES release(id),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS track_artist (
    track_id  INTEGER REFERENCES track(id),
    artist_id INTEGER REFERENCES artist(id),
    position  INTEGER,
    PRIMARY KEY (track_id, artist_id)
);

-- One row per asset on a platform (Spotify track, YouTube video, ...)
CREATE TABLE IF NOT EXISTS platform_item (
    id           INTEGER PRIMARY KEY,
    platform     TEXT NOT NULL,
    platform_id  TEXT NOT NULL,
    track_id     INTEGER REFERENCES track(id),
    asset_type   TEXT,
    title        TEXT,
    url          TEXT,
    match_method TEXT,     -- isrc | spotify_source | rules
    match_score  REAL,
    review_state TEXT,     -- auto | pending | approved | rejected
    UNIQUE (platform, platform_id)
);

-- Time series: one reading per item/metric/day (never overwritten)
CREATE TABLE IF NOT EXISTS snapshot (
    id               INTEGER PRIMARY KEY,
    platform_item_id INTEGER NOT NULL REFERENCES platform_item(id),
    metric           TEXT NOT NULL,
    value            REAL,
    capture_date     TEXT NOT NULL,
    captured_at      TEXT NOT NULL,
    UNIQUE (platform_item_id, metric, capture_date)
);

-- Every individual metric reading (YouTube is read every 6 h in a video's first week).
-- `snapshot` keeps one value per day (the latest reading of that day).
CREATE TABLE IF NOT EXISTS reading (
    id               INTEGER PRIMARY KEY,
    platform_item_id INTEGER NOT NULL REFERENCES platform_item(id),
    metric           TEXT NOT NULL,
    value            REAL,
    captured_at      TEXT NOT NULL
);

-- Matches a reviewer rejected, so they are never proposed again.
CREATE TABLE IF NOT EXISTS match_rejection (
    platform_item_id INTEGER NOT NULL,
    track_id         INTEGER NOT NULL,
    rejected_at      TEXT NOT NULL,
    PRIMARY KEY (platform_item_id, track_id)
);

-- Every reviewer decision, kept apart from the pipeline data: who, when, what, and a snapshot of
-- the affected rows before the change so it can be undone exactly.
CREATE TABLE IF NOT EXISTS review_decision (
    id          INTEGER PRIMARY KEY,
    decided_at  TEXT NOT NULL,
    decided_by  TEXT,
    action      TEXT NOT NULL,      -- approve | reject | link
    platform    TEXT,
    item_id     INTEGER,
    track_id    INTEGER,
    summary     TEXT,
    before      TEXT NOT NULL,      -- JSON snapshot
    undone_at   TEXT,
    undone_by   TEXT
);

-- Small key/value store for connector state (e.g. completed channel scans).
-- Song metadata: one row per (song, field, value), e.g. (12, 'singer', 'Arijit Singh'). value_key is the
-- normalised value used to roll metrics up across songs; source says where it came from
-- (youtube_credits, spotify, youtube_title, youtube_language, channel, rule).
CREATE TABLE IF NOT EXISTS song_meta (
    track_id  INTEGER NOT NULL REFERENCES track(id),
    field     TEXT NOT NULL,
    value     TEXT NOT NULL,
    value_key TEXT NOT NULL,
    source    TEXT,
    position  INTEGER,
    PRIMARY KEY (track_id, field, value_key)
);
CREATE INDEX IF NOT EXISTS song_meta_field ON song_meta(field, value_key);

-- AI explanations of insight panels (cached per panel, data and day).
CREATE TABLE IF NOT EXISTS ai_note (key TEXT PRIMARY KEY, created_at TEXT, panel TEXT, model TEXT, text TEXT);

-- Song names (song_key) that were merged into another song, so the next grouping run keeps the merge.
CREATE TABLE IF NOT EXISTS song_alias (
    song_key  TEXT PRIMARY KEY,
    track_id  INTEGER NOT NULL REFERENCES track(id),
    created_at TEXT
);

-- Pairs of songs a reviewer marked "not the same song"; merge rules never suggest them again.
CREATE TABLE IF NOT EXISTS merge_rejection (
    key_a TEXT NOT NULL,
    key_b TEXT NOT NULL,
    rejected_at TEXT,
    PRIMARY KEY (key_a, key_b)
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Albums checked and found not to be Sony Music India, so daily runs don't re-fetch them.
-- Keyed by the match rule in force: editing the rule in Settings re-checks everything.
CREATE TABLE IF NOT EXISTS rejected_album (
    spotify_album_id TEXT NOT NULL,
    label_regex      TEXT NOT NULL,
    label            TEXT,
    release_date     TEXT,
    checked_at       TEXT NOT NULL,
    PRIMARY KEY (spotify_album_id, label_regex)
);

-- Connector health checks (manual "Test connection" and the automatic watcher)
CREATE TABLE IF NOT EXISTS api_check (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    status     INTEGER,
    message    TEXT,
    trigger    TEXT,
    retry_at   TEXT     -- for quota blocks: when Spotify allows requests again
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    stats       TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # the studio reads while a job writes
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()  # never leave the migration's implicit write transaction open (it would lock out other writers)
    return conn


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(raw_capture)")}
    if "run_id" not in cols:
        conn.execute("ALTER TABLE raw_capture ADD COLUMN run_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS raw_capture_run ON raw_capture(run_id)")
    pi_cols = {r["name"] for r in conn.execute("PRAGMA table_info(platform_item)")}
    for col, typ in (("published_at", "TEXT"), ("duration_ms", "INTEGER"), ("channel", "TEXT"),
                     ("description", "TEXT"), ("match_detail", "TEXT"), ("first_seen_at", "TEXT"),
                     ("yt_tags", "TEXT"), ("audio_language", "TEXT"), ("yt_category", "TEXT"), ("snippet_at", "TEXT")):
        if col not in pi_cols:
            conn.execute(f"ALTER TABLE platform_item ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS platform_item_track ON platform_item(track_id)")
    # Songs discovered on YouTube (track.source='youtube') and their Spotify lookup state.
    t_cols = {r["name"] for r in conn.execute("PRAGMA table_info(track)")}
    for col, typ in (("source", "TEXT"), ("song_key", "TEXT"), ("yt_title", "TEXT"), ("first_published", "TEXT"),
                     ("release_status", "TEXT"), ("spotify_checked_at", "TEXT")):
        if col not in t_cols:
            conn.execute(f"ALTER TABLE track ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS track_song_key ON track(song_key)")
    if conn.execute("SELECT 1 FROM track WHERE source IS NULL LIMIT 1").fetchone():
        conn.execute("UPDATE track SET source='spotify' WHERE source IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS reading_item ON reading(platform_item_id, metric, captured_at)")
    if "retry_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(api_check)")}:
        conn.execute("ALTER TABLE api_check ADD COLUMN retry_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS api_check_source ON api_check(source, id)")
