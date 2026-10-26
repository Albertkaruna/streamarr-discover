"""Music: pure helpers (no I/O) — name cleaning, taste from Tunarr + Jellyfin, API-response parsing, ranking.
Kept separate so tests can run offline."""
import math
import re
import unicodedata

# tags in the Tamil part of the library are full of download-site junk ("Hariharan - MassTamilan.com",
# "EelamMp3.Com & TamilGood.Com Free High Quality ...") — strip it before treating anything as an artist
_SITE = re.compile(r"\(?\b(?:www\.)?[\w-]+\.(?:com|in|net|org|co|info)\b\)?|\bmass\s*tamil\w*|\beelam\w*|\btamilgood\w*|"
                   r"\b5eli\b|\bisaimini\w*|\btamilwire\w*|\bstarmusiq\w*", re.I)
_JUNK = re.compile(r"\bfree\b|download|kbps|\bmp3\b|high quality|full album|various|unknown|\btracks?\b|\brar\b|\bzip\b|original motion|"
                   r"soundtrack|\bost\b|^\W*$|^(tamil|hindi|telugu|songs?|music|hits|singles|mix)$", re.I)
_SPLIT = re.compile(r"\s*(?:/|,|;|&|\+|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\s+x\s+|\s+and\s+|\s+vs\b\.?)\s*", re.I)
_GENERIC_DIR = re.compile(r"hits|songs?|collection|mix|singles|best|top|romantic|evergreen|manual|melod|golden|"
                          r"\d|playlist|album|incomplete|various|^\.", re.I)
SKIP_SONG = re.compile(r"remix|karaoke|instrumental|\b8d\b|slowed|reverb|cover|\bbgm\b|ringtone|lo-?fi|"
                       r"theme music|jukebox|mashup|medley|\bdialogue|\(live\b|\blive at\b", re.I)
_FILM = re.compile(r"\(from\s*[\"'“‘]", re.I)       # 'Kokkorakko (From "Gilli")' — an Indian film song
INDIAN = {"ta": "Tamil", "in": "Indian"}


def norm(s):
    """Case/punctuation/accent-insensitive key ('S. P. Balasubrahmanyam' == 's p balasubrahmanyam')."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[\W_]+", " ", s).strip()


def tight(s):
    return norm(s).replace(" ", "")


def title_core(t):
    """'Mundhinam (From "Vaaranam Aayiram") [Remastered] - Live' → 'mundhinam' — for owned-song matching."""
    t = re.split(r"\s+-\s+|\(|\[|\bfeat\b|\bft\b\.", t or "", maxsplit=1, flags=re.I)[0]
    return tight(t)


def split_artists(s):
    """One tag value → clean artist names (junk dropped). 'AC/DC' stays whole; 'A/B' and 'A, B & C' split."""
    s = _SITE.sub(" ", s or "")
    s = re.sub(r"\b(\w{1,2})/(\w{1,2})\b", "\\1\u2215\\2", s)          # protect AC/DC-style names
    out = []
    for p in _SPLIT.split(s):
        p = re.sub(r"\s+", " ", p.replace("\u2215", "/")).strip(" -:.|()[]'\"")
        if 2 < len(p) <= 40 and len(p.split()) <= 5 and not _JUNK.search(p) and not re.search(r"\d{3,}", p):
            out.append(p)
    return list(dict.fromkeys(out))


def lang_of_dir(top):
    t = (top or "").lower()
    if "tamil" in t:
        return "ta"
    if any(w in t for w in ("hindi", "bollywood", "telugu", "malayalam", "kannada", "indian")):
        return "in"
    return "en"


def lang_of_genre(g):
    g = (g or "").lower()
    if "tamil" in g:
        return "ta"
    if any(w in g for w in ("indian", "bollywood", "telugu", "malayalam", "kannada", "hindi", "punjabi")):
        return "in"
    return "en"


def _add(acc, name, w, lang=None, tunarr=False):
    k = norm(name)
    if not k:
        return
    a = acc.setdefault(k, {"name": name, "w": 0.0, "n": 0, "lang": {}, "tunarr": False})
    a["tunarr"] |= tunarr
    a["w"] += w
    a["n"] += 1
    if lang:
        a["lang"][lang] = a["lang"].get(lang, 0) + w


def taste(tunarr_state, jf_audio, jf_fav_artists=()):
    """→ (artists {norm: {name, w, n, lang, tunarr}}, owned song keys {title_core}).
    Tunarr: each track counts for its artist(s), 'done' (actually downloaded) ×1.5; label channels (artist_wrong)
    don't name an artist. Jellyfin: each track + play count + favourite; the folder (Music/Tamil/Harris Jayaraj/…)
    names the composer, which the messy tags often don't."""
    acc, owned = {}, set()
    for pl in (tunarr_state or {}).get("playlists", {}).values():
        for t in pl.get("tracks", {}).values():
            owned.add(title_core(t.get("title")))
            if t.get("artist_wrong"):
                continue
            owned.add(title_core(t.get("artist")))       # uploader titles are sometimes "Song - Artist"
            for a in split_artists(t.get("artist")):
                _add(acc, a, 1.5 if t.get("status") == "done" else 1.0, tunarr=True)
    for it in jf_audio or []:
        ud = it.get("UserData") or {}
        w = 1.0 + math.log1p(ud.get("PlayCount") or 0) + (2.0 if ud.get("IsFavorite") else 0)
        owned.add(title_core(it.get("Name")))
        parts = (it.get("Path") or "").replace("\\", "/").split("/")
        low = [p.lower() for p in parts]
        i = low.index("music") + 1 if "music" in low else max(0, len(parts) - 3)
        dirs = parts[i:-1]                                  # folders between the library root and the file
        lang = lang_of_dir(dirs[0] if dirs else "")
        names = [x.get("Name") if isinstance(x, dict) else x for x in it.get("ArtistItems") or it.get("Artists") or []]
        for raw in names:
            for a in split_artists(raw):
                _add(acc, a, w, lang)
        for d in dirs[1:3]:                                 # composer folders, e.g. Tamil/Collection/Vidhyasagar
            if not _GENERIC_DIR.search(d) and split_artists(d) == [d.strip()]:
                _add(acc, d.strip(), 0.7 * w, lang)
                break
    for a in jf_fav_artists:
        for x in split_artists(a):
            _add(acc, x, 4.0)
    for a in acc.values():                                  # 400 tracks shouldn't be 400× one song
        a["w"] = round(math.log1p(a["w"]) * (1 + 0.15 * min(a["n"], 10) ** 0.5), 4)
    owned.discard("")
    return acc, owned


def main_lang(votes, default="en"):
    return max(votes, key=votes.get) if votes else default


def is_owned(title, owned):
    c = title_core(title)
    return len(c) >= 4 and c in owned


# ---------------- API response parsing ----------------
def pick_mb(results, query, lang=None):
    """Best MusicBrainz artist hit: exact name/alias match beats raw score ('Ilayaraja' → Ilaiyaraaja, not
    'Anthakudi Ilayaraja'); Indian artists preferred for Tamil seeds."""
    q, best, best_s = tight(query), None, 0
    for r in results or []:
        names = [r.get("name", ""), r.get("sort-name", "")] + [a.get("name", "") for a in r.get("aliases") or []]
        s = (r.get("score") or 0) + (30 if q in {tight(n) for n in names} else 0)
        s += 8 if lang in ("ta", "in") and r.get("country") == "IN" else 0
        if s > best_s:
            best, best_s = r, s
    if best and best_s >= 90:
        return {"mbid": best["id"], "name": best["name"], "country": best.get("country")}
    return None


def pick_dz(results, *queries):
    qs = {tight(q) for q in queries if q}
    for r in results or []:
        if tight(r.get("name")) in qs:
            return {"id": r["id"], "name": r["name"], "fans": r.get("nb_fan") or 0, "pic": r.get("picture_medium")}
    return None


def parse_lb_similar(body, limit=25):
    """labs.api.listenbrainz.org similar-artists → [(name, mbid, sim 0..1)], sim relative to the top hit."""
    rows = body or []
    if rows and isinstance(rows[0], dict) and "data" in rows[0]:   # older response shape
        rows = rows[0]["data"]
    rows = [r for r in rows if isinstance(r, dict) and r.get("name")][:limit]
    top = max([r.get("score") or 0 for r in rows] + [1])
    return [(r["name"], r.get("artist_mbid"), (r.get("score") or 0) / top) for r in rows]


def parse_dz_related(body, limit=20):
    rows = (body or {}).get("data") or []
    return [(r["name"], 1 - i / (limit + 5), r.get("nb_fan") or 0, r.get("picture_medium"))
            for i, r in enumerate(rows[:limit]) if r.get("name")]


def parse_lastfm_similar(body, limit=25):
    rows = ((body or {}).get("similarartists") or {}).get("artist") or []
    return [(r["name"], r.get("mbid") or None, float(r.get("match") or 0)) for r in rows[:limit] if r.get("name")]


def parse_itunes(body, artist=None):
    """iTunes song search → song dicts; with artist, only tracks credited to them (search is fuzzy)."""
    out, seen = [], set()
    a = norm(artist) if artist else None
    for r in (body or {}).get("results") or []:
        if r.get("wrapperType") != "track" or not r.get("trackName"):
            continue
        if a and a not in norm(r.get("artistName")):
            continue
        if SKIP_SONG.search(r["trackName"]) or title_core(r["trackName"]) in seen:
            continue
        seen.add(title_core(r["trackName"]))
        album = re.sub(r"\s*\((Original Motion Picture Soundtrack|Original Soundtrack|OST)\)", "",
                       r.get("collectionName") or "", flags=re.I)
        out.append({"title": r["trackName"], "artist": r.get("artistName") or artist or "", "album": album,
                    "year": (r.get("releaseDate") or "")[:4], "lang": _song_lang(r),
                    "art": (r.get("artworkUrl100") or "").replace("100x100bb", "160x160bb"),
                    "preview": r.get("previewUrl"), "url": r.get("trackViewUrl")})
    return out


def _song_lang(r):
    lang = lang_of_genre(r.get("primaryGenreName"))
    return "in" if lang == "en" and _FILM.search(r.get("trackName") or "") else lang


def artist_lang(song_langs, fallback="en"):
    """An artist's language from their songs: some Tamil → Tamil (composers work in several languages)."""
    langs = [x for x in song_langs if x]
    if not langs:
        return fallback
    if langs.count("ta") >= 0.3 * len(langs):
        return "ta"
    return "in" if sum(x in INDIAN for x in langs) >= 0.3 * len(langs) else "en"


def lang_factor(lang, shares):
    """Down-weight languages you barely have (Hindi/Telugu neighbours of Tamil composers) — 1.0 for yours."""
    return 0.55 + 0.45 * min(1.0, 2 * shares.get(lang, 0))


def parse_lb_top(body, limit=10):
    out = []
    for r in (body or [])[:limit * 2] if isinstance(body, list) else []:
        if r.get("recording_name") and not SKIP_SONG.search(r["recording_name"]):
            art = f"https://coverartarchive.org/release/{r['release_mbid']}/front-250" if r.get("caa_id") else ""
            out.append({"title": r["recording_name"], "artist": r.get("artist_name") or "", "album": r.get("release_name") or "",
                        "year": "", "lang": None, "art": art, "preview": None, "url": None})
    return out[:limit]


# ---------------- ranking ----------------
def nope_key(kind, artist, title=""):
    return f"a:{tight(artist)}" if kind == "artist" else f"s:{tight(artist)}|{title_core(title)}"


def score_artists(cands, seeds):
    """cands {norm: {name, support{seed: contrib}, fans, lang_votes}} → sorted list with score + because.
    support from several of your artists ≫ one strong link; tiny-fanbase artists get a sanity penalty."""
    top = max([sum(c["support"].values()) for c in cands.values()] + [1e-9])
    out = []
    for k, c in cands.items():
        sup = sum(c["support"].values()) / top
        n = len(c["support"])
        fans = c.get("fans")
        pop = min(1.0, math.log10(fans + 1) / 6) if fans is not None else 0.4
        s = 0.72 * sup ** 0.8 + 0.13 * min(n, 4) / 4 + 0.15 * pop
        if fans is not None and fans < 500:
            s *= 0.7
        because = [seeds[x]["name"] for x, _ in sorted(c["support"].items(), key=lambda kv: -kv[1]) if x in seeds]
        out.append({**c, "key": k, "score": round(s, 4), "because": because[:3]})
    out.sort(key=lambda c: -c["score"])
    return out


def balance(items, target_ta, limit, lang=lambda x: x.get("lang")):
    """Greedy interleave so Tamil/Indian picks make up ~target_ta of the list (if there are enough of them)."""
    pool = {"in": [x for x in items if lang(x) in INDIAN], "en": [x for x in items if lang(x) not in INDIAN]}
    out, n_in = [], 0
    while len(out) < limit and (pool["in"] or pool["en"]):
        want_in = n_in < target_ta * (len(out) + 1)
        side = "in" if (want_in and pool["in"]) or not pool["en"] else "en"
        out.append(pool[side].pop(0))
        n_in += side == "in"
    return out


def why_text(because, extra=""):
    if extra:
        return extra
    if not because:
        return ""
    return "Similar to " + (" and ".join(because[:2]) if len(because) <= 2 else f"{because[0]}, {because[1]} and {because[2]}")
