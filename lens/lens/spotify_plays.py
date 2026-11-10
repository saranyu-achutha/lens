"""Spotify play counts from the public track pages (open.spotify.com/track/<id>), read once a day.

The play count is shown to any visitor but drawn by the page's JavaScript, so each page is rendered with
headless Chrome (`--dump-dom`) and the number in data-testid="playcount" is read. About 15 s a page; pages
are rendered in parallel. The server has 1 CPU, so collection runs on a machine with Chrome (the laptop)
and only the small JSON result is imported on the server:

  python3 -m lens.spotify_plays export  > ids.json                 # server: linked tracks of the period
  python3 -m lens.spotify_plays collect ids.json [--workers 8]     # laptop: -> data/spotify_plays/<date>.json
  python3 -m lens.spotify_plays import data/spotify_plays/<date>.json   # server: store as daily readings

Readings go to snapshot/reading with metric 'plays' on the Spotify platform_item, like YouTube views.
A rerun on the same day skips tracks already read. See spotify_plays_daily.sh for the daily routine.
"""
import argparse
import base64
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, db



def out_dir():
    return config.DB_FILE.parent / "spotify_plays"


def export_ids(conn, since=None):
    """Spotify track ids linked to songs of the period (one entry per id)."""
    since = since or config.window(config.load())[0]
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT p.platform_id FROM platform_item p JOIN track t ON t.id=p.track_id
           WHERE p.platform='spotify' AND p.review_state IN ('auto','approved') AND t.first_published >= ? AND t.old_release IS NULL
           ORDER BY p.platform_id""", (since,))]


# chrome-headless-shell (Chrome for Testing, browser only, ~280 MB) unpacked in the home folder; used on the server.
HEADLESS_SHELL = Path.home() / "chrome-headless-shell" / "chrome-headless-shell-linux64" / "chrome-headless-shell"


def find_chrome():
    """LENS_CHROME, else a headless-shell build (home folder or Playwright's), else an installed Chrome/Chromium."""
    if os.environ.get("LENS_CHROME"):
        return os.environ["LENS_CHROME"]
    if HEADLESS_SHELL.exists():
        return str(HEADLESS_SHELL)
    # Playwright's headless shell, if installed (much lighter than full Chrome: ~430 MB for 3 tabs).
    pw = Path.home() / ".cache" / "ms-playwright"  # older builds: chrome-linux/headless_shell; newer (and ARM): chrome-headless-shell-linux-*/chrome-headless-shell
    for p in sorted([*pw.glob("chromium_headless_shell-*/chrome-*/headless_shell"), *pw.glob("chromium_headless_shell-*/chrome-*/chrome-headless-shell")], reverse=True):
        return str(p)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    sys.exit("No browser found: unpack chrome-headless-shell into ~/chrome-headless-shell, or install Chrome")


class _WS:
    """Minimal WebSocket client (text frames, client masking) for Chrome's DevTools protocol; stdlib only."""

    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=45)  # a frozen tab fails fast instead of hanging the run
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("DevTools handshake failed")
        self.n = 0

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("DevTools connection closed")
            buf += chunk
        return buf

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._recv_exact(2)
            n = b2 & 0x7F
            n = struct.unpack(">H", self._recv_exact(2))[0] if n == 126 else struct.unpack(">Q", self._recv_exact(8))[0] if n == 127 else n
            payload = self._recv_exact(n)
            if b1 & 0x0F == 0x9:  # ping: ignore
                continue
            msg += payload
            if b1 & 0x80:
                return json.loads(msg)

    def call(self, method, **params):
        self.n += 1
        want = self.n
        self.send({"id": want, "method": method, "params": params})
        while True:
            m = self.recv()
            if m.get("id") == want:
                return m.get("result", {})


# The page's play count, three ways (lens/heal.py): 1) a learned marker (a data-testid that held the count when the usual
# one was gone), 2) the usual data-testid="playcount", 3) the header line under the title ("Album • 2026 • 5:20 •
# 263,551,827"): the last number after a "•" separator that is not a year or a duration. "loaded" = the track's title is
# drawn (its usual marker, or the h1 matching the page's og:title), "notfound" = Spotify's not-found page, so a changed or
# half-drawn page is never mistaken for a hidden count.
READ_JS_TEMPLATE = """(() => { const HINT = __HINT__;
  const isNum = (s) => /^\\d{1,3}(,\\d{3})+$|^\\d{4,}$/.test(s) && !/^(19|20)\\d\\d$/.test(s);
  const txt = (e) => ((e && e.innerText) || "").trim();
  let plays = null, how = null, marker = null;
  const byTid = (tid) => { const e = tid && document.querySelector(`[data-testid="${tid}"]`); return e && isNum(txt(e)) ? txt(e) : null; };
  if (HINT && HINT !== "playcount") { plays = byTid(HINT); if (plays) { how = "learned"; marker = HINT; } }
  if (!plays) { plays = byTid("playcount"); if (plays) { how = "testid"; marker = "playcount"; } }
  const m = (n) => { const e = document.querySelector(`meta[name="${n}"], meta[property="${n}"]`); return e ? e.content : null; };
  // the track's own title: the usual marker, else the h1 whose text is the page's own title (never another heading)
  const name = (m("og:title") || document.title.split(" - ")[0] || "").trim().toLowerCase();
  const t = document.querySelector('[data-testid="entityTitle"]') ||
            [...document.querySelectorAll("h1")].find((h) => name && txt(h).toLowerCase() === name) || null;
  if (!plays && t) {
    // only the nearest block holding the title and the "•" separators: never further up (the page also lists other
    // tracks' counts, and a hidden count must give no number)
    const isSep = (e) => e.dataset.separator || txt(e) === "\u2022";
    let box = t.parentElement;
    for (let i = 0; i < 6 && box && ![...box.querySelectorAll("span")].some(isSep); i++) box = box.parentElement;
    if (box && [...box.querySelectorAll("span")].some(isSep)) {
      const leaves = [...box.querySelectorAll("span, div")].filter((e) => !e.children.length);
      let afterSep = false, found = null;
      for (const e of leaves) { if (isSep(e)) { afterSep = true; continue; } if (afterSep && isNum(txt(e))) found = e; }
      if (found) { plays = txt(found); how = "header-row"; marker = found.dataset.testid || null; }
    }
  }
  const all = (n) => [...document.querySelectorAll(`meta[name="${n}"]`)].map((e) => e.content);
  const head = (document.title + " " + txt(document.querySelector("main")).slice(0, 400)).toLowerCase();
  // loaded = the title is drawn (og:title is in the page before Spotify draws it: counting from then would call a
  // count that is still coming "hidden")
  return JSON.stringify({plays, how, marker, loaded: !!(t && txt(t)),
    notfound: /page not found|couldn.t find|isn.t available|not available in your country/.test(head), title: document.title, path: location.pathname,
    meta: {name: m("og:title"), release_date: m("music:release_date"), duration_s: m("music:duration"), album: m("music:album"),
           artists: all("music:musician_description"), summary: m("og:description")}}); })()"""


def read_js(hint=None):
    return READ_JS_TEMPLATE.replace("__HINT__", json.dumps(hint))


READ_JS = read_js()  # without a learned marker (kept for callers that read one page)


# Images, audio and video are not needed for the play count (blocking styles or scripts breaks Spotify's page).
# Trackers, cookie banner and fonts aren't needed either (~15% less work per page).
BLOCK = ["*.jpg*", "*.jpeg*", "*.png*", "*.webp*", "*.gif*", "*.mp3*", "*.mp4*", "*.m4a*", "*i.scdn.co/image*", "*mosaic.scdn.co*",
         "*google-analytics.com*", "*googletagmanager.com*", "*doubleclick.net*", "*sentry.io*", "*cookielaw.org*", "*onetrust*", "*hotjar*",
         "*analytics.tiktok.com*", "*pinterest*", "*facebook.net*", "*redditstatic*", "*snapchat*", "*.woff2*", "*.woff*", "*.ttf*"]
TAB_PAGES = 1  # a fresh tab for every track: a second Spotify page in the same tab hangs (since late Oct 2026)
BATCH = 150      # tracks per browser start in the Console job


def _kill_group(proc):
    """Stop Chrome and every process it started (renderers, GPU, zygotes): no leftovers after a stop."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def collect(ids, workers=3, day=None, log=print, should_stop=lambda: False, folder=None, hint=None, retry_hidden=False):
    """Read play counts with one light headless Chrome and `workers` tabs (low CPU priority, images etc. blocked)."""
    chrome = find_chrome()
    day = day or date.today().isoformat()
    path = Path(folder or out_dir()) / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    res = json.loads(path.read_text()) if path.exists() else {"date": day, "plays": {}, "hidden": [], "missing": {}}
    res.setdefault("hidden", [])
    res.setdefault("how", {})      # strategy -> values found that way (lens/heal.py)
    res.setdefault("markers", {})  # data-testid that held the count -> times
    js = read_js(hint)
    first_empty = {"dom": None}
    if retry_hidden:  # the guard held today's "hidden" outcomes (the page may have changed): read those tracks again
        res["hidden"] = [t for t in res["hidden"] if t not in set(ids)]
    todo = [i for i in ids if i not in res["plays"] and i not in res["hidden"]]
    log(f"Spotify plays {day}: {len(ids)} tracks, {len(ids) - len(todo)} already read today, {len(todo)} to read, {workers} tabs")
    if not todo:
        return path
    profile = tempfile.mkdtemp(prefix="lens_sp_")
    port = 9400 + os.getpid() % 500
    env = dict(os.environ, LD_LIBRARY_PATH=str(Path(chrome).parent))  # libgbm.so.1 ships next to chrome-headless-shell
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--mute-audio", "--lang=en-US",
                             "--blink-settings=imagesEnabled=false", "--disable-extensions", "--disable-background-networking",
                             "--disable-component-update", "--disable-default-apps", "--disable-sync", "--no-pings",
                             f"--renderer-process-limit={workers + 1}", "--js-flags=--max-old-space-size=384",
                             "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                             f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                            preexec_fn=lambda: os.nice(19))  # the browser yields the CPU to everything else
    stopping = {"sig": None}

    def on_signal(sig, _frame):  # a kill of the reader also closes its browser
        stopping["sig"] = sig
        _kill_group(proc)
        raise SystemExit(f"stopped by signal {sig}; {len(res['plays'])} counts saved, a rerun continues")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, on_signal)
        except ValueError:  # not the main thread (e.g. inside the Console): the finally block still cleans up
            pass
    t0, done, lock = time.time(), 0, threading.Lock()
    queue = list(todo)

    def new_tab():
        target = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT"), timeout=30))
        ws = _WS(target["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Network.enable")
        ws.call("Network.setBlockedURLs", urls=BLOCK)
        return target["id"], ws

    def close_tab(tab_id, ws):
        try:
            ws.sock.close()
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tab_id}", timeout=10)
        except OSError:
            pass

    def read_one(ws, tid):
        """Open one track page and wait for its play count (or for the page to show none: under 1,000 plays)."""
        got = None
        for attempt in range(2):
            ws.call("Page.navigate", url="about:blank")
            ws.call("Page.navigate", url=f"https://open.spotify.com/track/{tid}")
            loaded_at = None
            for tick in range(40 if attempt == 0 else 60):  # up to 20 s (30 s on the retry)
                time.sleep(0.5)
                r = ws.call("Runtime.evaluate", expression=js, returnByValue=True)
                got = json.loads(r.get("result", {}).get("value") or "{}")
                if not (got.get("path") or "").endswith(tid):  # still the previous page: its count is not this track's
                    got = None
                    continue
                if got.get("plays"):
                    break
                if got.get("loaded"):
                    loaded_at = loaded_at if loaded_at is not None else tick
                    if tick - loaded_at >= 6:  # page shown 3 s with no count: under 1,000 plays
                        break
            if got and (got.get("plays") or got.get("loaded") or got.get("notfound")):
                break
        if got and not got.get("plays") and first_empty["dom"] is None:  # one empty page kept, in case the layout changed
            try:
                r = ws.call("Runtime.evaluate", expression="document.documentElement.outerHTML", returnByValue=True)
                first_empty["dom"] = r.get("result", {}).get("value") or ""
            except (TimeoutError, ConnectionError, OSError):
                pass
        return got

    def tab_worker(_):
        nonlocal done
        tab_id, ws = new_tab()
        pages, fails_in_row = 0, 0
        while not stopping["sig"] and not should_stop():
            with lock:
                if not queue:
                    break
                tid = queue.pop(0)
            if pages and pages % TAB_PAGES == 0:  # recycle the tab
                close_tab(tab_id, ws)
                tab_id, ws = new_tab()
            pages += 1
            got = None
            try:
                got = read_one(ws, tid)
                fails_in_row = 0
            except (TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as e:  # a slow or broken page: skip it, fresh tab
                got = {"title": f"page error: {type(e).__name__}"}
                fails_in_row += 1
                close_tab(tab_id, ws)
                if fails_in_row >= 10:  # something is really wrong (blocked, browser gone): stop, progress is kept
                    log(f"  10 pages in a row failed ({type(e).__name__}); stopping this tab")
                    with lock:
                        queue.insert(0, tid)
                    return
                tab_id, ws = new_tab()
            with lock:
                done += 1
                digits = re.sub(r"\D", "", (got or {}).get("plays") or "")
                if (got or {}).get("meta", {}).get("release_date"):
                    res.setdefault("meta", {})[tid] = got["meta"]
                if digits:
                    res["plays"][tid] = int(digits)
                    res["missing"].pop(tid, None)
                    res["how"][got.get("how") or "testid"] = res["how"].get(got.get("how") or "testid", 0) + 1
                    res.setdefault("how_by", {})[tid] = got.get("how") or "testid"
                    if got.get("marker"):
                        res["markers"][got["marker"]] = res["markers"].get(got["marker"], 0) + 1
                elif got and got.get("notfound"):
                    res["missing"][tid] = "gone: " + (got.get("title") or "")[:70]  # Spotify says the track is not there
                elif got and got.get("loaded"):
                    res["hidden"].append(tid)  # page shown, no count: hidden under 1,000 plays (trusted only if the guard passes)
                    res["missing"].pop(tid, None)
                else:  # nothing shown: a slow page or a changed layout, never "gone" (retried)
                    res["missing"][tid] = "page error: not shown " + (got or {}).get("title", "")[:60]
                if done % 50 == 0 or done == len(todo):
                    res["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    path.write_text(json.dumps(res))
                    rate = done / max(time.time() - t0, 1)
                    log(f"  {done}/{len(todo)} read ({len(res['hidden'])} under 1,000, {len(res['missing'])} failed), ~{(len(todo) - done) / rate / 60:.0f} min left")
        try:
            ws.call("Page.navigate", url="about:blank")
        except (TimeoutError, ConnectionError, OSError):
            pass

    try:
        for _ in range(60):  # wait for Chrome's DevTools endpoint
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(tab_worker, range(workers)))
    finally:
        _kill_group(proc)
        shutil.rmtree(profile, ignore_errors=True)
        res["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps(res))
        if first_empty["dom"] and len(res["hidden"]) + len(res["missing"]) > len(res["plays"]):  # mostly empty: keep a sample
            from . import heal
            heal.sample("spotify_plays", first_empty["dom"], day)
    log(f"saved {len(res['plays'])} play counts, {len(res['hidden'])} under 1,000, {len(res['missing'])} failed -> {path}  [{time.time() - t0:.0f}s]")
    return path


def import_file(conn, path, log=print, only=None):
    """Store a day's play counts as readings (metric 'plays') on every Spotify item with that track id.
    `only`: the track ids of this batch (a day's file grows batch by batch; each value is stored once).
    Self-healing checks (lens/heal.py): a count that fell or jumped against the item's last reading is kept apart
    (reading_suspect), and "hidden" outcomes are trusted only when tracks known to show a count still showed one."""
    from . import freshness, heal
    res = json.loads(Path(path).read_text())
    day, ts = res["date"], res.get("captured_at") or db.now_iso()
    conn.execute("PRAGMA busy_timeout = 120000")  # wait for a running job's write to finish instead of failing
    heal.ensure(conn)
    pick = (lambda tid: tid in only) if only is not None else (lambda tid: True)
    plays = {t: v for t, v in res["plays"].items() if pick(t)}
    hidden = [t for t in res.get("hidden", []) if pick(t)]
    items = {}
    for tid in list(plays) + hidden:
        items[tid] = [r[0] for r in conn.execute("SELECT id FROM platform_item WHERE platform='spotify' AND platform_id=?", (tid,))]
    n = held_values = 0
    for tid, value in plays.items():
        for item_id in items[tid]:
            last = heal.last_snapshot(conn, item_id, "plays", day)
            verdict = heal.check_value(last, value, "plays")
            if verdict != "ok":  # not stored: a misread (e.g. another track's count) must not become a reading
                heal.suspect(conn, item_id, "plays", value, last, verdict, ts)
                held_values += 1
                continue
            conn.execute("INSERT INTO reading (platform_item_id, metric, value, captured_at) VALUES (?, 'plays', ?, ?)", (item_id, value, ts))
            conn.execute("""INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at) VALUES (?, 'plays', ?, ?, ?)
                            ON CONFLICT(platform_item_id, metric, capture_date) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                         (item_id, value, day, ts))
            n += 1
    # The page's own details (release date, duration): kept on the Spotify item, so release dates come from the
    # public page even for links that never went through the Spotify API.
    for tid, meta in res.get("meta", {}).items():
        if not pick(tid):
            continue
        dur = int(meta["duration_s"]) * 1000 if str(meta.get("duration_s") or "").isdigit() else None
        conn.execute("""UPDATE platform_item SET published_at=COALESCE(NULLIF(published_at, ''), ?), duration_ms=COALESCE(duration_ms, ?)
                        WHERE platform='spotify' AND platform_id=?""", (meta.get("release_date"), dur, tid))
    # Guard: tracks that showed a count on their last reading (5,000+) and now show none mean the page changed, not
    # that their counts were hidden; a jump in the share of hidden pages means the same.
    known = [t for t in list(plays) + hidden if any((heal.last_snapshot(conn, i, "plays", day) or 0) >= 5000 for i in items[t])]
    known_empty = sum(1 for t in known if t in hidden)
    held = heal.known_guard(conn, "spotify_plays", len(known), known_empty) or \
        heal.mass_guard(conn, "spotify_plays", "hidden", len(hidden), len(plays) + len(hidden))
    if held:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"heal_held:spotify_plays:{day}", ts))
        log(f"  GUARD: {len(hidden)} pages showed no count ({known_empty} of {len(known)} tracks that had one); the page may have "
            f"changed, so none is marked as read (they are retried). See Missed readings → Page readers.")
    # reads that gave no number still count as read (the 24-hour check, lens/freshness.py): hidden counts, pages gone
    marks = ([] if held else [(t, "hidden") for t in hidden]) + \
        [(t, "gone") for t, why in (res.get("missing") or {}).items() if pick(t) and str(why).startswith("gone")]
    for tid, outcome in marks:
        for (item_id,) in conn.execute("SELECT id FROM platform_item WHERE platform='spotify' AND platform_id=?", (tid,)).fetchall():
            freshness.mark(conn, item_id, "plays", outcome, ts)
    # Tracks found under 1,000 plays (Spotify hides the count): drop any value stored for them that day,
    # so an earlier wrong reading cannot stay (not while the guard holds the "hidden" outcomes).
    gone = 0
    for tid in ([] if held else hidden):
        gone += conn.execute("""DELETE FROM snapshot WHERE metric='plays' AND capture_date=? AND platform_item_id IN
                                (SELECT id FROM platform_item WHERE platform='spotify' AND platform_id=?)""", (day, tid)).rowcount
    # strategy counts and the learned marker (only for the values of this batch)
    if only is not None:
        how = {}
        for t in plays:
            k = (res.get("how_by") or {}).get(t) or "testid"
            how[k] = how.get(k, 0) + 1
    else:
        how = res.get("how") or {"testid": len(plays)}
    for strategy, k in how.items():
        heal.record(conn, "spotify_plays", strategy, k, day)
    failed = sum(1 for t, why in (res.get("missing") or {}).items() if pick(t) and str(why).startswith("page error"))
    if failed:
        heal.record(conn, "spotify_plays", "fail", failed, day)
    markers = {m: k for m, k in (res.get("markers") or {}).items() if m != "playcount"}
    if markers and not how.get("testid"):  # the usual marker found nothing: keep the one the fallback found
        learned = heal.learn(conn, "spotify_plays", markers)
        if learned:
            log(f"  learned the page's new play-count marker: data-testid=\"{learned}\" (tried first from now on)")
    conn.commit()
    log(f"imported {len(plays)} play counts for {day} ({n} Spotify items); {gone} hidden-count values removed"
        + (f"; {held_values} values held as suspicious" if held_values else ""))
    return n


def due_ids(conn, day=None):
    """Linked tracks of the period without a play count today (and not found hidden/failed in today's server file)."""
    day = day or date.today().isoformat()
    read = {r[0] for r in conn.execute("""SELECT p.platform_id FROM snapshot s JOIN platform_item p ON p.id=s.platform_item_id
                                         WHERE p.platform='spotify' AND s.metric='plays' AND s.capture_date=?""", (day,))}
    skip = set()
    # done for today: hidden counts (under 1,000) and pages Spotify says are gone, found by the server or by a helper
    # machine whose results were imported (laptop_<day>.json); timeouts are tried again
    held = conn.execute("SELECT 1 FROM state WHERE key=?", (f"heal_held:spotify_plays:{day}",)).fetchone()
    for f in (out_dir() / "server" / f"{day}.json", out_dir() / f"laptop_{day}.json"):
        if f.exists():
            r = json.loads(f.read_text())
            # hidden counts are done for today unless the guard held them (the page may have changed: read them again)
            skip |= (set() if held else set(r.get("hidden", []))) | {t for t, why in r.get("missing", {}).items() if str(why).startswith("gone")}
    last = dict(conn.execute("""SELECT p.platform_id, max(s.captured_at) FROM platform_item p JOIN snapshot s ON s.platform_item_id=p.id
                                AND s.metric='plays' WHERE p.platform='spotify' GROUP BY 1""").fetchall())
    # oldest read first (never read before everything), so a long day still reads every track within 24 hours
    return sorted((i for i in export_ids(conn) if i not in read and i not in skip), key=lambda i: last.get(i) or "")


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    """Console job: read today's missing play counts on this machine (light headless browser), then import."""
    try:
        find_chrome()
    except SystemExit as e:
        return {"status": "ok", "stop_reason": str(e)}
    todo = due_ids(conn)
    if not todo:
        log("Spotify plays: every linked track already read today")
        return {"status": "ok", "spotify_plays_read": 0}
    # Batches of BATCH tracks, each with a fresh browser: a browser that freezes costs one batch, not the rest of the day.
    conn.execute("PRAGMA busy_timeout = 120000")
    n = 0
    for i in range(0, len(todo), BATCH):
        if should_stop():
            break
        from . import heal
        batch = todo[i:i + BATCH]
        path = collect(batch, workers=cfg["songs"].get("spotify_plays_tabs", 2), log=log, should_stop=should_stop,
                       folder=out_dir() / "server", hint=heal.hint(conn, "spotify_plays"),
                       retry_hidden=bool(conn.execute("SELECT 1 FROM state WHERE key=?", (f"heal_held:spotify_plays:{date.today().isoformat()}",)).fetchone()))
        n += import_file(conn, path, log=log, only=set(batch))
    return {"status": "ok", "spotify_plays_read": n}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lens.spotify_plays")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--since")
    c = sub.add_parser("collect")
    c.add_argument("ids_file")
    c.add_argument("--workers", type=int, default=3)
    i = sub.add_parser("import")
    i.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        print(json.dumps(export_ids(db.connect(str(config.DB_FILE)), a.since)))
    elif a.cmd == "collect":
        collect(json.loads(Path(a.ids_file).read_text()), a.workers)
    else:
        import_file(db.connect(str(config.DB_FILE)), a.file)


if __name__ == "__main__":
    main()
