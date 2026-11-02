"""Server health for the Developer dashboard: CPU, memory, swap, disk, the app's folder sizes and network, from /proc
and the file system (Linux, standard library only; other systems get what os/shutil can tell).

Kept light: the hourly sample (worker loop, into system_stat) reads only counters (/proc, disk usage, the database
files' sizes) and never sleeps or walks folders. CPU % is the average since the previous reading (an hour for the
samples). Folder sizes are added up only when the Developer page asks, at most every 6 hours.
"""
import os
import platform
import shutil
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS system_stat (
    at          TEXT PRIMARY KEY,   -- hour of the sample (UTC, ISO)
    cpu_pct     REAL,
    load1       REAL,
    mem_pct     REAL,
    swap_pct    REAL,
    disk_used   INTEGER,
    disk_total  INTEGER,
    db_bytes    INTEGER
);
"""
_FOLDERS = {"t": 0, "v": None}  # folder sizes are the only slow part: added up at most every 6 hours
_CPU = {"t": None}  # previous (total, idle) CPU times, for the average since then


def ensure(conn):
    conn.executescript(SCHEMA)


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _cpu_times():
    f = _read("/proc/stat").split("\n", 1)[0].split()[1:]
    v = [int(x) for x in f] if f else []
    return (sum(v), v[3] + (v[4] if len(v) > 4 else 0)) if v else (0, 0)


def cpu_pct():
    """Average CPU use since the previous call (no waiting); since boot on the first call."""
    t2, i2 = _cpu_times()
    t1, i1 = _CPU["t"] or (0, 0)
    _CPU["t"] = (t2, i2)
    return round(100 * (1 - (i2 - i1) / (t2 - t1)), 1) if t2 > t1 else None


def meminfo():
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip().endswith("kB"):
            m[k] = int(v.split()[0]) * 1024
    return m


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def db_bytes():
    """The database and its WAL / shared-memory files (three stat calls)."""
    data = config.DB_FILE.parent
    return sum(f.stat().st_size for f in data.glob(config.DB_FILE.name + "*") if f.is_file())


def folders():
    """Sizes of the app's own data folders (added up at most every 6 hours)."""
    if _FOLDERS["v"] is not None and time.time() - _FOLDERS["t"] < 6 * 3600:
        return _FOLDERS["v"]
    data = config.DB_FILE.parent
    out = [("Database (with WAL)", db_bytes())]
    for name, p in (("Database backups", data / "backups"), ("Spotify play files", data / "spotify_plays"),
                    ("YouTube archive", data / "youtube_archive"), ("Spotify charts", data / "spotify_charts"),
                    ("CSV outputs", config.OUT_DIR)):
        if p.exists():
            out.append((name, dir_size(p)))
    _FOLDERS.update(t=time.time(), v=[{"name": n, "bytes": b} for n, b in out])
    return _FOLDERS["v"]


def console_rss():
    """Memory used by the Console process."""
    for line in _read("/proc/self/status").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def network():
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return {"rx": rx, "tx": tx}


def counters():
    """The cheap part: CPU, load, memory, swap, disk, uptime (what the hourly sample stores)."""
    m = meminfo()
    du = shutil.disk_usage(config.DB_FILE.parent)
    up = _read("/proc/uptime").split()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    mem_total, mem_avail = m.get("MemTotal"), m.get("MemAvailable")
    swap_total, swap_free = m.get("SwapTotal", 0), m.get("SwapFree", 0)
    return {
        "host": socket.gethostname(), "os": " ".join(x for x in (platform.system(), platform.release()) if x),
        "distro": next((l.split("=", 1)[1].strip('"') for l in _read("/etc/os-release").splitlines() if l.startswith("PRETTY_NAME=")), None),
        "arch": platform.machine(), "python": sys.version.split()[0], "cores": os.cpu_count(),
        "uptime_s": float(up[0]) if up else None,
        "cpu_pct": cpu_pct(), "load": [round(x, 2) for x in load] if load[0] is not None else None,
        "mem_total": mem_total, "mem_used": mem_total - mem_avail if mem_total and mem_avail is not None else None,
        "swap_total": swap_total, "swap_used": swap_total - swap_free,
        "disk_total": du.total, "disk_used": du.total - du.free, "disk_free": du.free, "db_bytes": db_bytes(),
    }


def snapshot():
    """Everything shown on the page: the counters plus folder sizes, the Console's memory and network totals."""
    return dict(counters(), console_rss=console_rss(), network=network(), folders=folders())


def record(conn, s=None):
    """Store this hour's sample (once an hour; later calls in the same hour replace it)."""
    ensure(conn)
    s = s or counters()
    at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
    pct = lambda a, b: round(100 * a / b, 1) if a is not None and b else None
    conn.execute("INSERT OR REPLACE INTO system_stat (at, cpu_pct, load1, mem_pct, swap_pct, disk_used, disk_total, db_bytes) VALUES (?,?,?,?,?,?,?,?)",
                 (at, s["cpu_pct"], s["load"][0] if s["load"] else None, pct(s["mem_used"], s["mem_total"]), pct(s["swap_used"], s["swap_total"]),
                  s["disk_used"], s["disk_total"], s["db_bytes"]))
    conn.commit()


def history(conn, days=7):
    ensure(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="minutes")
    rows = [dict(r) for r in conn.execute("SELECT * FROM system_stat WHERE at >= ? ORDER BY at", (since,))]
    # Disk growth per day over the samples (at least 12 hours apart) -> days until full at that rate
    growth = days_left = None
    if len(rows) >= 2:
        a, b = rows[0], rows[-1]
        hours = (datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])).total_seconds() / 3600
        if hours >= 12 and b["disk_used"] is not None and a["disk_used"] is not None:
            growth = (b["disk_used"] - a["disk_used"]) / hours * 24
            if growth > 0 and b["disk_total"]:
                days_left = round((b["disk_total"] - b["disk_used"]) / growth)
    return {"samples": rows, "disk_growth_per_day": growth, "disk_days_left": days_left}


def api(conn):
    """The page's numbers; the hourly samples are written by the worker, not by page loads."""
    return dict(snapshot(), history=history(conn))


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), indent=1))
