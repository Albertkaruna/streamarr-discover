"""Lookups shared by the Books and Cross-media features: Open Library, Hardcover, MusicBrainz, iTunes podcasts,
plus a cached Gemini call. No feature registration here — just helpers (pure ones are unit-tested).

Every GET goes through Http.req's SQLite cache for ≥14 days: books, albums and podcasts don't change daily, and
these are free community APIs we want to stay polite to."""
import asyncio
import hashlib
import logging
import math
import re
import time
import unicodedata

from discover import clients, config, store

log = logging.getLogger("discover")
DAY = 86400
CACHE = 21 * DAY
UA = {"User-Agent": "StreamarrDiscover/1.0 (self-hosted personal book recommender)"}
OL = "https://openlibrary.org"
OL_FIELDS = ("key,title,author_name,first_publish_year,cover_i,edition_count,readinglog_count,"
             "ratings_average,ratings_count,subject_key")


# ---------------- text helpers ----------------
def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"^(the|a|an)\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def short_title(t):
    """'Dune: Part Two' / 'Dune (Novel)' → 'dune' — subtitles differ between film, book and edition."""
    return norm(re.split(r"[:(\[]", str(t or ""))[0])


def same_author(a, b):
    """Last name + first initial: 'J.R.R. Tolkien' == 'John Ronald Reuel Tolkien', 'Weir, Andy' == 'Andy Weir'."""
    def parts(n):
        n = str(n or "")
        if "," in n:                                   # "Last, First"
            last, first = n.split(",", 1)
            n = f"{first} {last}"
        p = norm(n).split()
        return (p[0][:1], p[-1]) if p else ("", "")
    (fa, la), (fb, lb) = parts(a), parts(b)
    return bool(la) and la == lb and (not fa or not fb or fa == fb)


def latin(s):
    """Mostly Latin letters? A Japanese-script title on a card is unreadable here, even if an English edition exists."""
    letters = [c for c in str(s) if c.isalpha()]
    return not letters or sum(c.isascii() for c in letters) / len(letters) >= 0.6


OMNIBUS = re.compile(r"\b(box(ed)? set|collection|trilogy|omnibus|books? \d+ ?- ?\d+)\b", re.I)


def book(d):
    """Open Library search doc → our compact book dict (id = work id, e.g. OL45883W)."""
    return {"id": d["key"].rsplit("/", 1)[-1], "title": d.get("title") or "",
            "author": (d.get("author_name") or [""])[0], "year": d.get("first_publish_year") or "",
            "cover": d.get("cover_i"), "editions": d.get("edition_count") or 0,
            "readers": d.get("readinglog_count") or 0, "rating": round(d.get("ratings_average") or 0, 1),
            "subjects": (d.get("subject_key") or [])[:30]}


def best_match(docs, title, author="", before=None, authors_any=()):
    """Pick the doc that is really *this* book, not a study guide or a same-titled stranger.
    author / authors_any: required author match when given (film writers for 'based on'); before: published
    no later than this year (the book came before its film)."""
    want, want_short = norm(title), short_title(title)
    best, best_s = None, 0
    for d in docs:
        t = d.get("title") or ""
        if (" / " in t or OMNIBUS.search(t)) and not OMNIBUS.search(title):
            continue                                   # 'Hunger Games / Catching Fire / Mockingjay' box sets
        if norm(t) == want:
            s = 3
        elif short_title(t) == want_short and want_short:
            s = 2
        elif want and norm(t).startswith(want):
            s = 1
        else:
            continue
        names = d.get("author_name") or []
        if author and not any(same_author(author, n) for n in names):
            continue
        if authors_any and not any(same_author(a, n) for a in authors_any for n in names):
            continue
        y = d.get("first_publish_year")
        if before and y and y > before:
            continue
        s += math.log1p(d.get("readinglog_count") or 0) / 4 + math.log1p(d.get("edition_count") or 0) / 6
        if s > best_s:
            best, best_s = d, s
    return best


# ---------------- Open Library ----------------
async def ol_search(http, limit=10, **params):
    r = await http.req("GET", f"{OL}/search.json", params={**params, "fields": OL_FIELDS, "limit": str(limit)},
                       headers=UA, cache=CACHE, conc=3)
    return [d for d in (r or {}).get("docs", []) if d.get("key") and d.get("title")]


async def ol_find(http, title, author="", before=None, authors_any=()):
    """Title (+author) → one book or None. Retries without the author: Gemini and Hardcover spell names
    differently than Open Library sometimes, and best_match still checks the author on the way out."""
    try:
        docs = await ol_search(http, 8, title=title, **({"author": author} if author else {}))
        if not docs and author:
            docs = await ol_search(http, 8, title=title)
    except Exception as ex:
        log.warning("open library search failed: %s", ex)
        return None
    d = best_match(docs, title, author, before, authors_any)
    return book(d) if d else None


async def ol_subject(http, slug, limit=30):
    """Popular English books on a subject. search.json sorted by reading logs beats /subjects/{s}.json, whose
    edition-count order is all public-domain classics (Frankenstein tops 'science_fiction')."""
    try:
        docs = await ol_search(http, limit, q=f"subject_key:{slug} language:eng", sort="readinglog")
    except Exception as ex:
        log.warning("open library subject %s failed: %s", slug, ex)
        return []
    return [book(d) for d in docs if latin(d["title"]) and " / " not in d["title"] and not OMNIBUS.search(d["title"])]


async def ol_author_works(http, name, limit=8):
    try:
        docs = await ol_search(http, limit, author=name, sort="readinglog")
    except Exception as ex:
        log.warning("open library author failed: %s", ex)
        return []
    return [book(d) for d in docs if any(same_author(name, n) for n in d.get("author_name") or [])]


# ---------------- Hardcover (optional: your Read / Want to Read shelves) ----------------
HC_STATUS = {1: "want", 2: "reading", 3: "read", 5: "dnf"}
HC_QUERY = """{ me { user_books(where: {status_id: {_in: [1, 2, 3, 5]}}, order_by: {updated_at: desc}, limit: 300) {
  status_id rating book { title release_year contributions(limit: 1) { author { name } } } } } }"""


def hardcover_token():
    t = config.env("HARDCOVER_TOKEN")
    return t[7:].strip() if t.lower().startswith("bearer ") else t


def parse_hardcover(r):
    """GraphQL answer → [{title, author, year, status, w}]; w = taste weight (rated 5 ≫ 3, DNF negative)."""
    me = (r or {}).get("data", {}).get("me")
    me = me[0] if isinstance(me, list) and me else me or {}
    out = []
    for ub in me.get("user_books") or []:
        b = ub.get("book") or {}
        status = HC_STATUS.get(ub.get("status_id"))
        if not (b.get("title") and status):
            continue
        contrib = (b.get("contributions") or [{}])[0] or {}
        rating = ub.get("rating")
        w = {"read": 1.0 if rating is None else (rating - 2.5) / 2.5 + 0.2, "reading": 0.8,
             "want": 0.5, "dnf": -0.8}[status]
        out.append({"title": b["title"], "author": (contrib.get("author") or {}).get("name") or "",
                    "year": b.get("release_year"), "status": status, "w": round(w, 2)})
    return out


async def hardcover_shelf(http):
    tok = hardcover_token()
    if not tok:
        return []
    try:   # POST isn't cached by Http.req — one small call per refresh is fine
        r = await http.req("POST", "https://api.hardcover.app/v1/graphql", json_body={"query": HC_QUERY},
                           headers={**UA, "authorization": f"Bearer {tok}", "content-type": "application/json"},
                           conc=1, tries=2)
    except Exception as ex:
        log.warning("hardcover unavailable: %s", ex)
        return []
    return parse_hardcover(r)


# ---------------- MusicBrainz (≤1 request/second, per their rules) ----------------
MB = "https://musicbrainz.org/ws/2/release-group"


def _lucene(s):
    return re.sub(r'["\\]', " ", str(s or "")).strip()


def pick_release_group(groups, title, min_year=None):
    """Search hits → the real album. Raw score ties a lot (fan uploads score 100 too), so also weigh how many
    releases it has, Album over Single, and the title has to start with what we asked for."""
    want, best, best_s = short_title(title), None, 0
    for g in groups:
        if (g.get("score") or 0) < 60 or not want or short_title(g.get("title")) != want:
            continue                       # 'Night Train' must not land on 'Night Train to Lisbon'
        y = (g.get("first-release-date") or "")[:4]
        if min_year and y.isdigit() and int(y) < min_year:
            continue                       # an older namesake, not this film's score
        s = g["score"] / 100 + 0.25 * math.log1p(g.get("count") or 0)
        s += 0.3 if g.get("primary-type") == "Album" else 0
        if s > best_s:
            best, best_s = g, s
    if not best or best_s < 1.0:
        return None
    gid = best["id"]
    return {"title": best["title"], "artist": ((best.get("artist-credit") or [{}])[0]).get("name", ""),
            "year": (best.get("first-release-date") or "")[:4], "url": f"https://musicbrainz.org/release-group/{gid}",
            "cover": f"https://coverartarchive.org/release-group/{gid}/front-250"}


async def mb_album(http, title, artist="", soundtrack=False, min_year=None):
    q = f'releasegroup:"{_lucene(title)}"'
    q += f' AND artist:"{_lucene(artist)}"' if artist else ""
    q += " AND secondarytype:soundtrack" if soundtrack else ""
    params = {"query": q, "fmt": "json", "limit": "10"}
    key = MB + "?" + str(sorted(params.items()))       # same key Http.req uses → a cache hit skips the wait
    r = store.cache_get(key, CACHE)
    if r is None:
        if not hasattr(http, "_mb_lock"):
            http._mb_lock, http._mb_last = asyncio.Lock(), 0.0
        async with http._mb_lock:
            await asyncio.sleep(max(0.0, 1.1 - (time.monotonic() - http._mb_last)))
            try:
                r = await http.req("GET", MB, params=params, headers=UA, cache=CACHE, conc=1, tries=3)
            except Exception as ex:
                log.warning("musicbrainz failed: %s", ex)
                return None
            finally:
                http._mb_last = time.monotonic()
    return pick_release_group((r or {}).get("release-groups") or [], title, min_year)


# ---------------- iTunes podcast search (free, no key) ----------------
async def podcast(http, title):
    try:
        r = await http.req("GET", "https://itunes.apple.com/search", params={"media": "podcast", "term": title,
                           "limit": "3"}, headers=UA, cache=CACHE, conc=2)
    except Exception as ex:
        log.warning("itunes failed: %s", ex)
        return None
    res = (r or {}).get("results") or []
    hit = next((p for p in res if short_title(p.get("collectionName")) == short_title(title)), res[0] if res else None)
    if not hit:
        return None
    return {"title": hit.get("collectionName", ""), "by": hit.get("artistName", ""),
            "url": hit.get("collectionViewUrl", ""), "cover": hit.get("artworkUrl100", "")}


async def tmdb_doc(http, title):
    """A documentary (or any film/series) by title via TMDB search — prefer Documentary-genre hits (id 99)."""
    if not config.TMDB_KEY:
        return None
    try:
        r = await clients.tmdb(http, "/search/multi", cache=CACHE, query=title)
    except Exception as ex:
        log.warning("tmdb search failed: %s", ex)
        return None
    res = [x for x in (r or {}).get("results", []) if x.get("media_type") in ("movie", "tv")]
    res.sort(key=lambda x: (99 not in (x.get("genre_ids") or []), short_title(x.get("title") or x.get("name"))
                            != short_title(title)))
    if not res:
        return None
    x = res[0]
    return {"title": x.get("title") or x.get("name"), "year": (x.get("release_date") or x.get("first_air_date") or "")[:4],
            "url": f"https://www.themoviedb.org/{x['media_type']}/{x['id']}",
            "cover": f"/img/w154{x['poster_path']}" if x.get("poster_path") else ""}


# ---------------- Gemini, answer cached by prompt ----------------
async def gemini_cached(http, tag, prompt, max_age=7 * DAY):
    """One JSON call, reused while your taste (= the prompt) is unchanged: the free tier allows only a few
    generate calls a day and the movie/TV re-rank needs those. Returns None without a key or on failure."""
    if not config.GEMINI_KEY:
        return None
    key = f"gemini:{tag}:{hashlib.sha1(prompt.encode()).hexdigest()}"
    hit = store.cache_get(key, max_age)
    if hit is not None:
        return hit
    try:
        out = await clients.gemini_json(http, prompt)
    except Exception as ex:
        log.warning("gemini %s failed: %s", tag, ex)
        return None
    if out:
        store.cache_put(key, out)
    return out


def top_seeds(items, seeds_w, n):
    """Your strongest positive movie/TV seeds that we have metadata for, strongest first: [(item, w)]."""
    return sorted([(items[k], w) for k, w in seeds_w.items() if w > 0 and k in items], key=lambda x: -x[1])[:n]


BASED_ON = ("based on novel or book", "based on young adult novel", "based on short story",
            "based on memoir or autobiography", "based on novella")


def based_on(item):
    return any(k in BASED_ON for k in item.get("keywords") or [])


async def based_on_book(http, item):
    """The book a film/show was adapted from. Same-titled strangers are common ('The Martian' by du Maurier),
    so when TMDB lists writers the book's author must be one of them (the novelist is credited as 'Novel')."""
    writers = [n for role, n in item.get("people") or [] if role in ("writer", "creator")]
    year = str(item.get("year") or "")
    before = int(year) if year.isdigit() else None
    tries = [item["title"]]
    if ":" in item["title"]:              # 'The Hunger Games: Catching Fire' → 'Catching Fire', then 'The Hunger Games'
        head, tail = item["title"].split(":", 1)
        tries += [tail.strip(), head.strip()]
    for t in tries:
        b = await ol_find(http, t, before=before, authors_any=writers)
        if b:
            return b
    return None
