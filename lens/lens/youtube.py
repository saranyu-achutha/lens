"""Minimal YouTube Data API v3 client (API keys), stdlib only, with quota-unit accounting.

Several keys can be given (YOUTUBE_API_KEY=k1,k2,...). Requests use the first key that still has
quota; when Google reports quotaExceeded (or rejects a key), that key is set aside until the daily
reset at midnight Pacific time and the same request is retried with the next key."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3"
COST = {"search": 100}  # every other read endpoint we use costs 1 unit
DAILY_UNITS = 10000  # default quota per key (per Google Cloud project)

# key -> (epoch when usable again, reason); shared by every client in this process
_SET_ASIDE = {}


def parse_keys(value):
    """'k1, k2\nk3' or ['k1', 'k2'] -> ['k1', 'k2', 'k3'] (order kept, duplicates dropped)."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;]+", value or "")
    out = []
    for k in items:
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def key_hint(key):
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "set"


def next_reset():
    """Next midnight in Pacific time, when YouTube quotas reset."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = timezone(timedelta(hours=-8))
    now = datetime.now(tz)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def key_status(keys):
    """[{hint, available, until, reason}] for the Sources page."""
    out = []
    for k in parse_keys(keys):
        until, reason = _SET_ASIDE.get(k, (0, ""))
        busy = until > time.time()
        out.append({"hint": key_hint(k), "available": not busy, "reason": reason if busy else "",
                    "until": datetime.fromtimestamp(until).astimezone().isoformat(timespec="minutes") if busy else None})
    return out


class YouTubeError(Exception):
    def __init__(self, status, url, body):
        try:
            err = json.loads(body)["error"]
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            msg = err.get("message", "")
        except Exception:
            reason, msg = "", body[:300]
        super().__init__(f"HTTP {status}: {reason} {msg}".strip())
        self.status, self.reason, self.body = status, reason, body

    @property
    def quota_exceeded(self):
        return self.reason in ("quotaExceeded", "dailyLimitExceeded")

    @property
    def search_limit(self):
        """A project's daily cap on search.list calls (~100 a day), separate from the 10,000 units."""
        return self.reason == "rateLimitExceeded" and "Search Queries" in str(self)

    @property
    def bad_key(self):
        """The key itself is unusable (invalid, expired, API not enabled, restricted)."""
        return self.reason in ("keyInvalid", "keyExpired", "accessNotConfigured", "ipRefererBlocked") \
            or (self.status == 400 and "API key not valid" in str(self))


def mask(url):
    return re.sub(r"([?&]key=)[^&]+", r"\1***", url)


class YouTube:
    def __init__(self, keys, on_raw=None, log=None):
        self.keys = parse_keys(keys)
        self.on_raw = on_raw  # callback(url_without_key, status, body)
        self.log = log or (lambda m: None)
        self.units = 0
        self.units_by_key = {}  # key hint -> units used by this client

    def _key(self, endpoint=None):
        now = time.time()
        for k in self.keys:
            if _SET_ASIDE.get(k, (0, ""))[0] <= now and _SET_ASIDE.get((k, endpoint), (0, ""))[0] <= now:
                return k
        body = json.dumps({"error": {"message": f"all {len(self.keys)} YouTube API keys are out of quota or unusable",
                                     "errors": [{"reason": "quotaExceeded"}]}})
        raise YouTubeError(403, "config", body)

    def get(self, endpoint, **params):
        while True:
            key = self._key(endpoint)
            try:
                return self._get(endpoint, key, params)
            except YouTubeError as e:
                if e.search_limit:  # this key can't search any more today; it still serves other calls
                    _SET_ASIDE[(key, endpoint)] = (next_reset(), "daily search limit")
                    left = sum(1 for k in self.keys if _SET_ASIDE.get((k, endpoint), (0, ""))[0] <= time.time())
                    self.log(f"⚠ YouTube key {key_hint(key)}: daily search limit reached; next key ({left} of {len(self.keys)} can still search)")
                    continue
                if not (e.quota_exceeded or e.bad_key):
                    raise
                _SET_ASIDE[key] = (next_reset(), e.reason or "rejected")
                left = sum(1 for k in self.keys if _SET_ASIDE.get(k, (0, ""))[0] <= time.time())
                self.log(f"⚠ YouTube key {key_hint(key)}: {e.reason or 'rejected'}; "
                         f"switching to the next key ({left} of {len(self.keys)} left)")

    def _get(self, endpoint, key, params):
        url = f"{API}/{endpoint}?" + urllib.parse.urlencode(dict(params, key=key))
        for attempt in range(4):
            cost = COST.get(endpoint, 1)
            self.units += cost
            self.units_by_key[key_hint(key)] = self.units_by_key.get(key_hint(key), 0) + cost
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    body = r.read().decode()
                    if self.on_raw:
                        self.on_raw(mask(url), r.status, body)
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if self.on_raw:
                    self.on_raw(mask(url), e.code, body)
                if e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise YouTubeError(e.code, mask(url), body)
            except urllib.error.URLError:
                time.sleep(2 ** attempt)
        raise YouTubeError(0, mask(url), "retries exhausted")

    def channels(self, ids=None, handle=None):
        p = {"part": "snippet,statistics,contentDetails"}
        if handle:
            p["forHandle"] = handle
        else:
            p["id"] = ",".join(ids)
        return self.get("channels", **p).get("items", [])

    def uploads(self, channel_id, page_token=None):
        """One page (50) of a channel's uploads, newest first."""
        p = {"part": "snippet,contentDetails", "playlistId": "UU" + channel_id[2:], "maxResults": 50}
        if page_token:
            p["pageToken"] = page_token
        return self.get("playlistItems", **p)

    def videos(self, ids):
        out = []
        for i in range(0, len(ids), 50):
            # snippet (tags, audio language, category) costs nothing extra: videos.list is 1 unit per call.
            out += self.get("videos", part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]),
                            maxResults=50).get("items", [])
        return out


def iso_duration(s):
    """PT1H2M3S -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se
