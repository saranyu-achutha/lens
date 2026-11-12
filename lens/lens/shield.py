"""Anti-scraping shield (2026-11-12, user rule: the website must not be crawled or scraped by any bot or script, even
with a valid username; no data theft). Applies to client logins (the shared client login and every named client such
as timesnow) and to failed logins; the developer login is exempt (deploy script, health checks, tests).

Layers, each closing a different way of pulling the data out:
  1. Login lockout: 10 wrong passwords from one IP in 10 minutes (or 20 for one username) block that IP / username for
     15 minutes, so passwords cannot be guessed.
  2. Bots and scripts: a client request whose User-Agent is a tool (curl, wget, Python, Go, Java, Node, Postman,
     Scrapy, headless browsers, Selenium/Puppeteer/Playwright, crawlers, spiders) is refused.
  3. Browser session: the Console page hands out a signed session cookie (HttpOnly, SameSite=Strict, 12 h) bound to
     the login and the browser; client data feeds need it (plus the Console's own request header). A script with a
     stolen username and password alone gets nothing; a stale page reloads itself and gets a fresh cookie.
  4. Rate limits: per login 90 data requests a minute and 1,200 an hour, per IP 150 a minute; above that the login
     (and IP) is blocked for 15 minutes. A person clicking through pages never comes close.
  5. Daily volume caps per login: 300 different songs opened (song details, daily history, comparisons) and 3,000
     data requests (the status poll not counted). Bulk copying of the catalogue stops there.
  6. Every block is logged (data/shield.db, its own file so it can never lock the main database) and raises an alert
     on the Developer dashboard naming the login and IP.
Limits are in memory per process (a restart clears the counters, not the log). Settings: config.json -> shield.
"""
import hashlib
import hmac
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from . import config

BOT_UA = re.compile(r"curl|wget|python|requests|httpx|aiohttp|urllib|scrapy|go-http|golang|java/|okhttp|apache-httpclient|libwww|"
                    r"perl|ruby|php|node-fetch|undici|axios|got \(|postman|insomnia|httpie|powershell|winhttp|headless|phantom|"
                    r"selenium|webdriver|puppeteer|playwright|crawl|spider|scrap|httrack|nutch|wkhtml|\bbot\b|bot/|-bot|_bot", re.I)
DEFAULTS = {"per_minute": 90, "per_hour": 1200, "ip_per_minute": 150, "block_minutes": 15, "songs_per_day": 300,
            "requests_per_day": 3000, "login_fail_ip": 10, "login_fail_user": 20, "login_window_minutes": 10,
            "session_hours": 12}
SONG_PATHS = {"/api/track", "/api/song/compare", "/api/shorts", "/api/insights/song", "/api/meta/detail"}
UNCOUNTED = {"/api/status"}
COOKIE = "ml_sess"

_LOCK = threading.Lock()
_hits = defaultdict(deque)          # ("u", user) / ("ip", ip) -> request times (last hour)
_blocked = {}                       # key -> unblock time
_fails = defaultdict(deque)         # ("ip", ip) / ("user", user) -> failed login times
_day = {"day": None, "req": defaultdict(int), "songs": defaultdict(set)}
_SECRET = None


def settings():
    try:
        s = (config.load().get("shield") or {})
    except Exception:
        s = {}
    return {k: s.get(k, v) for k, v in DEFAULTS.items()}


def _secret():
    """The cookie signing key: SHIELD_SECRET from .env if set, else derived from the existing login secrets (stable
    across restarts, nothing written to .env; changing a password ends every session, which is what one wants)."""
    global _SECRET
    if not _SECRET:
        env = config.read_secrets()
        s = (env.get("SHIELD_SECRET") or "").strip() or "|".join(
            env.get(k) or "" for k in ("STUDIO_PASSWORD", "DEVELOPER_PASSWORD", "DEVELOPER_USERNAME", "CLIENT_LOGINS"))
        _SECRET = hashlib.sha256(("market-lens-shield|" + s).encode()).digest()
    return _SECRET


# ---------- the event log (own file) ----------

def _db():
    c = sqlite3.connect(str(config.DB_FILE.parent / "shield.db"), timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS shield_event (at TEXT, kind TEXT, user TEXT, ip TEXT, path TEXT, detail TEXT)")
    return c


def log_event(kind, user, ip, path="", detail=""):
    try:
        c = _db()
        c.execute("INSERT INTO shield_event VALUES (?,?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, user or "", ip or "", path[:200], detail[:300]))
        c.commit()
        c.close()
    except Exception:
        pass  # logging must never break a request


def events(hours=24):
    try:
        c = _db()
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = c.execute("SELECT kind, user, ip, count(*), max(at) FROM shield_event WHERE at >= ? GROUP BY 1,2,3 ORDER BY 4 DESC",
                         (since,)).fetchall()
        c.close()
        return rows
    except Exception:
        return []


def alert_conditions(conn=None):
    """For lens.alerts: blocked scraping attempts and login lockouts in the last 24 hours."""
    rows = events(24)
    if not rows:
        return []
    total = sum(r[3] for r in rows)
    names = {"bot": "bot/script", "session": "no browser session", "rate": "too fast", "volume": "daily cap",
             "login": "wrong passwords", "lockout": "locked out"}
    top = "; ".join(f"{names.get(k, k)}: {u or '-'} @ {ip} ×{n}" for k, u, ip, n, _ in rows[:5])
    serious = any(k in ("rate", "volume", "lockout") for k, *_ in rows)
    return [{"key": "shield", "severity": "critical" if serious else "warning", "action": "open:dev",
             "title": f"Scraping protection blocked {total} request{'s' if total != 1 else ''} in the last 24 hours",
             "detail": f"{top}. Blocks end by themselves; repeated ones from a login may mean its password is being misused "
                       f"(reset it: python3 -m lens.logins reset <user>)."}]


# ---------- checks ----------

def client_ip(headers, fallback):
    """The caller's address: the last X-Forwarded-For entry (added by nginx; earlier entries can be forged)."""
    xff = [p.strip() for p in (headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
    return xff[-1] if xff else (headers.get("X-Real-IP") or fallback)


def login_blocked(ip, user):
    now = time.time()
    with _LOCK:
        for key in (("ip", ip), ("user", user)):
            if _blocked.get(("login",) + key, 0) > now:
                return True
    return False


def login_failed(ip, user):
    s, now = settings(), time.time()
    win = 60 * s["login_window_minutes"]
    with _LOCK:
        hit = False
        for key, limit in ((("ip", ip), s["login_fail_ip"]), (("user", user or "-"), s["login_fail_user"])):
            q = _fails[key]
            q.append(now)
            while q and q[0] < now - win:
                q.popleft()
            if len(q) >= limit:
                _blocked[("login",) + key] = now + 60 * s["block_minutes"]
                hit = True
    log_event("lockout" if hit else "login", user, ip, "", "locked for %d min" % s["block_minutes"] if hit else "wrong password")


def is_bot(ua):
    return not ua or bool(BOT_UA.search(ua))


def _ua_hash(ua):
    return hashlib.sha256((ua or "").encode()).hexdigest()[:16]


def make_cookie(user, ua, https):
    exp = int(time.time() + 3600 * settings()["session_hours"])
    body = f"{user}|{exp}|{_ua_hash(ua)}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    val = f"{user}|{exp}|{sig}".replace(";", "")
    return f"{COOKIE}={val}; Path=/; Max-Age={3600 * settings()['session_hours']}; HttpOnly; SameSite=Strict" + ("; Secure" if https else "")


def session_ok(cookie_header, user, ua):
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            try:
                u, exp, sig = v.rsplit("|", 2)
                if u != user or int(exp) < time.time():
                    return False
                want = hmac.new(_secret(), f"{u}|{exp}|{_ua_hash(ua)}".encode(), hashlib.sha256).hexdigest()[:32]
                return hmac.compare_digest(sig, want)
            except ValueError:
                return False
    return False


def allow(user, ip, path, qs):
    """None when a client data request may go ahead, else (status, message). Counts it."""
    s, now = settings(), time.time()
    today = datetime.now(timezone.utc).date().isoformat()
    with _LOCK:
        for key in (("u", user), ("ip", ip)):
            if _blocked.get(key, 0) > now:
                return 429, "Too many requests from this login. Please wait a few minutes and try again."
        if _day["day"] != today:
            _day.update(day=today, req=defaultdict(int), songs=defaultdict(set))
        if path in UNCOUNTED:
            return None
        for key, per_min in ((("u", user), s["per_minute"]), (("ip", ip), s["ip_per_minute"])):
            q = _hits[key]
            q.append(now)
            while q and q[0] < now - 3600:
                q.popleft()
            last_min = sum(1 for t in q if t >= now - 60)
            if last_min > per_min or (key[0] == "u" and len(q) > s["per_hour"]):
                _blocked[("u", user)] = _blocked[("ip", ip)] = now + 60 * s["block_minutes"]
                reason = f"{last_min} requests in a minute" if last_min > per_min else f"{len(q)} requests in an hour"
                break
        else:
            reason = None
        if reason is None:
            _day["req"][user] += 1
            if _day["req"][user] > s["requests_per_day"]:
                reason, kind = f"over {s['requests_per_day']} requests today", "volume"
            else:
                song = qs.get("id") or qs.get("song") or qs.get("track") or qs.get("a") or ""
                if song and (path in SONG_PATHS or path.startswith("/api/daily")):
                    _day["songs"][user].add(f"{path}:{song}" if path == "/api/meta/detail" else str(song))
                    if len(_day["songs"][user]) > s["songs_per_day"]:
                        reason, kind = f"over {s['songs_per_day']} songs opened today", "volume"
            if reason is None:
                return None
        else:
            kind = "rate"
    log_event(kind, user, ip, path, reason)
    if kind == "volume":
        return 429, "This login has reached today's viewing limit. It resets at midnight (UTC)."
    return 429, "Too many requests from this login. Please wait a few minutes and try again."
