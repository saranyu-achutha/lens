"""Minimal Spotify Web API client (client-credentials flow), stdlib only."""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

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


class Spotify:
    def __init__(self, client_id, client_secret, market="IN", on_raw=None, max_calls=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.market = market
        self.on_raw = on_raw  # callback(url, status, body) for raw capture
        self.page_limit = 50  # drops to 10 automatically if Spotify rejects larger pages
        self.batch_ok = {"albums": True, "tracks": True}
        self.calls = 0
        self.max_calls = max_calls  # per-run budget; None = unlimited
        self._token = None
        self._token_exp = 0

    def _fetch_token(self):
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
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
            self.calls += 1
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
                    if wait > MAX_RETRY_WAIT:  # daily quota block: give up now, resume later
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
