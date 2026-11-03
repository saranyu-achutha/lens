"""Daily cleanup: gives disk space back without losing anything. Nothing is deleted that cannot be rebuilt or is not
kept elsewhere: debugging data is compressed into archive files, not removed (user rule, 2026-11-03).

Once a day (worker, after `cleanup.after_hour`; also `python3 -m lens.cleanup`):
  1. Database backup, if today's is missing (the server may have been off at the 02:30 cron): VACUUM INTO a compact
     copy, gzipped to data/backups/lens_<day>.db.gz; dated backups older than `backup_keep_days` are removed.
  2. Raw API replies (raw_capture) older than `raw_keep_days`: written to data/archive/raw_capture/ as one
     jsonl.gz per day (checked line by line), then removed from the database.
  3. CSV files left in out/ (runs stopped writing them on 2026-11-03; downloads are built on request): gzipped into out/archive/.
  4. Loose uncompressed database copies in data/backups (one-off copies such as the pre-merge one): gzipped.
  5. Browser profiles left in /tmp by a crashed reader (older than 12 hours): removed.
  6. Space freed inside the database is returned to the disk (incremental vacuum; the first run switches the
     database to incremental mode with one full VACUUM).
  7. System logs over 200 MB and the apt download cache (only where sudo needs no password).
"""
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, db

BACKUPS = config.DB_FILE.parent / "backups"
RAW_ARCHIVE = config.DB_FILE.parent / "archive" / "raw_capture"
OUT_ARCHIVE = config.OUT_DIR / "archive"
DATED_BACKUP = re.compile(r"^lens_(\d{4}-\d{2}-\d{2})\.db\.gz$")


def _settings(cfg):
    return {"enabled": True, "after_hour": 3, "raw_keep_days": 7, "backup_keep_days": 14,
            **(cfg.get("cleanup") or {})}


def _gzip(src, dst):
    """Compress src into dst (written beside it first, so a half-written file never looks complete)."""
    tmp = dst.with_name(dst.name + ".part")
    with open(src, "rb") as fi, gzip.open(tmp, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    os.replace(tmp, dst)
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))


def backup(log=print, keep_days=14):
    """Today's database backup if it is missing. Returns bytes written (0 if it already existed)."""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    z = BACKUPS / f"lens_{today}.db.gz"
    made = 0
    if not z.exists():
        need = config.DB_FILE.stat().st_size * 2
        if shutil.disk_usage(BACKUPS).free < need:
            raise RuntimeError(f"not enough free space for a backup (need {need >> 20} MB)")
        raw = BACKUPS / f"lens_{today}.db"
        raw.unlink(missing_ok=True)
        src = sqlite3.connect(str(config.DB_FILE), timeout=300)
        try:
            src.execute("VACUUM INTO ?", (str(raw),))  # a consistent, compact copy; other jobs keep writing
        finally:
            src.close()
        try:
            _gzip(raw, z)
        finally:
            raw.unlink(missing_ok=True)
        made = z.stat().st_size
        log(f"backup: {z.name} written ({made >> 20} MB)")
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    for f in BACKUPS.iterdir():
        m = DATED_BACKUP.match(f.name)
        if m and m.group(1) < cutoff:
            f.unlink()
            log(f"backup: {f.name} removed (older than {keep_days} days)")
    return made


def archive_raw(conn, keep_days, log=print, should_stop=lambda: False):
    """Move raw API replies older than keep_days into per-day jsonl.gz files. Returns (rows, body bytes)."""
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    days = [r[0] for r in conn.execute("SELECT DISTINCT substr(captured_at,1,10) FROM raw_capture WHERE captured_at < ? ORDER BY 1", (cutoff,))]
    RAW_ARCHIVE.mkdir(parents=True, exist_ok=True)
    rows = size = 0
    cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_capture)")]
    for day in days:
        if should_stop():
            break
        lo, hi, n, b = conn.execute("SELECT min(id), max(id), count(*), coalesce(sum(length(body)),0) FROM raw_capture WHERE substr(captured_at,1,10)=?",
                                    (day,)).fetchone()
        f = RAW_ARCHIVE / f"raw_capture_{day}_{lo}-{hi}.jsonl.gz"
        tmp = f.with_name(f.name + ".part")
        written = 0
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fo:
            for r in conn.execute(f"SELECT {','.join(cols)} FROM raw_capture WHERE substr(captured_at,1,10)=? ORDER BY id", (day,)):
                fo.write(json.dumps(dict(zip(cols, r)), ensure_ascii=False) + "\n")
                written += 1
        with gzip.open(tmp, "rt", encoding="utf-8") as fi:  # read it back before anything leaves the database
            back = sum(1 for _ in fi)
        if written != n or back != n:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"raw_capture {day}: archive check failed ({written}/{back} of {n} rows)")
        os.replace(tmp, f)
        while True:  # small deletes, so other jobs' writes are never held up for long
            cur = conn.execute("DELETE FROM raw_capture WHERE id IN (SELECT id FROM raw_capture WHERE substr(captured_at,1,10)=? LIMIT 2000)", (day,))
            conn.commit()
            if cur.rowcount < 2000:
                break
        rows += n
        size += b
        log(f"raw replies {day}: {n:,} rows ({b >> 20} MB) archived to {f.name} ({f.stat().st_size >> 20} MB)")
    return rows, size


def compress_outputs(log=print):
    """Gzip CSV files left in out/ into out/archive/ (nothing writes them any more). Returns bytes saved."""
    if not config.OUT_DIR.exists():
        return 0
    OUT_ARCHIVE.mkdir(parents=True, exist_ok=True)
    saved = n = 0
    for f in config.OUT_DIR.glob("*.csv"):
        if time.time() - f.stat().st_mtime < 3600:  # a file someone is still writing by hand
            continue
        before = f.stat().st_size
        z = OUT_ARCHIVE / (f.name + ".gz")
        _gzip(f, z)
        f.unlink()
        saved += before - z.stat().st_size
        n += 1
    if n:
        log(f"CSV files: {n} gzipped into out/archive ({saved >> 20} MB saved)")
    return saved


def compress_loose_backups(log=print):
    """Gzip uncompressed database copies in data/backups (not touched for a day and not open). Returns bytes saved."""
    saved = 0
    if not BACKUPS.exists():
        return 0
    for f in sorted(BACKUPS.glob("*.db")):
        wal = f.with_name(f.name + "-wal")
        if time.time() - f.stat().st_mtime < 86400 or (wal.exists() and wal.stat().st_size):
            continue
        z = f.with_name(f.name + ".gz")
        if z.exists():
            continue
        before = f.stat().st_size
        _gzip(f, z)
        f.unlink()
        for side in (wal, f.with_name(f.name + "-shm")):
            side.unlink(missing_ok=True)
        saved += before - z.stat().st_size
        log(f"backups: {f.name} gzipped ({before >> 20} MB -> {z.stat().st_size >> 20} MB)")
    return saved


def clear_temp_profiles(log=print):
    """Browser profiles a crashed reader left behind (each run removes its own). Returns bytes freed."""
    freed = 0
    for d in Path(tempfile.gettempdir()).glob("lens_[si][pg]_*"):
        if d.is_dir() and time.time() - d.stat().st_mtime > 12 * 3600:
            freed += sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
            shutil.rmtree(d, ignore_errors=True)
    if freed:
        log(f"temp: old browser profiles removed ({freed >> 20} MB)")
    return freed


def reclaim(conn, log=print, should_stop=lambda: False):
    """Return the database's free pages to the disk. Returns bytes the file shrank by."""
    before = config.DB_FILE.stat().st_size
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        if free * page < 100 << 20:
            return 0  # not worth a full rewrite yet
        if shutil.disk_usage(config.DB_FILE.parent).free < before * 2:
            log("database: not enough free disk for the one-time VACUUM; skipped")
            return 0
        log(f"database: one-time VACUUM to switch to incremental mode ({free * page >> 20} MB free inside)")
        conn.commit()  # VACUUM can't run inside a transaction
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
    else:
        while free and not should_stop():
            conn.execute("PRAGMA incremental_vacuum(25000)").fetchall()  # about 100 MB a step
            conn.commit()
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    for _ in range(20):  # the rewrite sits in the WAL until no other job is mid-read; wait for a gap (up to 10 minutes)
        if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0:
            break
        time.sleep(30)
    shrunk = before - config.DB_FILE.stat().st_size
    if shrunk > 0:
        log(f"database: {shrunk >> 20} MB returned to the disk ({config.DB_FILE.stat().st_size >> 20} MB now)")
    return max(shrunk, 0)


def system(log=print):
    """System logs and apt cache, where sudo needs no password. Returns bytes freed."""
    before = shutil.disk_usage("/").free
    for cmd in (["journalctl", "--vacuum-size=200M"], ["apt-get", "clean"]):
        try:
            subprocess.run(["sudo", "-n", *cmd], capture_output=True, timeout=300)
        except Exception:
            pass
    freed = max(shutil.disk_usage("/").free - before, 0)
    if freed >> 20:
        log(f"system: logs and apt cache trimmed ({freed >> 20} MB)")
    return freed


def done_today(conn):
    return bool(conn.execute("SELECT 1 FROM run_log WHERE job='cleanup' AND status IN ('ok','partial') AND started_at >= ?",
                             (date.today().isoformat(),)).fetchone())


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, **_):
    s = _settings(cfg)
    disk_before = shutil.disk_usage(config.DB_FILE.parent).free
    stats = {"status": "ok"}
    try:
        stats["cleanup_backup_bytes"] = backup(log, s["backup_keep_days"])
    except Exception as e:  # without a fresh backup, nothing is taken out of the database today
        log(f"backup failed: {e}; raw replies stay in the database today")
        stats.update(status="partial", stop_reason=f"backup: {e}")
        s["raw_keep_days"] = None
    if s["raw_keep_days"] is not None:
        stats["cleanup_raw_rows"], stats["cleanup_raw_bytes"] = archive_raw(conn, s["raw_keep_days"], log, should_stop)
    stats["cleanup_outputs_saved"] = compress_outputs(log)
    stats["cleanup_backups_saved"] = compress_loose_backups(log)
    stats["cleanup_temp_freed"] = clear_temp_profiles(log)
    stats["cleanup_db_shrunk"] = reclaim(conn, log, should_stop)
    stats["cleanup_system_freed"] = system(log)
    stats["cleanup_disk_freed"] = max(shutil.disk_usage(config.DB_FILE.parent).free - disk_before, 0)
    log(f"cleanup: {stats['cleanup_disk_freed'] / 2**30:.2f} GB of disk given back; nothing lost (archives in data/archive, out/archive)")
    return stats


if __name__ == "__main__":
    if sys.argv[1:] == ["backup"]:  # the 02:30 cron
        print(f"{datetime.now()} backup: {backup(keep_days=_settings(config.load())['backup_keep_days']) >> 20} MB")
    else:
        c = db.connect(str(config.DB_FILE))
        print(run(c, config.load(), {}))
