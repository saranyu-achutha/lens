"""Background job runner (one job at a time) with live log and a daily scheduler."""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import config, db, discover_spotify, health, jiosaavn, song_daily, songs, spotify_plays, sync_youtube, youtube_music
from .spotify import SpotifyError
from .youtube import YouTubeError

JOBS = {
    "pipeline": "Pipeline: YouTube sync, songs (Spotify metadata, Deezer rank), YouTube Music, JioSaavn",
    "youtube_sync": "YouTube sync only (new videos, views/likes/comments)",
    "songs": "Songs only (group videos, Spotify lookup, Deezer rank)",
    "spotify_discovery": "Spotify label crawl (heavy on Spotify quota, manual only)",
    "ytmusic": "YouTube Music only (find songs' audio tracks, read their plays)",
    "jiosaavn": "JioSaavn only (find songs' tracks, read their plays)",
    "spotify_plays": "Spotify play counts (public track pages, light headless browser)",
}
STEPS = {
    "pipeline": [("youtube", sync_youtube.run), ("songs", songs.run), ("ytmusic", youtube_music.run), ("jiosaavn", jiosaavn.run), ("daily", song_daily.run)],
    "jiosaavn": [("jiosaavn", jiosaavn.run), ("daily", song_daily.run)],
    "spotify_plays": [("spotify_plays", spotify_plays.run), ("daily", song_daily.run)],
    "ytmusic": [("ytmusic", youtube_music.run), ("daily", song_daily.run)],
    "youtube_sync": [("youtube", sync_youtube.run), ("daily", song_daily.run)],
    "songs": [("songs", songs.run), ("daily", song_daily.run)],
    "spotify_discovery": [("spotify", discover_spotify.run)],
}
SEVERITY = ["ok", "partial", "quota_exceeded", "cancelled", "failed"]
CANCELLED = (discover_spotify.Cancelled, sync_youtube.Cancelled, songs.Cancelled, youtube_music.Cancelled, jiosaavn.Cancelled)


class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = None  # dict(job, run_id, started_at, status, log[list], stop[Event])
        self.last = None

    def state(self, log_from=0):
        with self.lock:
            j = self.current or self.last
            if not j:
                return {"running": False}
            return {"running": self.current is not None, "job": j["job"], "run_id": j["run_id"],
                    "started_at": j["started_at"], "status": j["status"], "trigger": j["trigger"],
                    "log_total": len(j["log"]), "log": j["log"][log_from:]}

    def start(self, job, trigger="manual", **params):
        if job not in JOBS:
            raise ValueError(f"unknown job {job}")
        with self.lock:
            if self.current:
                raise RuntimeError(f"{self.current['job']} is already running")
            conn = db.connect(str(config.DB_FILE))
            run_id = conn.execute("INSERT INTO run_log (job, started_at, status) VALUES (?, ?, 'running')",
                                  (job, db.now_iso())).lastrowid
            conn.commit()
            conn.close()
            self.current = {"job": job, "run_id": run_id, "started_at": db.now_iso(), "status": "running",
                            "trigger": trigger, "log": [], "stop": threading.Event()}
            j = self.current
        threading.Thread(target=self._run, args=(j, params), daemon=True).start()
        return run_id

    def cancel(self):
        with self.lock:
            if self.current:
                self.current["stop"].set()
                return True
        return False

    def _log(self, j, msg):
        j["log"].append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def _run(self, j, params):
        conn = db.connect(str(config.DB_FILE))
        steps = STEPS[j["job"]]
        stats, results = {}, {}
        for name, fn in steps:
            if j["stop"].is_set():
                break
            if len(steps) > 1:
                self._log(j, f"── {name} ──")
            try:
                st = fn(conn, config.load(), config.read_secrets(), log=lambda m: self._log(j, m),
                        should_stop=j["stop"].is_set, since=params.get("since") or None,
                        until=params.get("until") or None, run_id=j["run_id"])
                step_status = st.get("status", "ok")
                if name == "spotify" and step_status == "quota_exceeded":
                    health.record(conn, False, 429, st["stop_reason"], f"run #{j['run_id']}", st["retry_at"])
            except CANCELLED:
                st, step_status = {}, "cancelled"
                self._log(j, "cancelled by user (data stored so far is kept)")
            except (SpotifyError, YouTubeError) as e:  # one source failing doesn't stop the next
                st, step_status = {"error": str(e)}, "failed"
                self._log(j, f"ERROR ({name}) {e}")
            except Exception as e:  # keep the studio alive and show the failure
                st, step_status = {"error": repr(e)}, "failed"
                self._log(j, "ERROR " + traceback.format_exc())
            results[name] = {"status": step_status, "stop_reason": st.get("stop_reason"), "error": st.get("error")}
            stats.update({k: v for k, v in st.items() if k not in ("status", "stop_reason", "error", "csv")})
            if st.get("csv"):
                stats.setdefault("csvs", []).append(st["csv"])
        status = max((r["status"] for r in results.values()), key=SEVERITY.index, default="cancelled")
        stats["steps"] = results
        problems = [f"{n}: {r['error'] or r['stop_reason']}" for n, r in results.items() if r["error"] or r["stop_reason"]]
        if problems:
            stats["stop_reason" if status != "failed" else "error"] = " · ".join(problems)
        self._log(j, "done" if status == "ok" else f"finished: {status}")
        conn.commit()
        stats["trigger"] = j["trigger"]
        stats["log_tail"] = j["log"][-200:]
        conn.execute("UPDATE run_log SET finished_at=?, status=?, stats=? WHERE id=?",
                     (db.now_iso(), status, json.dumps(stats), j["run_id"]))
        conn.commit()
        conn.close()
        with self.lock:
            j["status"] = status
            j["ended_at"] = time.time()
            self.last, self.current = j, None


def next_scheduled(cfg):
    """When the scheduler will next fire (for display)."""
    s = cfg["schedule"]
    if not s["enabled"]:
        return None
    now = datetime.now()
    hh, mm = (int(x) for x in s["time"].split(":"))
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t <= now and not ran_ok_today():
        return now
    return t if t > now else t + timedelta(days=1)


def ran_ok_today():
    conn = db.connect(str(config.DB_FILE))
    today = datetime.now().astimezone().date()
    rows = conn.execute("SELECT started_at FROM run_log WHERE job IN ('spotify_discovery','pipeline') "
                        "AND status IN ('ok','partial','running')"
                        " ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    return any(datetime.fromisoformat(r["started_at"]).astimezone().date() == today for r in rows)


def hours_since_youtube():
    conn = db.connect(str(config.DB_FILE))
    row = conn.execute("SELECT max(started_at) FROM run_log WHERE job IN ('youtube_sync','pipeline') "
                       "AND status NOT IN ('failed','interrupted')").fetchone()
    conn.close()
    if not row[0]:
        return float("inf")
    return (datetime.now().astimezone() - datetime.fromisoformat(row[0])).total_seconds() / 3600


def start_scheduler(manager):
    """After the configured time each day, run discovery unless a successful run already
    happened today. Also catches up a missed run if the studio was down at that time."""
    def loop():
        last_attempt = last_yt_attempt = 0.0
        while True:
            cfg = config.load()
            s = cfg["schedule"]
            # A failed scheduled run is retried at most once an hour.
            if s["enabled"] and not manager.current and time.time() - last_attempt > 3600:
                hh, mm = (int(x) for x in s["time"].split(":"))
                now = datetime.now()
                if now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0) and not ran_ok_today():
                    last_attempt = time.time()
                    try:
                        manager.start("pipeline", trigger="schedule")
                    except RuntimeError:
                        pass
            # YouTube every N hours (6-hourly readings for videos in their first week).
            y = cfg["youtube"]
            if y["enabled"] and not manager.current and config.read_secrets().get("YOUTUBE_API_KEY") \
                    and hours_since_youtube() >= y["every_hours"] and time.time() - last_yt_attempt > 1800:
                last_yt_attempt = time.time()
                try:
                    manager.start("pipeline", trigger="schedule")
                except RuntimeError:
                    pass
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()


# ---- Keep the server busy: between scheduled pipelines, run whatever useful work is due, within each source's limits.
def _count(sql, args=()):
    conn = db.connect(str(config.DB_FILE))
    try:
        return conn.execute(sql, args).fetchone()[0] or 0
    finally:
        conn.close()


def _due_work(cfg, tried):
    """The next job worth running now, or None. `tried` holds when each job was last started by this loop."""
    since, until = config.window(cfg)
    today = datetime.now().date().isoformat()
    now = time.time()
    ago = lambda job: now - tried.get(job, 0)
    in_window = "t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?"
    # 1. Spotify play counts: once a day for tracks without today's count (needs a browser on this machine).
    if ago("spotify_plays") > 2 * 3600:
        try:
            spotify_plays.find_chrome()
            conn = db.connect(str(config.DB_FILE))
            n = len(spotify_plays.due_ids(conn))
            conn.close()
            if n:
                return "spotify_plays", f"{n} Spotify tracks without today's play count"
        except SystemExit:
            pass
    # 2. JioSaavn: songs never searched (or due a retry) and tracks without today's plays.
    if ago("jiosaavn") > 600:
        recheck = (datetime.now().date() - timedelta(days=7)).isoformat()
        todo = _count(f"""SELECT count(*) FROM track t WHERE {in_window}
                          AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='jiosaavn')
                          AND coalesce((SELECT value FROM state WHERE key='js_lookup:' || t.id), '') < ?""", (since, until, recheck))
        unread = _count("""SELECT count(*) FROM platform_item p WHERE p.platform='jiosaavn' AND p.track_id IS NOT NULL
                           AND NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.metric='plays' AND s.capture_date=?)""", (today,))
        if todo or unread:
            return "jiosaavn", f"{todo} songs to search, {unread} tracks to read"
    # 3. Songs: Spotify / iTunes / MusicBrainz lookups still pending, or Deezer not read today (at most hourly).
    if ago("songs") > 3600 and hours_since("songs") >= 1:
        pending = _count(f"""SELECT count(*) FROM track t WHERE {in_window} AND (t.release_status IS NULL OR t.release_status='unchecked')""", (since, until))
        dz = _count("""SELECT count(*) FROM platform_item p WHERE p.platform='deezer' AND p.track_id IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.platform_item_id=p.id AND s.capture_date=?)""", (today,))
        if pending or dz:
            return "songs", f"{pending} songs awaiting lookups, {dz} Deezer ranks to read"
    # 4. YouTube Music: songs not yet searched, while today's search quota lasts (one try an hour).
    if ago("ytmusic") > 3600:
        from .youtube import _SET_ASIDE, parse_keys
        keys = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
        can_search = any(_SET_ASIDE.get((k, "search"), (0, ""))[0] <= now and _SET_ASIDE.get(k, (0, ""))[0] <= now for k in keys)
        todo = _count(f"""SELECT count(*) FROM track t WHERE {in_window}
                          AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='ytmusic')
                          AND coalesce((SELECT value FROM state WHERE key='ytm_lookup:' || t.id), '') < ?""",
                      (since, until, (datetime.now().date() - timedelta(days=7)).isoformat()))
        if can_search and todo:
            return "ytmusic", f"{todo} songs to search on YouTube Music"
    return None


def hours_since(job):
    conn = db.connect(str(config.DB_FILE))
    row = conn.execute("SELECT max(started_at) FROM run_log WHERE job IN (?, 'pipeline') AND status NOT IN ('failed','interrupted')", (job,)).fetchone()
    conn.close()
    if not row[0]:
        return float("inf")
    return (datetime.now().astimezone() - datetime.fromisoformat(row[0])).total_seconds() / 3600


def start_worker(manager):
    """Between scheduled runs, keep the machine working on whatever is due (config worker.enabled)."""
    def loop():
        tried, idle_since = {}, None
        time.sleep(90)  # let the scheduler claim a due pipeline first
        while True:
            try:
                cfg = config.load()
                w = cfg.get("worker", {})
                last_end = manager.last and manager.last.get("ended_at")
                pause_ok = not last_end or time.time() - last_end >= w.get("pause_seconds", 60)
                if w.get("enabled", True) and not manager.current and pause_ok:
                    due = _due_work(cfg, tried)
                    if due:
                        job, why = due
                        tried[job] = time.time()
                        try:
                            manager.start(job, trigger="worker")
                            print(f"worker: started {job} ({why})", flush=True)
                        except RuntimeError:
                            pass
            except Exception:
                traceback.print_exc()
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()


def start_health_watcher(manager):
    """Re-check Spotify access in the background: every `recheck_minutes` while it is failing,
    every 6 hours while healthy. On recovery, optionally start discovery straight away."""
    def loop():
        time.sleep(5)
        while True:
            try:
                cfg = config.load()
                s = config.read_secrets()
                if s.get("SPOTIFY_CLIENT_ID") and s.get("SPOTIFY_CLIENT_SECRET"):
                    conn = db.connect(str(config.DB_FILE))
                    last = health.last_check(conn)
                    every = 6 * 3600 if last and last["ok"] else cfg["health"]["recheck_minutes"] * 60
                    now = datetime.now().astimezone()
                    age = (now - datetime.fromisoformat(last["checked_at"])).total_seconds() if last else None
                    blocked = last and last.get("retry_at") and now < datetime.fromisoformat(last["retry_at"])
                    if not blocked and (age is None or age >= every or last.get("retry_at")):
                        res = health.check_spotify(conn, trigger="watcher")
                        recovered = res["ok"] and not (last and last["ok"])
                        if recovered and cfg["health"]["auto_run_on_recovery"] and not manager.current \
                                and not ran_ok_today():
                            manager.start("pipeline", trigger="recovery")
                    conn.close()
            except Exception:
                traceback.print_exc()
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()
