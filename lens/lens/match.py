"""Rule-based matching of Spotify tracks to YouTube videos. No AI: every score is a fixed formula
over title, artists, film/release, duration and dates, and every decision is explainable."""
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

# Words that describe the video, not the song.
NOISE = {
    "official", "music", "video", "videos", "lyric", "lyrics", "lyrical", "audio", "full", "song", "songs",
    "hd", "4k", "8k", "visualizer", "visualiser", "out", "now", "new", "latest", "hindi", "tamil", "telugu",
    "malayalam", "kannada", "punjabi", "bengali", "marathi", "gujarati", "the", "a", "film", "movie", "from",
    "feat", "ft", "featuring", "with", "starring", "original", "soundtrack", "ost", "single", "track",
    "jukebox", "vevo", "sony", "india", "version", "remastered", "remaster", "teaser", "promo", "trailer",
    "fps", "uhd", "hdr", "dolby", "atmos", "saregama", "bhojpuri", "odia",
}
# "X Version" words that still mean the original recording.
NEUTRAL_VERSIONS = {"film", "full", "official", "original", "video", "lyrical", "lyric", "audio", "song", "movie",
                    "extended", "hd", "4k", "8k", "new"}
# Words that change *which recording* it is; a mismatch on these lowers the title score.
QUALIFIERS = {
    "remix", "reprise", "unplugged", "acoustic", "instrumental", "lofi", "slowed", "reverb", "male", "female",
    "duet", "sad", "karaoke", "mashup", "cover", "live", "extended",
}
# Suffix words after " - " in a Spotify title that are not part of the song name.
SUFFIX_WORDS = NOISE | QUALIFIERS | {"title", "background", "score", "bgm", "theme", "new", "version", "from",
                                     "edit", "mix", "flip", "romantic"}
JUKEBOX_RE = re.compile(r"jukebox|mashup|non[- ]?stop|back to back|top \d+|\bmix\b|songs collection|"
                        r"full album|audio album|video album|all songs", re.I)

W_TITLE, W_ARTIST, W_FILM, W_DURATION, W_DATE = 0.55, 0.15, 0.10, 0.10, 0.10
AUTO_TOTAL, AUTO_TITLE = 0.75, 0.85      # matched without review
REVIEW_TOTAL, MIN_TITLE = 0.55, 0.60     # sent to the review queue


def fold(s):
    """Lowercase, strip accents/punctuation, collapse spaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b\d+\s*(k|fps)\b", " ", s)  # 8K, 60 FPS
    s = s.replace("&", " and ")
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s, drop_noise=True):
    return [t for t in fold(s).split() if not (drop_noise and t in NOISE)]


def asset_type(title, duration_s):
    t = (title or "").lower()
    if duration_s is not None and duration_s <= 75 or "#short" in t:
        return "short"
    if JUKEBOX_RE.search(t) or (duration_s or 0) > 15 * 60:
        return "jukebox"
    if re.search(r"teaser|promo|glimpse|trailer|motion poster|announcement|behind the (music|scenes)|making of"
                 r"|sneak peek|first look", t):
        return "teaser"
    if re.search(r"lyric", t):
        return "lyric"
    if re.search(r"\baudio\b|visuali[sz]er|full song audio", t):
        return "audio"
    if re.search(r"video|8k|4k|official", t) or duration_s is not None:
        # A full-length upload that is not a lyric, audio, short or teaser is the song's video
        # ("Song | Film | Cast" titles often omit the word "video").
        return "video"
    return "other"


def recording_qualifiers(text):
    """What kind of recording a title describes: {'remix'}, {'male'}, {'sitar-version'} ..."""
    f = fold(text)
    quals = {w for w in f.split() if w in QUALIFIERS}
    quals |= {f"{w}-version" for w in re.findall(r"([a-z]+) version", f) if w not in NEUTRAL_VERSIONS | QUALIFIERS}
    if re.search(r"only vocals|vocals only|without music", f):
        quals.add("vocals")
    if re.search(r"\bpurana\b|\bold version\b", f):
        quals.add("old-version")
    return quals


def parse_spotify_title(title):
    """'Vaathi Coming (From "Master")' -> core 'Vaathi Coming', film 'Master', qualifiers {...}."""
    film = None
    m = re.search(r"[(\[-]\s*from\s+[\"“']?([^\"”')\]]+)[\"”']?\s*[)\]]?", title, re.I)
    if m:
        film = m.group(1).strip()
        title = title[:m.start()] + title[m.end():]
    title = re.sub(r"[(\[]\s*(feat|ft|with)\b[^)\]]*[)\]]", " ", title, flags=re.I)
    quals = recording_qualifiers(title)
    core = re.sub(r"[(\[][^)\]]*[)\]]", " ", title)        # remaining bracketed text
    head, *rest = re.split(r"\s+-\s+", core)
    # Drop ' - Title Track' / ' - Remastered' style suffixes, keep ' - Homecoming'.
    if rest and all(w in SUFFIX_WORDS or w.isdigit() for w in fold(" ".join(rest)).split()):
        core = head
    return core.strip() or title.strip(), film, quals


def title_parts(video_title):
    """Candidate song-title fragments of a YouTube title, e.g. 'Song - Lyrical Video | Cast | Movie'."""
    segs = [s.strip() for s in re.split(r"\s*[|｜]\s*", video_title) if s.strip()]
    parts = []
    for seg in segs[:3]:
        clean = re.sub(r"[(\[][^)\]]*[)\]]", " ", seg)
        clean = re.sub(r"\btitle (track|song)\b", " ", clean, flags=re.I)
        parts.append(clean)
        for piece in re.split(r"\s+[-–—:]\s+|:\s+", clean):
            if piece.strip():
                parts.append(piece)
    return parts


def _tok_eq(a, b):
    return a == b or (len(a) >= 4 and len(b) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.85)


def title_score(track_core, video_title):
    t_tokens = tokens(track_core)
    if not t_tokens:
        t_tokens = tokens(track_core, drop_noise=False)
    if not t_tokens:  # e.g. a title only in Devanagari/Tamil script: nothing comparable
        return 0.0
    t_join = "".join(t_tokens)
    best = 0.0
    for part in title_parts(video_title):
        p_tokens = tokens(part)
        if not p_tokens:
            continue
        p_join = "".join(p_tokens)
        if t_join == p_join:
            return 1.0
        covered = sum(1 for t in t_tokens if any(_tok_eq(t, p) for p in p_tokens)) / len(t_tokens)
        extra = max(0, len(p_tokens) - len(t_tokens)) / max(len(p_tokens), 1)
        seq = SequenceMatcher(None, t_join, p_join).ratio()
        best = max(best, 0.6 * covered * (1 - 0.5 * extra) + 0.4 * seq)
    return round(best, 3)


def _contains_name(name, haystack_folded):
    n = fold(name)
    return len(n) >= 4 and re.search(rf"\b{re.escape(n)}\b", haystack_folded) is not None


def score(track, video):
    """track: title, artists[list], release_title, release_date, duration_ms.
    video: title, description, published_at, duration_s.
    Returns (total, detail dict)."""
    core, film, t_quals = parse_spotify_title(track["title"])
    v_fold = fold(video["title"])
    ts = title_score(core, video["title"])
    # Title tracks share their name with the film, which appears in every video of that film:
    # only accept them when the video says "title track/song" or leads with the name.
    if film and fold(core) == fold(film) and not re.search(r"title (track|song)", v_fold):
        first = tokens(re.split(r"\s*[|｜]\s*", video["title"])[0])
        if first != tokens(core):  # anything besides the name in the lead segment is another song
            ts = min(ts, 0.5)
    v_quals = recording_qualifiers(video["title"])

    hay = v_fold + " " + fold((video.get("description") or "")[:1500])
    # Artist names that are ordinary words ('Album', 'Various Artists') prove nothing.
    artists = [a for a in (track.get("artists") or []) if len(fold(a)) > 3 and fold(a) not in NOISE
               and fold(a) not in ("various artists", "album", "traditional")]
    found = sum(1 for a in artists if _contains_name(a, hay))
    artist_s = min(found / min(len(artists), 2), 1.0) if artists else 0.0

    # The film/album only counts as separate evidence when it isn't just the song name again (singles).
    film_names = [x for x in (film, track.get("release_title")) if x and fold(x) != fold(core)]
    film_s = 1.0 if any(_contains_name(f, v_fold) for f in film_names) else 0.0

    dur_s = 0.0
    if video.get("duration_s") and track.get("duration_ms"):
        d = abs(video["duration_s"] - track["duration_ms"] / 1000)
        dur_s = 1.0 if d <= 3 else 0.7 if d <= 10 else 0.3 if d <= 45 else 0.0

    date_s = 0.0
    if video.get("published_at") and track.get("release_date") and len(track["release_date"]) == 10:
        days = abs((date.fromisoformat(video["published_at"][:10]) - date.fromisoformat(track["release_date"])).days)
        date_s = 1.0 if days <= 3 else 0.6 if days <= 14 else 0.2 if days <= 60 else 0.0

    total = W_TITLE * ts + W_ARTIST * artist_s + W_FILM * film_s + W_DURATION * dur_s + W_DATE * date_s
    return round(total, 3), {"title": ts, "artist": round(artist_s, 2), "film": film_s, "duration": dur_s,
                             "date": date_s, "total": round(total, 3),
                             "variant": sorted(t_quals ^ v_quals) or None}


def decide(total, detail):
    if detail["title"] < MIN_TITLE:
        return None
    if detail.get("variant"):  # remix / male version / instrumental ... is a different recording
        return None
    corroborated = detail["artist"] > 0 or detail["film"] > 0 or (detail["duration"] >= 0.7 and detail["date"] >= 0.6)
    if not corroborated:
        return None
    if total >= AUTO_TOTAL and detail["title"] >= AUTO_TITLE:
        return "auto"
    # Exact title plus artist confirmation is enough even when the video is a longer cut or an old song.
    if detail["title"] >= 0.95 and (detail["artist"] >= 1.0 or (detail["artist"] >= 0.5 and
                                                                (detail["film"] or detail["duration"] >= 0.7))):
        return "auto"
    if total >= REVIEW_TOTAL:
        return "pending"
    return None


def block_keys(text):
    """Tokens used to pair tracks and videos cheaply before scoring (rare-ish words only)."""
    return {t for t in tokens(text) if len(t) >= 3 and not t.isdigit()}


DESCRIPTOR_RE = re.compile(
    r"\b(official|music|lyric(al)?|full|video|audio|song|visuali[sz]er|8k|4k|hd|fps|with lyrics|video with)\b", re.I)


# Words that describe a song's occasion or genre; a title part made only of these is not the song name.
GENERIC_WORDS = {"durga", "puja", "pujo", "celebration", "original", "festive", "festival", "devotional", "bhakti",
                 "bhajan", "aarti", "special", "garba", "navratri", "romantic", "sad", "party", "dance", "folk",
                 "wedding", "love", "diwali", "ganpati", "chhath", "dj", "remix", "hit", "hits", "superhit", "bengali",
                 "bangla", "bhojpuri", "haryanvi", "gujarati", "marathi", "rajasthani", "odia", "2024", "2025", "2026",
                 "mata", "rani", "bhakti", "geet", "gaan", "gaana", "anthem", "track", "new", "latest"}


def extract_song(video_title):
    """Song name + recording qualifiers from a YouTube title, e.g.
    'Odhani - Lyrical Video | Rajkummar Rao | Made In China' -> ('Odhani', set())
    'Leo - Badass Video with Lyrics | Vijay'                  -> ('Badass', set())
    'R.D. Burman, Lata Mangeshkar - Hum To Hai Chui Mui (Audio Lyrical)' -> ('Hum To Hai Chui Mui', set())
    'Rathinamo (Male Version) - Lyric Video | Saurav Srisan'  -> ('Rathinamo', {'male'})"""
    quals = recording_qualifiers(video_title)
    first = re.split(r"\s*[|｜]\s*", video_title)[0]
    first = re.sub(r"[(\[][^)\]]*[)\]]", " ", first)
    first = re.sub(r"\btitle (track|song)\b", " ", first, flags=re.I)
    first = re.sub(r"#\S+", " ", first)
    parts = [p.strip() for p in re.split(r"\s*[-–—:]\s+|\s+[-–—:]\s*", first) if p.strip()]
    if not parts:
        return None, quals

    def content(p):
        return [t for t in tokens(p) if not t.isdigit()]
    def generic(p):  # 'Durga Puja Original Song', 'New Punjabi Song 2026': occasion/genre words, not a name
        return all(t in GENERIC_WORDS for t in content(p))
    with_desc = [p for p in parts if DESCRIPTOR_RE.search(p)]
    pick = None
    for p in with_desc:  # 'Kon Varuvaan Video Song' -> the descriptor sits on the song name
        if content(p) and not generic(DESCRIPTOR_RE.sub(" ", p)):
            pick = p
            break
    if pick is None:
        # '<Artists> - <Song>' (VEVO) has commas in the first part; otherwise the song comes first.
        plain = [p for p in parts if content(p) and not generic(DESCRIPTOR_RE.sub(" ", p))] or \
            [p for p in parts if content(p)]
        if not plain:
            return None, quals
        pick = plain[1] if len(plain) > 1 and "," in plain[0] else plain[0]
    name = re.sub(r"\b\d+\s*(k|fps)\b|\bwith (\w+ )?lyrics\b", " ", pick, flags=re.I)
    name = DESCRIPTOR_RE.sub(" ", name)
    kept = [w for w in name.split() if fold(w) not in NOISE]
    name = " ".join(kept) if kept else name
    name = re.sub(r"\b(version|remix|reprise|unplugged|lofi|male|female|new)\b", " ", name, flags=re.I)
    # Keep the vowel signs of Indian scripts (\w alone drops them: "हे नाम रे" would become "ह न म र").
    name = re.sub(r"\s+", " ", re.sub(r"[^\w\s'&.\u0900-\u0dff]", " ", name)).strip(" .")
    return (name or None), quals


def song_key(name, quals):
    k = " ".join(t for t in tokens(name) if t)
    return (k + ("|" + ",".join(sorted(quals)) if quals else "")) if k else None
