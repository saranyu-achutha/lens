"""Song metadata from fixed rules (no AI), stored one value per row in `song_meta` so every field can be
rolled up into metrics (Explore page): who sang, composed and wrote each song, its film or album, cast,
language, category, occasion and label.

Sources, in the order they are trusted:
  spotify          ISRC, album, release date, Spotify artists (Spotify is the best for these)
  youtube_credits  the credits block labels put in video descriptions ("Singer : X", "Music - Y",
                   "Lyrics: Z", "Directed by W", "Composed, Arranged & Produced by V" ...)
  youtube_title    'X Movie' / 'from "X"' in titles, language words in titles
  youtube_language the video's audio language set by the label on YouTube
  channel          the channel's label and language (e.g. Saregama Tamil -> Tamil)
  rule             category / occasion keyword rules over title, tags and channel

The step rebuilds the rows of every song in the window on each run; it makes no API calls.
"""
import json
import re
import unicodedata
from collections import Counter, defaultdict

from . import config, match

# Field -> label shown in the UI. Order is the display order.
FIELDS = {
    "film": "Film",
    "album": "Album",
    "music_director": "Music director",
    "singer": "Singers",
    "lyricist": "Lyricists",
    "cast": "Cast / featuring",
    "director": "Film director",
    "producer": "Producer",
    "video_director": "Video director",
    "arranger": "Arranger / programming",
    "mix_master": "Mixing & mastering",
    "choreographer": "Choreographer",
    "music_label": "Music label (credits)",
    "label": "Label (tracked)",
    "channel": "YouTube channel",
    "language": "Language",
    "category": "Category",
    "occasion": "Occasion",
    "spotify_artist": "Spotify artists",
    "spotify_album": "Spotify album",
    "apple_album": "Apple Music album",
    "apple_artist": "Apple Music artists",
    "genre": "Genre (Apple Music)",
    "release_date": "Release date (Apple Music)",
    "song_title": "Official song title",
}
# Fields with metrics on the Explore page.
EXPLORE = ["singer", "music_director", "lyricist", "cast", "film", "director", "language", "category", "genre",
           "occasion", "label", "channel", "spotify_artist"]

LANGS = ["Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Bengali", "Marathi", "Gujarati", "Punjabi",
         "Bhojpuri", "Odia", "Assamese", "Chhattisgarhi", "Haryanvi", "Rajasthani", "Himachali", "Sanskrit", "English", "Urdu"]
LANG_WORDS = {l.lower(): l for l in LANGS} | {"bangla": "Bengali", "oriya": "Odia", "tamizh": "Tamil"}
YT_LANG = {"hi": "Hindi", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "kn": "Kannada", "bn": "Bengali",
           "mr": "Marathi", "gu": "Gujarati", "pa": "Punjabi", "bho": "Bhojpuri", "or": "Odia", "as": "Assamese",
           "sa": "Sanskrit", "ur": "Urdu", "en": "English", "hne": "Chhattisgarhi", "raj": "Rajasthani"}
SCRIPTS = [(0x0B80, 0x0BFF, "Tamil"), (0x0C00, 0x0C7F, "Telugu"), (0x0C80, 0x0CFF, "Kannada"),
           (0x0D00, 0x0D7F, "Malayalam"), (0x0980, 0x09FF, "Bengali"), (0x0A80, 0x0AFF, "Gujarati"),
           (0x0A00, 0x0A7F, "Punjabi"), (0x0B00, 0x0B7F, "Odia"), (0x0900, 0x097F, "Hindi")]
# Channels whose name has no language word but publish one language.
CHANNEL_LANG = {"Aditya Music": "Telugu", "Aditya Bhakthi": "Telugu", "Think Music India": "Tamil",
                "Think Indie Collective": "Tamil", "Mharo Rajasthan": "Rajasthani", "Saregama Hum Bhojpuri": "Bhojpuri",
                "T-Series Apna Punjab": "Punjabi", "Shabad Gurbani": "Punjabi", "T-Series Himachali": "Himachali",
                "T-Series Bhavageethegalu & Folk": "Kannada"}

CATEGORY_RULES = [  # (category, channel-name pattern, title/tags pattern); first match wins
    ("Devotional", r"bhakti|bhakthi|devotional|divine|gurbani|shabad|islamic|ibadat|prarthana",
     r"\b(aarti|arti|bhajan|stotram|stotra|mantra|chalisa|devotional|bhakti|bhakthi|jas geet|kirtan|shloka|ashtakam|suprabhatam|mata rani|bhakti geet)\b"),
    ("Classical", r"classical|carnatic|hindustani", r"\b(hindustani|carnatic|thumri|khayal|raag|raga)\b"),
    ("Ghazal / Sufi", r"ghazal|sufi", r"\b(ghazal|sufi|qawwali|nazm)\b"),
    ("TV / Web show", None, r"\b(ep \d+|episode|netflix|prime video|web series|season \d)\b"),
]
OCCASIONS = [
    ("Navratri", r"navratri|navaratri|garba|dandiya|durga ashtami"),
    ("Ganesh Chaturthi", r"ganpati|ganesh chaturthi|ganesha|bappa|vinayaka|vinayagar"),
    ("Durga Puja", r"durga puja|pujo|mahalaya"),
    ("Dussehra", r"dussehra|dasara|vijayadashami|dashain"),
    ("Diwali", r"diwali|deepavali|deepawali"),
    ("Chhath", r"chhath"),
    ("Karwa Chauth", r"karwa chauth"),
    ("Janmashtami", r"janmashtami"),
    ("Onam", r"\bonam\b"),
    ("Holi", r"\bholi\b"),
    ("Birth anniversary / tribute", r"birth anniversary|death anniversary|tribute"),
]

FILM_KEYS = re.compile(r"^(film|movie|movie name|film name|movie title|from the (film|movie)|film title)$")
ALBUM_KEYS = re.compile(r"^(album|album title|album name|ep|ep name)$")
# Promotional lines that are never credits.
PROMO = re.compile(r"download|subscribe|follow|visit|\bbuy\b|click|watch|stream|listen|learn|app\b|channel|"
                   r"youtube|instagram|facebook|twitter|whatsapp|\bnow\b|http", re.I)
LABEL_NAMES = [(r"saregama", "Saregama India Ltd"), (r"sony music", "Sony Music Entertainment India"),
               (r"aditya music", "Aditya Music"), (r"think music", "Think Music"), (r"t-?series", "T-Series"),
               (r"zee music", "Zee Music Company"), (r"lahari", "Lahari Music"), (r"tips", "Tips Music")]
SECTION_FILM = re.compile(r"(movie|film) (credits|details)|video credits|cast and crew|crew")
SECTION_SONG = re.compile(r"(song|audio|music) credits|credits")


def clean_text(s):
    """NFKC turns styled letters (𝐒𝐨𝐧𝐠 𝐂𝐫𝐞𝐝𝐢𝐭𝐬) into plain ones; drop emoji and bullets."""
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(ch for ch in s if not unicodedata.category(ch).startswith("S") or ch in "&+@'") \
        .replace("‍", "").replace("️", "")


def name_key(name):
    """Normalised person/value key: 'S. P. Balasubrahmanyam' and 'SP Balasubrahmanyam' match."""
    k = match.fold(name)
    while True:
        k2 = re.sub(r"\b([a-z]) ([a-z])\b", r"\1\2", k)
        if k2 == k:
            break
        k = k2
    return re.sub(r"\b([a-z]{1,2}) (?=[a-z]{3,})", r"\1", k) if len(k.split()) > 1 and len(k.split()[0]) <= 2 else k


def split_names(value):
    """'Vineeth Sreenivasan & Renjith Unni, @SaiAbhyankkar' -> ['Vineeth Sreenivasan', 'Renjith Unni', 'Sai Abhyankkar']"""
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"\s+(at|@)\s+.*$", "", value)          # 'Mixed by X at Y Studios'
    value = re.sub(r"\([^)]*\)", " ", value)
    out = []
    for part in re.split(r"\s*(?:,|&|/|;|\band\b|\||\s[–—]\s)\s*", value):
        part = part.strip(" .-:–—\"'")
        if part.startswith("@"):
            part = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", part[1:])
        part = re.sub(r"\s+", " ", part)
        if not part or len(part) > 60 or len(part.split()) > 6 or re.search(r"\d{3,}|www\.|\.com", part):
            continue
        if part.lower() in ("chorus", "group", "various artists", "unknown"):
            continue
        if part.lower() in ("others", "team", "and others", "etc", "n/a", "na", "traditional"):
            if part.lower() == "traditional":
                out.append("Traditional")
            continue
        out.append(part)
    return out


def roles_for(key):
    """Credit roles a description key stands for (a key can hold several: 'Music & Lyrics')."""
    k = key.lower()
    if FILM_KEYS.match(k):
        return ["film"]
    if ALBUM_KEYS.match(k):
        return ["album"]
    if re.search(r"song (name|title)|track name|^song$|^title$", k):
        return ["song_title"]
    if "label" in k:
        return ["music_label"]
    if re.search(r"backing|chorus|harmony|choir|supervis|advis|consult|operation|manager|coordinat|head|coach|"
                 r"assist|associate|design|styl|makeup|hair|\bdi\b|\bdit\b|\bart\b|rhythm|percussion|guitar|keyboard|"
                 r"violin|flute|shehnai|kanjira|bass|strings|live|session|executive|line produc|co.?produc|creative|"
                 r"management|project|publicity|digital|marketing|poster|still|vfx|colou?rist|banner|presents|"
                 r"engineer|record|team|photograph|\bdop\b|camera|edit|special thanks|courtesy|process|tuning|"
                 r"dubbing|whistle|humming|ad.?lib", k):
        return []
    r = []
    if re.search(r"\bmusic\b", k) and re.search(r"produc", k) and not re.search(r"compos", k):
        return ["arranger"]  # 'Music Produced by' is music production, not the composer
    if re.search(r"compos", k) or (re.search(r"\bmusic\b", k) and not re.search(r"video|arrang|programm|label|"
                                                                              r"additional|production", k)):
        r.append("music_director")
    if re.search(r"lyric|lyrcist|\bwords\b|penned", k):
        r.append("lyricist")
    if re.search(r"\bsing|\bsung\b|vocal|\bvox\b|voice|^artists?$|^rap$|and rap|rap and", k):
        r.append("singer")
    if re.search(r"arrang|programm", k):
        r.append("arranger")
    if re.search(r"\bmix|master", k):
        r.append("mix_master")
    if re.search(r"\bcast\b|starring|featuring|\bactors?\b", k):
        r.append("cast")
    if re.search(r"direct", k) and not re.search(r"music|art|creative|photograph|dance|choreo|associate|assistant", k):
        r.append("director")
    if re.search(r"produc", k) and not re.search(r"music|compos|arrang|rhythm|additional|line|executive|co.?produc|"
                                                 r"production (company|house)", k) and "music_director" not in r:
        r.append("producer")
    if re.search(r"choreo", k):
        r.append("choreographer")
    return r


KV = re.compile(r"^\s*([A-Za-z][A-Za-z ,&/.'’-]{1,60}?)\s*(?::|\s-\s|-\s|–|—|\s-(?=\S))\s*(.*)$")
BY = re.compile(r"^\s*([A-Za-z][A-Za-z ,&/'-]{2,60}?)\s+by\s*[:\-]?\s*(.+)$", re.I)


def parse_credits(description):
    """[(role, name, section)] from a video description's credits lines. section is 'song' or 'film'."""
    out = []
    section = "song"
    lines = [clean_text(l).strip() for l in (description or "").splitlines()]
    pending = None  # 'Cast :' with the names on the next line
    for line in lines:
        if not line:
            continue
        low = line.lower().strip(" :-")
        if len(low) < 40 and SECTION_FILM.search(low) and not KV.match(line.rstrip(":- ").rstrip()) or \
                re.fullmatch(r"(movie|film|video) (credits|details)\s*[:\-]*", low):
            section = "video" if low.startswith("video") else "film"
            pending = None
            continue
        if len(low) < 40 and re.fullmatch(r"(song|audio|music|track)?\s*credits\s*[:\-]*", low):
            section = "song"
            pending = None
            continue
        if pending and not KV.match(line) and not BY.match(line) and len(line) < 200 and not PROMO.search(line):
            names = split_names(line)
            if names and all(len(x.split()) <= 4 and x[:1].isupper() for x in names):
                for role in pending:
                    out += [(role, n, section) for n in names]
            pending = None
            continue
        pending = None
        m = BY.match(line)
        key = val = None
        if m and roles_for(m.group(1)):
            key, val = m.group(1), m.group(2)
        else:
            m = KV.match(line)
            if m:
                key, val = m.group(1), m.group(2)
        if not key or len(key.split()) > 6 or PROMO.search(line) and not re.search(r"label", key, re.I):
            continue
        roles = roles_for(key.strip())
        if not roles:
            continue
        val = val.strip(" :-–")
        if not val:
            pending = [r for r in roles if r in ("cast", "singer", "music_director", "producer", "director")] or None
            continue
        if roles in (["film"], ["album"]):
            v = re.sub(r"\(.*?\)|https?://\S+", "", val).strip(" \"'“”‘’.")
            if v and len(v) <= 80:
                out.append((roles[0], v, section))
            continue
        if roles == ["song_title"]:
            v = val.strip(" \"'“”‘’.")
            if v and len(v) <= 80:
                out.append(("song_title", v, section))
            continue
        if roles == ["music_label"]:
            v = re.sub(r"https?://\S+", "", val).strip(" .:")
            v = next((name for pat, name in LABEL_NAMES if re.search(pat, v, re.I)), v)
            if v and len(v) <= 80:
                out.append(("music_label", v, section))
            continue
        for role in roles:
            out += [(role, n, section) for n in split_names(val)]
    return out


def film_from_title(title):
    """'… | Oke Okkadu Movie | …' or 'from "Baththa"' -> film name."""
    m = re.search(r"(?:from|movie)\s+[\"“'‘]([^\"”'’]{2,60})[\"”'’]", title or "", re.I)
    if m:
        return m.group(1).strip()
    for part in re.split(r"\s*[|｜]\s*", title or ""):
        m = re.fullmatch(r"(.{2,50}?)\s+(?:telugu |tamil |kannada |malayalam |hindi )?(?:movie|film)(?: songs?)?", part.strip(), re.I)
        if m and not re.search(r"\b(song|video|lyric)", m.group(1), re.I):
            return m.group(1).strip()
    return None


def film_from_spotify(title, album, album_type):
    m = re.search(r"\(from [\"“]([^\"”]+)[\"”]\)", title or "", re.I)
    if m:
        return m.group(1)
    if album and re.search(r"original motion picture soundtrack|\(ost\)|original soundtrack", album, re.I):
        return re.sub(r"\s*[\(\[].*?(soundtrack|ost).*?[\)\]]\s*", "", album, flags=re.I).strip()
    return None


def script_language(text):
    counts = Counter()
    for ch in text or "":
        o = ord(ch)
        for lo, hi, lang in SCRIPTS:
            if lo <= o <= hi:
                counts[lang] += 1
                break
    return counts.most_common(1)[0][0] if counts else None


def language_of(videos, channel):
    """(language, source) by fixed precedence."""
    titles = " ".join(v["title"] or "" for v in videos)
    tags = " ".join(" ".join(json.loads(v["yt_tags"] or "[]")) for v in videos)
    for w in re.findall(r"[a-z]+", titles.lower()):
        if w in LANG_WORDS and w != "english":
            return LANG_WORDS[w], "youtube_title"
    codes = Counter((v["audio_language"] or "").split("-")[0].lower() for v in videos if v["audio_language"])
    for code, _ in codes.most_common():
        if code in YT_LANG and code != "en":
            return YT_LANG[code], "youtube_language"
    for w in re.findall(r"[a-z]+", (channel or "").lower()):
        if w in LANG_WORDS:
            return LANG_WORDS[w], "channel"
    if channel in CHANNEL_LANG:
        return CHANNEL_LANG[channel], "channel"
    sl = script_language(titles)
    if sl:
        return sl, "youtube_title_script"
    for w in re.findall(r"[a-z]+", tags.lower()):
        if w in LANG_WORDS and w != "english":
            return LANG_WORDS[w], "youtube_tags"
    if codes.get("en"):
        return "English", "youtube_language"
    return None, None


def category_of(videos, channel, has_film):
    text = " ".join((v["title"] or "") + " " + " ".join(json.loads(v["yt_tags"] or "[]")) for v in videos).lower()
    for cat, ch_pat, txt_pat in CATEGORY_RULES:
        if ch_pat and re.search(ch_pat, (channel or "").lower()):
            return cat, "channel"
        if txt_pat and re.search(txt_pat, text):
            return cat, "rule"
    if has_film:
        return "Film", "rule"
    return "Non-film / Indie", "rule"


def occasions_of(videos):
    text = " ".join((v["title"] or "") + " " + " ".join(json.loads(v["yt_tags"] or "[]")) for v in videos).lower()
    return [o for o, pat in OCCASIONS if re.search(pat, text)]


def build(conn, cfg, since, until, log=print, track_ids=None):
    """Rebuild song_meta for the window's songs (or the given ones). Returns the number of songs."""
    labels = config.channel_labels(cfg)
    if track_ids is None:
        track_ids = [r[0] for r in conn.execute(
            "SELECT id FROM track WHERE first_published >= ? AND substr(first_published,1,10) <= ?", (since, until))]
    n = 0
    for tid in track_ids:
        t = conn.execute("""SELECT t.*, r.title release_title, r.album_type, r.release_date, r.label release_label
                            FROM track t LEFT JOIN release r ON r.id=t.release_id WHERE t.id=?""", (tid,)).fetchone()
        if not t:
            continue
        vids = conn.execute("""SELECT title, description, channel, asset_type, yt_tags, audio_language FROM platform_item
                               WHERE track_id=? AND platform='youtube' AND review_state IN ('auto','approved','song')
                               ORDER BY asset_type NOT IN ('video','lyric','audio'), published_at""", (tid,)).fetchall()
        full = [v for v in vids if v["asset_type"] in ("video", "lyric", "audio", "other")] or list(vids)
        rows = defaultdict(dict)  # field -> {key: (value, source, position)}

        def add(field, value, source):
            value = (value or "").strip()
            key = name_key(value) if field not in ("language", "category", "occasion", "label", "channel") else value.lower()
            if not value or not key:
                return
            cur = rows[field].get(key)
            if cur:
                if source not in cur[1].split(","):
                    rows[field][key] = (cur[0], cur[1] + "," + source, cur[2])
            else:
                rows[field][key] = (value, source, len(rows[field]))

        # Spotify first: it has the best metadata.
        sp_artists = [r[0] for r in conn.execute("""SELECT a.name FROM track_artist ta JOIN artist a ON a.id=ta.artist_id
                                                    WHERE ta.track_id=? ORDER BY ta.position""", (tid,))]
        for a in sp_artists:
            add("spotify_artist", a, "spotify")
        if t["release_title"]:
            add("spotify_album", t["release_title"], "spotify")
            f = film_from_spotify(t["title"], t["release_title"], t["album_type"])
            if f:
                add("film", f, "spotify")
        # Apple / iTunes (songs Spotify hasn't reached): album, release date, artists, genre.
        ap = conn.execute("""SELECT match_detail FROM platform_item WHERE track_id=? AND platform='apple'
                             AND review_state IN ('auto','approved') LIMIT 1""", (tid,)).fetchone()
        if ap and ap["match_detail"]:
            md = json.loads(ap["match_detail"])
            if md.get("album"):
                add("apple_album", md["album"], "apple")
                f = film_from_spotify(md["album"], md["album"], None)
                if f:
                    add("film", f, "apple")
            if md.get("release_date") and not t["release_title"]:
                add("release_date", md["release_date"], "apple")
            if md.get("genre"):
                add("genre", md["genre"], "apple")
            for a in md.get("artists") or []:
                add("apple_artist", a, "apple")
        # YouTube credits: song section first; film/video sections only fill what the song section lacks.
        by_section = defaultdict(list)
        for v in full:
            for role, name, section in parse_credits(v["description"]):
                by_section[section].append((role, name))
        song_roles = {r for r, _ in by_section["song"]}
        for section in ("song", "film", "video"):
            for role, name in by_section[section]:
                if section != "song" and role in ("music_director", "singer", "lyricist", "song_title") and role in song_roles:
                    continue
                if section == "video" and role == "director":
                    role = "video_director"
                if role in ("film", "album") and name_key(name) in (name_key(t["yt_title"] or ""), name_key(t["title"] or "")):
                    continue  # a single named after itself
                add(role, name, "youtube_credits")
        # 'Album: The Paradise' on a song with film credits (cast, director, producer) names the film.
        if "film" not in rows and "album" in rows and any(r in rows for r in ("cast", "director", "producer")):
            for k_, (v_, s_, _) in list(rows["album"].items()):
                add("film", v_, s_)
        if "film" not in rows:
            for v in full:
                f = film_from_title(v["title"])
                if f:
                    add("film", f, "youtube_title")
                    break
        # Singers: when the credits name none, Spotify's artists stand in (they are the performers).
        if "singer" not in rows and sp_artists:
            for a in sp_artists:
                add("singer", a, "spotify")
        if "singer" not in rows and "apple_artist" in rows:
            for k_, (v_, s_, _) in list(rows["apple_artist"].items()):
                add("singer", v_, "apple")
        channel = Counter(v["channel"] for v in vids).most_common(1)[0][0] if vids else None
        if channel:
            add("channel", channel, "channel")
            add("label", labels.get(channel, config.FIRST_LABEL), "channel")
        lang, src = language_of(full, channel)
        if lang:
            add("language", lang, src)
        cat, src = category_of(full, channel, "film" in rows)
        add("category", cat, src)
        for o in occasions_of(full):
            add("occasion", o, "rule")
        conn.execute("DELETE FROM song_meta WHERE track_id=?", (tid,))
        conn.executemany("INSERT INTO song_meta (track_id, field, value, value_key, source, position) VALUES (?,?,?,?,?,?)",
                         [(tid, f, v, k, s, p) for f, vals in rows.items() for k, (v, s, p) in vals.items()])
        n += 1
    conn.commit()
    filled = {f: c for f, c in conn.execute("SELECT field, count(DISTINCT track_id) FROM song_meta GROUP BY field")}
    log(f"metadata: {n} songs; with singer {filled.get('singer', 0)}, music director {filled.get('music_director', 0)}, "
        f"lyricist {filled.get('lyricist', 0)}, film {filled.get('film', 0)}, language {filled.get('language', 0)}")
    return n


META_SQL = "SELECT field, value, source FROM song_meta WHERE track_id=? ORDER BY field, position"


def for_song(conn, tid):
    """{field: [{value, key, source}]} for one song."""
    out = defaultdict(list)
    for r in conn.execute("SELECT field, value, value_key, source FROM song_meta WHERE track_id=? ORDER BY field, position", (tid,)):
        out[r["field"]].append({"value": r["value"], "key": r["value_key"], "source": r["source"]})
    return dict(out)


def flat(conn, track_ids):
    """{track_id: {field: 'a, b'}} for CSV columns."""
    out = defaultdict(dict)
    if not track_ids:
        return out
    for r in conn.execute(f"""SELECT track_id, field, group_concat(value, ', ') v FROM
                              (SELECT * FROM song_meta WHERE track_id IN ({','.join('?' * len(track_ids))})
                               ORDER BY track_id, field, position) GROUP BY track_id, field""", list(track_ids)):
        out[r["track_id"]][r["field"]] = r["v"]
    return out
