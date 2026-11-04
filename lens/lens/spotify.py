"""Minimal Spotify Web API client (client-credentials flow), stdlib only."""
import base64
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
MAX_SEARCH_OFFSET = 1000  # Spotify rejects offset + limit > 1000


MAX_RETRY_WAIT = 120  # seconds; a longer Retry-After means a quota block, not a short burst limit


class SpotifyError(Exception):
    def __init__(self, status, url, body, retry_after=None):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after  # seconds, for 429s

    @property
    def quota_exceeded(self):
        return self.status == 429 and (self.retry_after or 0) > MAX_RETRY_WAIT


class BudgetExhausted(Exception):
    """The per-run call budget is used up; the run stops cleanly and the next run continues."""


class DailyLimitReached(BudgetExhausted, RuntimeError):
    """An account has made its daily limit of calls in the last 24 h; jobs stop cleanly like a spent run budget
    (and a Console request gets a plain error)."""


# ---- Calls per account, rolling 24 h (songs.spotify_primary_daily_limit / spotify_second_daily_limit).
# Kept in its own small database so counting never waits on, or locks, the main database a job is writing to.
_LEDGER_LOCK = threading.Lock()


def _ledger():
    from . import config
    path = config.DB_FILE.parent / "spotify_calls.db"
    fresh = not path.exists()
    c = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS call_hour (account TEXT NOT NULL, hour TEXT NOT NULL, "
              "calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account, hour))")
    if fresh:
        _seed(c, config.DB_FILE)
    return c


def _hour(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H")


def _seed(ledger, db_file):
    """First use: start from the calls already made in the last 24 h (run log, and account 2's per-search marks),
    so a new counter does not hand out a fresh day on top of calls Spotify has already counted."""
    try:
        main = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        counts = {}  # (account, hour) -> calls, each in the hour it was made so it frees up on time

        def add(account, at, n):
            hour = _hour(datetime.fromisoformat(at).astimezone(timezone.utc))
            counts[(account, hour)] = counts.get((account, hour), 0) + n
        for job, at, st in main.execute("SELECT job, started_at, stats FROM run_log WHERE started_at >= ?", (since,)):
            if job == "spotify_second_account":
                continue
            x = json.loads(st or "{}")
            n = sum(int(x.get(k) or 0) for k in ("spotify_searches", "spotify_artist_calls", "api_calls")
                    if isinstance(x.get(k), (int, float)))
            if n:
                add("primary", at, n)
        for (at,) in main.execute("SELECT value FROM state WHERE key LIKE 'spotify_second_call:%' AND value >= ?", (since,)):
            add("second", at, 1)
    except (sqlite3.Error, ValueError):
        return
    finally:
        main.close()
    for (account, hour), n in counts.items():
        ledger.execute("INSERT OR REPLACE INTO call_hour VALUES (?, ?, ?)", (account, hour, n))


def calls_last_24h(account, ledger=None):
    """Calls the account made in the last 24 hours (whole hours: this hour and the 23 before it)."""
    c = ledger or _ledger()
    try:
        since = _hour(datetime.now(timezone.utc) - timedelta(hours=23))
        return c.execute("SELECT coalesce(sum(calls), 0) FROM call_hour WHERE account=? AND hour >= ?",
                         (account, since)).fetchone()[0]
    finally:
        if ledger is None:
            c.close()


def _count_call(ledger, account):
    ledger.execute("INSERT INTO call_hour VALUES (?, ?, 1) ON CONFLICT(account, hour) DO UPDATE SET calls=calls+1",
                   (account, _hour()))
    ledger.execute("DELETE FROM call_hour WHERE hour < ?", (_hour(datetime.now(timezone.utc) - timedelta(days=30)),))


def daily_limit(account, cfg=None):
    from . import config
    songs = (cfg or config.load())["songs"]
    return int(songs.get("spotify_second_daily_limit" if account == "second" else "spotify_primary_daily_limit", 600))


class Spotify:
    def __init__(self, client_id, client_secret, market="IN", on_raw=None, max_calls=None,
                 fallback_credentials=None, stop_on_rate_limit=False, account_name="primary", enforce_daily=True):
        self._credentials = [(client_id, client_secret)] + list(fallback_credentials or [])
        self._account_names = [account_name] + [f"fallback_{i}" for i in range(1, len(self._credentials))]
        self._credential_index = 0
        self.account_name = self._account_names[0]
        self.client_id, self.client_secret = self._credentials[0]
        self.market = market
        self.on_raw = on_raw  # callback(url, status, body) for raw capture
        self.page_limit = 50  # drops to 10 automatically if Spotify rejects larger pages
        self.batch_ok = {"albums": True, "tracks": True}
        self.calls = 0
        self.calls_by_account = {self.account_name: 0}
        self.token_requests_by_account = {self.account_name: 0}
        self.max_calls = max_calls  # per-run budget; None = unlimited
        self.stop_on_rate_limit = stop_on_rate_limit
        # every call is counted per account; with enforce_daily the account's rolling-24 h limit stops the caller
        # (health probes pass False: counted, never refused, so the status check keeps working)
        self.enforce_daily = enforce_daily
        self._ledger = None
        self._token = None
        self._token_exp = 0

    def _reserve_call(self):
        """Count one call for this account, or raise DailyLimitReached if its 24-hour limit is used up."""
        with _LEDGER_LOCK:
            if self._ledger is None:
                self._ledger = _ledger()
                self._limit = daily_limit(self.account_name)
            if self.enforce_daily:
                limit, used = self._limit, calls_last_24h(self.account_name, self._ledger)
                if used >= limit:
                    raise DailyLimitReached(f"Spotify {self.account_name} account: daily limit of {limit} calls "
                                            f"used in the last 24 h; the next run continues when calls free up")
            _count_call(self._ledger, self.account_name)

    @classmethod
    def from_secrets(cls, secrets, **kwargs):
        """Build a primary-only client; account two is reserved for the explicit catch-up action."""
        primary = (secrets.get("SPOTIFY_CLIENT_ID"), secrets.get("SPOTIFY_CLIENT_SECRET"))
        if not all(primary):
            raise ValueError("Spotify primary account credentials are not configured")
        return cls(*primary, account_name="primary", **kwargs)

    @classmethod
    def second_account_from_secrets(cls, secrets, **kwargs):
        """Build a client using only the explicitly configured second account."""
        pair = (secrets.get("SPOTIFY_CLIENT_ID_2"), secrets.get("SPOTIFY_CLIENT_SECRET_2"))
        if not all(pair):
            raise ValueError("Spotify second account credentials are not configured")
        kwargs.setdefault("stop_on_rate_limit", True)
        return cls(*pair, account_name="second", **kwargs)

    def _switch_account(self):
        if self._credential_index + 1 >= len(self._credentials):
            return False
        self._credential_index += 1
        self.client_id, self.client_secret = self._credentials[self._credential_index]
        self.account_name = self._account_names[self._credential_index]
        self.calls_by_account.setdefault(self.account_name, 0)
        self.token_requests_by_account.setdefault(self.account_name, 0)
        self._token = None
        self._token_exp = 0
        return True

    def _fetch_token(self):
        self.token_requests_by_account[self.account_name] = self.token_requests_by_account.get(self.account_name, 0) + 1
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise SpotifyError(e.code, TOKEN_URL, e.read().decode(errors="replace"))
        self._token = data["access_token"]
        self._token_exp = time.time() + data.get("expires_in", 3600) - 60

    def get(self, path, params=None):
        if not self._token or time.time() >= self._token_exp:
            self._fetch_token()
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise BudgetExhausted()
            self._reserve_call()
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
            self.calls += 1
            self.calls_by_account[self.account_name] = self.calls_by_account.get(self.account_name, 0) + 1
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode()
                    if self.on_raw:
                        self.on_raw(url, r.status, body)
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if self.on_raw:  # failures are kept too, so they can be inspected later
                    self.on_raw(url, e.code, body)
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After") or 5)
                    if self.stop_on_rate_limit:
                        raise SpotifyError(429, url, body, retry_after=wait)
                    if wait > MAX_RETRY_WAIT:  # daily quota block: give up now, resume later
                        if self._switch_account():
                            self._fetch_token()
                            continue  # retry the same request on the next account
                        raise SpotifyError(429, url, body, retry_after=wait)
                    time.sleep(wait + 1)
                elif e.code == 401 and attempt == 0:
                    self._fetch_token()
                elif e.code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    raise SpotifyError(e.code, url, body)
            except urllib.error.URLError:
                time.sleep(2 ** attempt)
        raise SpotifyError(0, url, "retries exhausted")

    def _pages(self, path, params, key=None, max_offset=None):
        """Yield items page by page. Spotify's development mode caps page size (often 10) and
        reports a misleading `total`/`next` (it echoes the page size), so we keep requesting
        by offset until a page comes back short or empty."""
        offset = 0
        while max_offset is None or offset < max_offset:
            limit = self.page_limit if max_offset is None else min(self.page_limit, max_offset - offset)
            try:
                page = self.get(path, dict(params, limit=limit, offset=offset))
            except SpotifyError as e:
                if e.status == 400 and "limit" in e.body.lower() and self.page_limit > 10:
                    self.page_limit = 10  # this app tier only allows small pages
                    continue
                raise
            if key:
                page = page[key]
            items = page.get("items") or []
            for item in items:
                if item:
                    yield item
            offset += len(items)
            if len(items) < limit:
                break

    def search(self, q, type_):
        """Yield every item for a search query, up to Spotify's 1000-result cap.
        Ends with a ('__total__', n) tuple: n is the number of items actually returned."""
        n = 0
        for item in self._pages("/search", {"q": q, "type": type_, "market": self.market},
                                key=type_ + "s", max_offset=MAX_SEARCH_OFFSET):
            n += 1
            yield item
        yield ("__total__", n)

    def _several(self, kind, ids, batch):
        """Fetch full objects by id, batched; falls back to one-by-one if the
        batch endpoint is unavailable for this app."""
        out = []
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            if self.batch_ok[kind]:
                try:
                    out.extend(x for x in self.get(f"/{kind}", {"ids": ",".join(chunk), "market": self.market})[kind] if x)
                    continue
                except SpotifyError as e:
                    if e.status not in (403, 404):
                        raise
                    self.batch_ok[kind] = False
            for one in chunk:
                try:
                    out.append(self.get(f"/{kind}/{one}", {"market": self.market}))
                except SpotifyError as e:
                    if e.status != 404:
                        raise
        return out

    def albums(self, ids):
        return self._several("albums", ids, 20)

    def tracks(self, ids):
        return self._several("tracks", ids, 50)

    def album_tracks(self, album):
        """All simplified tracks of a full album object; fetches the rest when the embedded
        list is shorter than the album's track count."""
        items = list(album["tracks"]["items"])
        if len(items) < (album.get("total_tracks") or 0):
            items = list(self._pages(f"/albums/{album['id']}/tracks", {"market": self.market}))
        return items

    def artist_albums(self, artist_id):
        yield from self._pages(f"/artists/{artist_id}/albums",
                               {"include_groups": "album,single,compilation", "market": self.market})
