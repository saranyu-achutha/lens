"""Self-healing page readers (lens/heal.py): every reader on real saved pages, then on the same pages changed the way a
site might change them (a renamed marker, a moved block, a missing field). Run from the app folder:

    python3 -m unittest lens.tests.test_heal -v

The Spotify track-page script runs in headless Chrome when one is installed (skipped otherwise).
"""
import gzip
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from lens import db, freshness, heal, instagram_audio, jiosaavn, shorts_use, spotify_links, spotify_plays

FX = Path(__file__).parent / "fixtures"


def fixture(name):
    return gzip.decompress((FX / f"{name}.gz").read_bytes()).decode("utf-8")


class ShortsPage(unittest.TestCase):
    def setUp(self):
        self.page = fixture("yt_sound.html")

    def test_usual_page(self):
        p = shorts_use.parse(self.page)
        self.assertEqual((p["count"], p["count_text"], p["how"]), (99000, "99K Shorts", "header"))
        self.assertTrue(p["owner_id"].startswith("UC"))
        self.assertGreater(len(p["shorts"]), 0)

    def test_renamed_header_renderer(self):
        usual = shorts_use.parse(self.page)
        p = shorts_use.parse(self.page.replace('"pageHeaderRenderer"', '"pageHeaderViewModel2"'))
        self.assertEqual((p["count"], p["owner_id"], p["how"]), (usual["count"], usual["owner_id"], "any-header"))

    def test_header_moved_elsewhere(self):
        usual = shorts_use.parse(self.page)
        d = shorts_use.initial_data(self.page)
        d["somewhereElse"] = {"wrapper": d.pop("header")}
        page = "<script>var ytInitialData = " + json.dumps(d) + ";</script>"
        p = shorts_use.parse(page)
        self.assertEqual((p["count"], p["owner_id"], p["how"]), (usual["count"], usual["owner_id"], "scan"))

    def test_no_header_is_not_a_count(self):
        d = shorts_use.initial_data(self.page)
        d.pop("header")
        page = "<script>var ytInitialData = " + json.dumps({k: v for k, v in d.items() if k not in ("contents",)}) + ";</script>"
        self.assertIsNone(shorts_use.parse(page))
        self.assertIsNotNone(shorts_use.initial_data(page))  # fetch() reports "no sound page", trusted only if the guards pass

    def test_count_with_a_brace_inside_a_string(self):  # raw_decode: a "};" inside the data must not cut it short
        page = self.page.replace('"99K Shorts"', '"99K Shorts"', 1).replace("var ytInitialData = {", 'var ytInitialData = {"x":"};",', 1)
        self.assertEqual(shorts_use.parse(page)["count"], 99000)


class InstagramPage(unittest.TestCase):
    def setUp(self):
        self.page = fixture("ig_audio.html")

    def test_usual_page(self):
        p = instagram_audio.parse(self.page)
        self.assertEqual((p["count"], p["how"]), (83000, "audio-label"))
        self.assertIn("Kalyana Virundhu", p["title"])

    def test_audio_label_gone_uses_og_description(self):
        page = re.sub(r">\s*Audio\s*<", "><", self.page)
        p = instagram_audio.parse(page)
        self.assertEqual((p["count"], p["how"], p["title"]), (83000, "og-description", "Kalyana Virundhu"))
        self.assertFalse(p["original"])

    def test_login_wall(self):
        wall = '<html><head><meta property="og:url" content="https://www.instagram.com/accounts/login/"></head><body><form id="loginForm"><input name="username"></form></body></html>'
        self.assertIsNone(instagram_audio.parse(wall))
        self.assertTrue(instagram_audio.login_wall(wall))
        self.assertFalse(instagram_audio.login_wall(self.page))


class SpotifyEmbed(unittest.TestCase):
    SID = "2jw1vFPJnVSjywctRTpVeO"

    def setUp(self):
        self.page = fixture("sp_embed.html")

    def test_usual_page(self):
        d, how = spotify_links.parse_embed(self.page, self.SID)
        self.assertEqual(how, "next-data")
        self.assertEqual(d["title"], "Woh Lamhe Woh Baatein")
        self.assertTrue(d["duration_ms"] and d["release_date"] and d["artists"])

    def test_moved_entity(self):
        usual, _ = spotify_links.parse_embed(self.page, self.SID)
        d, how = spotify_links.parse_embed(self.page.replace('"pageProps"', '"pagePropsV2"'), self.SID)
        self.assertEqual(how, "scan")
        self.assertEqual((d["title"], d["duration_ms"], d["release_date"]), (usual["title"], usual["duration_ms"], usual["release_date"]))

    def test_unreadable_page_is_not_gone(self):
        self.assertEqual(spotify_links.parse_embed("<html><body>changed</body></html>", self.SID), (None, None))


class JioSaavnReply(unittest.TestCase):
    def setUp(self):
        d = json.loads(fixture("js_details.json"))
        self.song = (d.get("songs") or [v for v in d.values() if isinstance(v, dict)])[0]

    def test_usual(self):
        v, how = jiosaavn.play_count(self.song)
        self.assertEqual(how, "play_count")
        self.assertGreater(int(v), 1_000_000)

    def test_moved_and_renamed(self):
        s = dict(self.song)
        v = s.pop("play_count")
        self.assertEqual(jiosaavn.play_count(dict(s, more_info=dict(s.get("more_info") or {}, play_count=v))), (v, "more_info"))
        self.assertEqual(jiosaavn.play_count(dict(s, stats={"playCount": v})), (v, "scan"))

    def test_missing_field_is_not_hidden(self):
        s = {k: v for k, v in self.song.items() if k != "play_count"}
        s["more_info"] = {k: v for k, v in (s.get("more_info") or {}).items() if k != "play_count"}
        self.assertEqual(jiosaavn.play_count(s), (None, None))  # a changed reply: retried, never marked as hidden
        self.assertEqual(jiosaavn.play_count(dict(s, play_count="")), ("", "play_count"))  # a hidden count


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        heal.ensure(self.conn)

    def test_values(self):
        self.assertEqual(heal.check_value(None, 5, "plays"), "ok")
        self.assertEqual(heal.check_value(1000, 1050, "plays"), "ok")
        self.assertEqual(heal.check_value(1000, 850, "plays"), "fell")
        self.assertEqual(heal.check_value(100_000, 50_000_000, "plays"), "jumped")
        self.assertEqual(heal.check_value(99_000, 60_000, "shorts"), "ok")  # rounded counts may move more

    def test_mass_guard_learns_the_usual_share(self):
        for _ in range(5):
            self.assertFalse(heal.mass_guard(self.conn, "x", "hidden", 10, 100))  # 10% is normal for this reader
        self.assertTrue(heal.mass_guard(self.conn, "x", "hidden", 80, 100))      # 80% is not
        self.assertLess(heal.baseline(self.conn, "x", "hidden"), 0.2)           # a held run does not become normal

    def test_known_guard(self):
        self.assertFalse(heal.known_guard(self.conn, "x", 4, 4))   # too few to judge
        self.assertTrue(heal.known_guard(self.conn, "x", 10, 6))
        self.assertFalse(heal.known_guard(self.conn, "x", 10, 1))

    def test_learn_needs_several_items(self):
        self.assertIsNone(heal.learn(self.conn, "x", {"newMarker": 2}))
        self.assertEqual(heal.learn(self.conn, "x", {"newMarker": 5, "other": 1}), "newMarker")
        self.assertEqual(heal.hint(self.conn, "x"), "newMarker")

    def test_summary_states(self):
        heal.record(self.conn, "spotify_plays", "testid", 50)
        heal.record(self.conn, "shorts", "any-header", 40)
        heal.record(self.conn, "shorts", "header", 2)
        heal.record(self.conn, "instagram", "fail", 12)
        st = {r["reader"]: r["state"] for r in heal.summary(self.conn)["readers"]}
        self.assertEqual((st["spotify_plays"], st["shorts"], st["instagram"], st["jiosaavn"]), ("ok", "healed", "broken", "idle"))
        keys = {a["key"]: a["severity"] for a in heal.alert_conditions(self.conn)}
        self.assertEqual((keys.get("heal:shorts"), keys.get("heal:instagram")), ("warning", "critical"))


class SpotifyImport(unittest.TestCase):
    """import_file: suspicious values are not stored; "hidden" outcomes are held when known tracks come back empty."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        c, ts = self.conn, db.now_iso()
        freshness.ensure(c)
        for i in range(12):
            c.execute("INSERT INTO platform_item (platform, platform_id, title, first_seen_at) VALUES ('spotify', ?, ?, ?)", (f"T{i}", f"t{i}", ts))
            c.execute("INSERT INTO snapshot (platform_item_id, metric, value, capture_date, captured_at) VALUES (?, 'plays', ?, '2026-11-09', ?)",
                      (i + 1, 100_000 + i, ts))
        c.commit()

    def write(self, plays, hidden, missing=None, how=None):
        p = Path(self.tmp) / "2026-11-10.json"
        p.write_text(json.dumps({"date": "2026-11-10", "captured_at": "2026-11-10T06:00:00+00:00", "plays": plays, "hidden": hidden,
                                 "missing": missing or {}, "how": how or {"testid": len(plays)}, "markers": {}}))
        return p

    def marks(self):
        return self.conn.execute("SELECT count(*) FROM read_mark").fetchone()[0]

    def test_known_tracks_empty_holds_hidden(self):
        p = self.write({"T0": 100_500}, [f"T{i}" for i in range(1, 12)])
        spotify_plays.import_file(self.conn, p, log=lambda m: None)
        self.assertEqual(self.marks(), 0)  # nothing marked as read
        self.assertTrue(self.conn.execute("SELECT 1 FROM state WHERE key='heal_held:spotify_plays:2026-11-10'").fetchone())

    def test_values_that_fell_are_not_stored(self):
        p = self.write({"T0": 100_500, "T1": 5_000}, [])
        spotify_plays.import_file(self.conn, p, log=lambda m: None)
        got = dict(self.conn.execute("SELECT platform_item_id, value FROM snapshot WHERE capture_date='2026-11-10'").fetchall())
        self.assertEqual(got, {1: 100_500})
        self.assertEqual(self.conn.execute("SELECT reason FROM reading_suspect").fetchone()[0], "fell")

    def test_gone_only_when_spotify_says_so(self):
        p = self.write({f"T{i}": 101_000 + i for i in range(10)}, [], {"T10": "gone: Page not found", "T11": "page error: not shown Spotify"})
        spotify_plays.import_file(self.conn, p, log=lambda m: None)
        rows = [tuple(r) for r in self.conn.execute("SELECT platform_item_id, outcome FROM read_mark").fetchall()]
        self.assertEqual(rows, [(11, "gone")])  # T11 (a page that did not show) is retried, never marked


def _chrome():
    try:
        return spotify_plays.find_chrome()
    except SystemExit:
        return None


@unittest.skipUnless(_chrome(), "no headless Chrome on this machine")
class SpotifyTrackPageScript(unittest.TestCase):
    """The play-count script in a real browser on saved track pages, as found and as a site change might leave them."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        ok, hidden = fixture("sp_ok.html"), fixture("sp_hidden.html")
        pages = {"ok": ok, "renamed": ok.replace('data-testid="playcount"', 'data-testid="trackPlays"'),
                 "renamed_notitle": ok.replace('data-testid="playcount"', 'data-testid="trackPlays"').replace('data-testid="entityTitle"', 'data-testid="hdr"'),
                 "hidden": hidden, "blank": "<html><head><title>Spotify</title></head><body><main></main></body></html>"}
        for k, v in pages.items():
            Path(cls.dir, f"{k}.html").write_text(v)
        chrome, cls.port = _chrome(), 9557
        cls.proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={tempfile.mkdtemp()}",
                                     f"--remote-debugging-port={cls.port}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(chrome)), start_new_session=True)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/json/version", timeout=2)
                break
            except OSError:
                time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        spotify_plays._kill_group(cls.proc)

    def read(self, name, hint=None):
        t = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")))
        ws = spotify_plays._WS(t["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Page.navigate", url=f"file://{self.dir}/{name}.html")
        time.sleep(1.0)
        r = json.loads(ws.call("Runtime.evaluate", expression=spotify_plays.read_js(hint), returnByValue=True)["result"]["value"])
        ws.sock.close()
        return r

    def test_usual(self):
        r = self.read("ok")
        self.assertEqual((r["plays"], r["how"]), ("263,551,827", "testid"))

    def test_renamed_marker_found_and_learned(self):
        r = self.read("renamed")
        self.assertEqual((r["plays"], r["how"], r["marker"]), ("263,551,827", "header-row", "trackPlays"))
        self.assertEqual(self.read("renamed", "trackPlays")["how"], "learned")

    def test_title_marker_also_renamed(self):
        self.assertEqual(self.read("renamed_notitle")["plays"], "263,551,827")

    def test_hidden_count_gives_no_number(self):  # the page lists other tracks' counts: never take one of those
        r = self.read("hidden")
        self.assertEqual((r["plays"], r["loaded"]), (None, True))

    def test_blank_page_is_not_loaded(self):
        r = self.read("blank")
        self.assertEqual((r["plays"], r["loaded"]), (None, False))


if __name__ == "__main__":
    unittest.main()
