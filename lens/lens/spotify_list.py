"""List every Spotify release (and its tracks) of the shortlisted labels from the period start (laptop tool).

    python3 -m lens.spotify_list [--accounts second] [--state data/spotify_list_state.json] [--known known.txt]

For each switched-on label's phrases (songs.spotify_label_queries): album search label:"<phrase>" year:<y> page by page
(10 a page, Spotify shows at most 1,000 a query), keep albums released from the period start, then one
/albums/{id}/tracks call each. One thread per account, one call a second each (songs.spotify_min_interval_s); an account
stops at its first 429. Progress is saved to the state file as it goes, so the next run continues where this one
stopped. --known: album IDs already read on the server (one per line), not fetched again.
On the server, `python3 -m lens.spotify_labels import <state file>` loads the list; its track lists are matched and
linked there without further calls.
"""
import argparse
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .spotify_labels import queries

API = "https://api.spotify.com/v1"


class Lane:
    """One account: its own token and pace; one call at a time."""
    def __init__(self, acct, secrets, interval, state, lock):
        cid, sec = (secrets["SPOTIFY_CLIENT_ID_2"], secrets["SPOTIFY_CLIENT_SECRET_2"]) if acct == "second" else \
                   (secrets["SPOTIFY_CLIENT_ID"], secrets["SPOTIFY_CLIENT_SECRET"])
        creds = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
                                     headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
        self.tok = json.load(urllib.request.urlopen(req, timeout=30))["access_token"]
        self.acct, self.interval, self.S, self.lock = acct, interval, state, lock
        self.next, self.stop = time.monotonic(), None

    def get(self, path, params):
        if self.stop:
            return None
        time.sleep(max(0, self.next - time.monotonic()))
        self.next = max(time.monotonic(), self.next) + self.interval
        url = f"{API}{path}?" + urllib.parse.urlencode(dict(params, market="IN"))
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {self.tok}"}), timeout=30) as r:
                    body = json.loads(r.read())
                self._count()
                return body
            except urllib.error.HTTPError as e:
                self._count()
                if e.code == 429:
                    self.stop = f"429 (Retry-After {e.headers.get('Retry-After')} s) after {self.S['calls'][self.acct]} calls"
                    with self.lock:
                        self.S["stops"][self.acct] = dict(reason=self.stop, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                          retry_after=e.headers.get("Retry-After"))
                    return None
                if e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                return {}
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt)
        return {}

    def _count(self):
        with self.lock:
            self.S["calls"][self.acct] = self.S["calls"].get(self.acct, 0) + 1


def run(accounts=("second",), state_file="data/spotify_list_state.json", known_file=None, log=print):
    cfg = config.load()
    since, until = config.window(cfg)
    interval = float(cfg["songs"].get("spotify_min_interval_s", 1))
    labels = queries(cfg)
    S = json.load(open(state_file)) if os.path.exists(state_file) else {"since": since, "albums": {}, "queries": {}, "calls": {}, "stops": {}}
    for lb, phrases in labels.items():  # new labels, years or phrases join the list
        for p in phrases:
            for y in range(int(since[:4]), int(until[:4]) + 1):
                S["queries"].setdefault(f"{lb}|{p}|{y}", {"label": lb, "q": f'label:"{p}" year:{y}', "offset": 0, "done": False})
    known = set(open(known_file).read().split()) if known_file and os.path.exists(known_file) else set()
    lock = threading.Lock()
    S["stops"] = {}

    def save():
        with lock:
            tmp = state_file + ".tmp"
            json.dump(S, open(tmp, "w"))
            os.replace(tmp, state_file)

    def work(lane):
        n = 0
        while not lane.stop:
            with lock:  # next job: an album still without its tracks, else the next page of an open search
                alb = next((a for a in S["albums"].values() if a.get("tracks") is None and not a.get("taken")), None)
                if alb:
                    alb["taken"] = True
                q = None if alb else next((x for x in S["queries"].values() if not x["done"] and not x.get("taken")), None)
                if q:
                    q["taken"] = True
            if alb:
                res = lane.get(f"/albums/{alb['id']}/tracks", {"limit": 50})
                with lock:
                    alb.pop("taken", None)
                    if res is not None:
                        alb["tracks"] = [{"id": t["id"], "name": t["name"], "duration_ms": t.get("duration_ms"),
                                          "artists": [{"name": r["name"], "id": r["id"]} for r in t["artists"]]}
                                         for t in res.get("items") or []]
            elif q:
                page = lane.get("/search", {"q": q["q"], "type": "album", "limit": 10, "offset": q["offset"]})
                with lock:
                    q.pop("taken", None)
                    if page is not None:
                        items = [a for a in (page.get("albums") or {}).get("items", []) if a]
                        for a in items:
                            if (a.get("release_date") or "") >= since and a["id"] not in S["albums"] and a["id"] not in known:
                                S["albums"][a["id"]] = {"id": a["id"], "label": q["label"], "name": a["name"],
                                                        "release_date": a.get("release_date"), "total_tracks": a.get("total_tracks"), "tracks": None}
                        q["offset"] += 10
                        q["done"] = len(items) < 10 or q["offset"] >= 1000
            else:
                return
            n += 1
            if n % 20 == 0:
                save()

    secrets = config.read_secrets()
    t0 = time.time()
    calls0 = dict(S["calls"])
    lanes = [Lane(a, secrets, interval, S, lock) for a in accounts]
    threads = [threading.Thread(target=work, args=(lane,)) for lane in lanes]
    for t in threads:
        t.start()
    shown = 0
    while any(t.is_alive() for t in threads):
        time.sleep(5)
        n = sum(S["calls"].values())
        if n - shown >= 120:
            shown = n
            log(f"{time.strftime('%H:%M:%S')} {n} calls, {len(S['albums'])} releases "
                f"({sum(a['tracks'] is not None for a in S['albums'].values())} read), "
                f"{sum(len(a['tracks'] or []) for a in S['albums'].values())} tracks, "
                f"{sum(q['done'] for q in S['queries'].values())}/{len(S['queries'])} searches done")
    for a in S["albums"].values():
        a.pop("taken", None)
    for q in S["queries"].values():
        q.pop("taken", None)
    save()
    read = sum(a["tracks"] is not None for a in S["albums"].values())
    return {"minutes": round((time.time() - t0) / 60, 1),
            "calls_this_run": {k: v - calls0.get(k, 0) for k, v in S["calls"].items()}, "stops": S["stops"],
            "releases": len(S["albums"]), "releases_read": read, "tracks": sum(len(a["tracks"] or []) for a in S["albums"].values()),
            "searches_done": f"{sum(q['done'] for q in S['queries'].values())}/{len(S['queries'])}",
            "complete": read == len(S["albums"]) and all(q["done"] for q in S["queries"].values()),
            "by_label": {lb: {"releases": sum(a["label"] == lb for a in S["albums"].values()),
                              "tracks": sum(len(a["tracks"] or []) for a in S["albums"].values() if a["label"] == lb)} for lb in labels}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--accounts", default="second", help="comma-separated: primary, second")
    ap.add_argument("--state", default=str(config.DB_FILE.parent / "spotify_list_state.json"))
    ap.add_argument("--known", help="file of album IDs already read on the server")
    a = ap.parse_args()
    print(json.dumps(run(a.accounts.split(","), a.state, a.known), indent=1))


if __name__ == "__main__":
    main()
