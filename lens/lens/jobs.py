"""Background job runner (one job at a time) with live log and a daily scheduler."""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import alerts, cleanup, config, db, discover_spotify, health, instagram_audio, jiosaavn, shorts_use, song_daily, songs, spotify_plays, sync_youtube, sysstats, youtube_music
from .spotify import SpotifyError
from .youtube import YouTubeError

JOBS = {
    "pipeline": "Pipeline: YouTube sync, songs (Spotify metadata, Deezer rank), YouTube Music, JioSaavn",
    "youtube_sync": "YouTube sync only (new videos, views/likes/comments)",
    "songs": "Songs only (group videos, Spotify lookup, Deezer rank)",
    "spotify_second_account": "Spotify re-discover: songs without a Spotify link, on account 2 (automatic, own daily cap)",
    "spotify_discovery": "Spotify label crawl (heavy on Spotify quota, manual only)",
    "ytmusic": "YouTube Music only (find songs' audio tracks, read their plays)",
    "jiosaavn": "JioSaavn only (find songs' tracks, read their plays)",
    "spotify_plays": "Spotify play counts (public track pages, light headless browser)",
    "shorts": "YouTube Shorts made with each song's official audio (public sound pages)",
    "instagram": "Instagram Reels made with each song's official audio (public audio pages, light headless browser)",
    "cleanup": "Daily cleanup: backup if missing, old raw replies and CSVs compressed into archives, disk space given back",
}
STEPS = {
    "pipeline": [("youtube", sync_youtube.run), ("songs", songs.run), ("ytmusic", youtube_music.run), ("jiosaavn", jiosaavn.run), ("daily", song_daily.run)],
    "jiosaavn": [("jiosaavn", jiosaavn.run), ("daily", song_daily.run)],
    "spotify_plays": [("spotify_plays", spotify_plays.run), ("daily", song_daily.run)],
    "ytmusic": [("ytmusic", youtube_music.run), ("daily", song_daily.run)],
    "youtube_sync": [("youtube", sync_youtube.run), ("daily", song_daily.run)],
    "songs": [("songs", songs.run), ("daily", song_daily.run)],
    "spotify_second_account": [("spotify_second_account", songs.run_spotify_second_account)],
    "spotify_discovery": [("spotify", discover_spotify.run)],
    "shorts": [("shorts", shorts_use.run), ("daily", song_daily.run)],
    "instagram": [("instagram", instagram_audio.run), ("daily", song_daily.run)],
    "cleanup": [("cleanup", cleanup.run)],
}
SEVERITY = ["ok", "partial", "quota_exceeded", "cancelled", "failed"]
CANCELLED = (discover_spotify.Cancelled, sync_youtube.Cancelled, songs.Cancelled, youtube_music.Cancelled, jiosaavn.Cancelled, shorts_use.Cancelled, instagram_audio.Cancelled)


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
            step_start = time.time()
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
            results[name] = {"status": step_status, "stop_reason": st.get("stop_reason"), "error": st.get("error"),
                             "seconds": round(time.time() - step_start, 1)}
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


BROWSER_JOBS = ("spotify_plays", "instagram")  # CPU-heavy headless-browser work, run in its own lane beside the API jobs


def _due_work(cfg, tried, browser=True, api=True):
    """The next job worth running now, or None. `tried` holds when each job was last started by this loop.
    browser / api choose which kind of work to consider (the worker runs one lane of each when it has two)."""
    since, until = config.window(cfg)
    today = datetime.now().date().isoformat()
    now = time.time()
    ago = lambda job: now - tried.get(job, 0)
    in_window = "t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?"
    # 1. Instagram audio pages: linked audios without today's Reels count, at most every 3 hours. Checked before the
    #    Spotify plays so a long plays backlog can't keep it waiting all day (a run is under an hour).
    if browser and ago("instagram") > 3 * 3600:
        try:
            spotify_plays.find_chrome()
            conn = db.connect(str(config.DB_FILE))
            shorts_use.ensure(conn)
            n = len(instagram_audio.due(conn, today))
            conn.close()
            if n:
                return "instagram", f"{n} Instagram audios without today's Reels count"
        except SystemExit:
            pass
    # 1b. Spotify play counts: once a day for tracks without today's count (needs a browser on this machine).
    if browser and ago("spotify_plays") > 600:  # tracks still waiting: go again soon (each run ends when its browser is done)
        try:
            spotify_plays.find_chrome()
            conn = db.connect(str(config.DB_FILE))
            n = len(spotify_plays.due_ids(conn))
            conn.close()
            if n:
                return "spotify_plays", f"{n} Spotify tracks without today's play count"
        except SystemExit:
            pass
    if not api:
        return None
    # 1c. Daily cleanup, once a day after the backup hour (it returns disk space; nothing is lost).
    cl = cfg.get("cleanup") or {}
    if cl.get("enabled", True) and datetime.now().hour >= cl.get("after_hour", 3) and ago("cleanup") > 3600:
        c0 = db.connect(str(config.DB_FILE))
        try:
            due = not cleanup.done_today(c0)
        finally:
            c0.close()
        if due:
            return "cleanup", "daily cleanup not run yet today"
    # 1d. Spotify account 2: re-discover songs still without a Spotify link, on its own daily cap (hourly while due).
    s2 = config.read_secrets()
    if cfg["songs"].get("spotify_second_auto", True) and ago("spotify_second_account") > 3600 \
            and s2.get("SPOTIFY_CLIENT_ID_2") and s2.get("SPOTIFY_CLIENT_SECRET_2"):
        c1 = db.connect(str(config.DB_FILE))
        try:
            left = songs.second_account_budget(c1, cfg)[2]
            n = len(songs.second_account_due(c1, cfg, 1)) if left and not songs.second_account_blocked(c1) else 0
        finally:
            c1.close()
        if n:
            return "spotify_second_account", f"songs without a Spotify link to re-discover on account 2 ({left} searches left today)"
    # 2. JioSaavn: songs never searched (or due a retry) and tracks without today's plays.
    if ago("jiosaavn") > 600:
        recheck = (datetime.now().date() - timedelta(days=7)).isoformat()
        todo = _count(f"""SELECT count(*) FROM track t WHERE {in_window}
                          AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='jiosaavn')
                          AND coalesce((SELECT value FROM state WHERE key='js_lookup:' || t.id), '') < ?""", (since, until, recheck))
        fresh = (datetime.now().astimezone() - timedelta(hours=cfg["songs"].get("jiosaavn_refresh_hours", 3))).isoformat(timespec="seconds")
        unread = _count(f"SELECT count(*) FROM platform_item p JOIN track t ON t.id=p.track_id WHERE {jiosaavn.UNREAD}",
                        (since, today, fresh, today))  # the same tracks the reader reads
        if todo or unread:
            return "jiosaavn", f"{todo} songs to search, {unread} tracks to read"
    # 3. Songs: Spotify / iTunes / MusicBrainz lookups still pending, Deezer not read today, or new releases still
    #    without a Spotify link that the playlist / album / web-search routes haven't tried this week (every 30 min).
    if ago("songs") > 1800 and hours_since("songs") >= 0.5:
        pending = _count(f"""SELECT count(*) FROM track t WHERE {in_window} AND (t.release_status IS NULL OR t.release_status='unchecked')""", (since, until))
        dz = _count(f"""SELECT count(*) FROM track t LEFT JOIN release r ON r.id=t.release_id
                        WHERE (t.first_published >= :since OR r.release_date >= :since) AND {songs.DEEZER_DUE}""", {"since": since, "today": today})
        from . import spotify_links
        c2 = db.connect(str(config.DB_FILE))
        unlinked = len(spotify_links.due(c2, since, until, today, limit=1))
        c2.close()
        if pending or dz or unlinked:
            return "songs", f"{pending} songs awaiting lookups, {dz} Deezer ranks to read" + (", songs due a Spotify-link try" if unlinked else "")
    # 4. Shorts: official sounds without today's count (public pages, no quota; a run reads up to 300, ~15 min).
    if ago("shorts") > 1200:
        c3 = db.connect(str(config.DB_FILE))
        try:
            shorts_use.ensure(c3)
            n = len(shorts_use.due(c3, cfg, today))
        finally:
            c3.close()
        if n:
            return "shorts", f"{n} official sounds without today's Shorts count"
    # 5. YouTube Music: songs not yet searched, while today's search quota lasts (one try an hour).
    if ago("ytmusic") > 3600:
        from .youtube import _SET_ASIDE, parse_keys
        keys = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
        can_search = any(_SET_ASIDE.get((k, "search"), (0, ""))[0] <= now and _SET_ASIDE.get(k, (0, ""))[0] <= now for k in keys)
        c4 = db.connect(str(config.DB_FILE))
        try:
            todo = len(youtube_music._todo(c4, since, until, today, 1))  # never searched, or a miss whose wait is over
        finally:
            c4.close()
        if can_search and todo:
            return "ytmusic", "songs due a YouTube Music search, and the Topic-channel scan"
        # no API searches due: still list Topic channels, search the web for missing tracks (every 3 hours, polite) and read plays
        if hours_since("ytmusic") >= (3 if cfg["songs"].get("ytmusic_web_search", True) else 6):
            return "ytmusic", "Topic-channel scan and web search for songs still without a YouTube Music track"
    # 6. Nothing else due: refresh YouTube readings (a sync reads new uploads and videos in their first week, so it is
    #    cheap) so new uploads and views keep coming in between the 6-hourly pipelines; at most hourly.
    if ago("youtube_sync") > 3600 and hours_since("youtube_sync") >= 1:
        return "youtube_sync", "keeping YouTube readings and new uploads fresh between pipelines"
    return None


def hours_since(job):
    conn = db.connect(str(config.DB_FILE))
    row = conn.execute("SELECT max(started_at) FROM run_log WHERE job IN (?, 'pipeline') AND status NOT IN ('failed','interrupted')", (job,)).fetchone()
    conn.close()
    if not row[0]:
        return float("inf")
    return (datetime.now().astimezone() - datetime.fromisoformat(row[0])).total_seconds() / 3600


def start_worker(manager, browser_manager=None):
    """Between scheduled runs, keep the machine working on whatever is due (config worker.enabled).
    With a browser_manager there are two lanes: the browser job (Spotify pages, CPU) beside the API jobs (network),
    so a 2-core machine uses both cores."""
    def lane(mgr, w, tried, **kinds):
        last_end = mgr.last and mgr.last.get("ended_at")
        if mgr.current or (last_end and time.time() - last_end < w.get("pause_seconds", 60)):
            return
        due = _due_work(config.load(), tried, **kinds)
        if due:
            job, why = due
            tried[job] = time.time()
            try:
                mgr.start(job, trigger="worker")
                print(f"worker: started {job} ({why})", flush=True)
            except RuntimeError:
                pass

    def loop():
        tried = {}
        sampled = None
        checked = 0.0
        time.sleep(90)  # let the scheduler claim a due pipeline first
        while True:
            if time.time() - checked > 600:  # Developer alerts, every 10 minutes
                checked = time.time()
                try:
                    conn = db.connect(str(config.DB_FILE))
                    alerts.sync(conn)
                    conn.close()
                except Exception:
                    traceback.print_exc()
            hour = datetime.now().strftime("%Y-%m-%d %H")
            if hour != sampled:  # server health sample for the Developer dashboard, once an hour
                try:
                    conn = db.connect(str(config.DB_FILE))
                    sysstats.record(conn)
                    conn.close()
                    sampled = hour
                except Exception:
                    traceback.print_exc()
            try:
                w = config.load().get("worker", {})
                if w.get("enabled", True):
                    if browser_manager is None:
                        lane(manager, w, tried)
                    else:
                        lane(browser_manager, w, tried, api=False)
                        lane(manager, w, tried, browser=False)
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
                has_primary = s.get("SPOTIFY_CLIENT_ID") and s.get("SPOTIFY_CLIENT_SECRET")
                if has_primary:
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
