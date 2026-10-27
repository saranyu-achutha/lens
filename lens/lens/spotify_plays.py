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
           WHERE p.platform='spotify' AND p.review_state IN ('auto','approved') AND t.first_published >= ?
           ORDER BY p.platform_id""", (since,))]


# chrome-headless-shell (Chrome for Testing, browser only, ~280 MB) unpacked in the home folder; used on the server.
HEADLESS_SHELL = Path.home() / "chrome-headless-shell" / "chrome-headless-shell-linux64" / "chrome-headless-shell"


def find_chrome():
    """LENS_CHROME, else chrome-headless-shell in the home folder, else an installed Chrome/Chromium."""
    if os.environ.get("LENS_CHROME"):
        return os.environ["LENS_CHROME"]
    if HEADLESS_SHELL.exists():
        return str(HEADLESS_SHELL)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    sys.exit("No browser found: unpack chrome-headless-shell into ~/chrome-headless-shell, or install Chrome")


class _WS:
    """Minimal WebSocket client (text frames, client masking) for Chrome's DevTools protocol; stdlib only."""

    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=60)
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
        self.send({"id": self.n, "method": method, "params": params})
        while True:
            m = self.recv()
            if m.get("id") == self.n:
                return m.get("result", {})


READ_JS = """(() => { const p = document.querySelector('[data-testid="playcount"]');
  const t = document.querySelector('[data-testid="entityTitle"]');
  return JSON.stringify({plays: p ? p.innerText : null, loaded: !!t, title: document.title}); })()"""


def collect(ids, workers=10, day=None, log=print):
    """Read play counts with one headless Chrome and `workers` tabs reused page after page."""
    chrome = find_chrome()
    day = day or date.today().isoformat()
    path = out_dir() / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    res = json.loads(path.read_text()) if path.exists() else {"date": day, "plays": {}, "hidden": [], "missing": {}}
    res.setdefault("hidden", [])
    todo = [i for i in ids if i not in res["plays"] and i not in res["hidden"]]
    log(f"Spotify plays {day}: {len(ids)} tracks, {len(ids) - len(todo)} already read today, {len(todo)} to read, {workers} tabs")
    profile = tempfile.mkdtemp(prefix="lens_sp_")
    port = 9400 + os.getpid() % 500
    env = dict(os.environ, LD_LIBRARY_PATH=str(Path(chrome).parent))  # libgbm.so.1 ships next to chrome-headless-shell
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--mute-audio", "--lang=en-US",
                             "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                             f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    t0, done, lock = time.time(), 0, threading.Lock()
    queue = list(todo)

    def tab_worker(_):
        nonlocal done
        target = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT"), timeout=30))
        ws = _WS(target["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        while True:
            with lock:
                if not queue:
                    break
                tid = queue.pop(0)
            got = None
            for attempt in range(2):
                ws.call("Page.navigate", url=f"https://open.spotify.com/track/{tid}")
                loaded_at = None
                for tick in range(40 if attempt == 0 else 60):  # up to 20 s (30 s on the retry)
                    time.sleep(0.5)
                    r = ws.call("Runtime.evaluate", expression=READ_JS, returnByValue=True)
                    got = json.loads(r.get("result", {}).get("value") or "{}")
                    if got.get("plays"):
                        break
                    if got.get("loaded"):
                        loaded_at = loaded_at if loaded_at is not None else tick
                        if tick - loaded_at >= 6:  # page shown 3 s with no count: under 1,000 plays
                            break
                if got and (got.get("plays") or got.get("loaded")):
                    break
            with lock:
                done += 1
                digits = re.sub(r"\D", "", (got or {}).get("plays") or "")
                if digits:
                    res["plays"][tid] = int(digits)
                    res["missing"].pop(tid, None)
                elif got and got.get("loaded"):
                    res["hidden"].append(tid)  # page shown, no count: Spotify hides counts under 1,000 plays
                    res["missing"].pop(tid, None)
                else:
                    res["missing"][tid] = (got or {}).get("title", "")[:80]
                if done % 50 == 0 or done == len(todo):
                    res["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    path.write_text(json.dumps(res))
                    rate = done / max(time.time() - t0, 1)
                    log(f"  {done}/{len(todo)} read ({len(res['hidden'])} under 1,000, {len(res['missing'])} failed), ~{(len(todo) - done) / rate / 60:.0f} min left")
        ws.call("Page.navigate", url="about:blank")

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
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    res["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(res))
    log(f"saved {len(res['plays'])} play counts, {len(res['hidden'])} under 1,000, {len(res['missing'])} failed -> {path}  [{time.time() - t0:.0f}s]")
    return path


def import_file(conn, path, log=print):
    """Store a day's play counts as readings (metric 'plays') on every Spotify item with that track id."""
    res = json.loads(Path(path).read_text())
    day, ts = res["date"], res.get("captured_at") or db.now_iso()
    n = 0
    for tid, plays in res["plays"].items():
        for (item_id,) in conn.execute("SELECT id FROM platform_item WHERE platform='spotify' AND platform_id=?", (tid,)).fetchall():
            conn.execute("INSERT INTO reading (platform_item_id, metric, value, captured_at) VALUES (?, 'plays', ?, ?)", (item_id, plays, ts))
            conn.execute("""INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at) VALUES (?, 'plays', ?, ?, ?)
                            ON CONFLICT(platform_item_id, metric, capture_date) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at""",
                         (item_id, plays, day, ts))
            n += 1
    conn.commit()
    log(f"imported {len(res['plays'])} play counts for {day} ({n} Spotify items)")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lens.spotify_plays")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--since")
    c = sub.add_parser("collect")
    c.add_argument("ids_file")
    c.add_argument("--workers", type=int, default=10)
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
