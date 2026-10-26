"""Cross-media (#13): "Because you loved X…" → one book, one album, one documentary/podcast per loved title.

One Gemini call per refresh (cached while your top titles are unchanged) suggests them; every suggestion must
resolve on a real catalogue (Open Library, MusicBrainz, TMDB, iTunes) or it's dropped — that's our guard against
invented titles. Without Gemini: the book a title was adapted from + its soundtrack.
Shown at the top of the Books tab and as a collapsed row on Movies (templates read rows() via fx.crossmedia)."""
import asyncio
import json
import logging
import time
import urllib.parse

from discover import features as fx
from discover import store
from discover.features import books_sources as S

log = logging.getLogger("discover")
TOP = 8
_ready = set()


def _db():
    con = store.db()
    if id(con) not in _ready:
        con.execute("CREATE TABLE IF NOT EXISTS cross_picks (pos INTEGER PRIMARY KEY, data TEXT)")
        _ready.add(id(con))
    return con


def save(rows):
    with _db() as c:
        c.execute("DELETE FROM cross_picks")
        c.executemany("INSERT INTO cross_picks VALUES (?,?)", [(i, json.dumps(r)) for i, r in enumerate(rows)])


def rows():
    """Stored rows for templates: [{loved, year, kind, poster, picks: [{type, title, by, why, url, cover, ...}]}]."""
    try:
        return [json.loads(d) for (d,) in _db().execute("SELECT data FROM cross_picks ORDER BY pos")]
    except Exception:                       # never break the Movies page over an extra
        log.exception("cross-media rows")
        return []


def _prompt(seeds):
    loved = "\n".join(f"- {it['title']} ({it.get('year', '')}, {'film' if it['kind'] == 'movie' else 'series'})"
                      for it, _ in seeds)
    return f"""For each film/series below that one person loved, suggest things beyond the screen:
one book, one album (a soundtrack or an album that matches its mood), and one documentary OR podcast.
Only real, findable works (exact title, usual author/artist name). Not the book it was adapted from.

Loved:
{loved}

"why": max 14 words, concrete, about the connection to the loved title.
Return JSON: {{"items": [{{"loved": "<title exactly as given>",
  "book": {{"title": "", "author": "", "why": ""}},
  "album": {{"title": "", "artist": "", "why": ""}},
  "watch": {{"type": "documentary|podcast", "title": "", "why": ""}}}}]}}"""


def parse(out, seeds):
    """Gemini JSON → {seed key: suggestion dict}. Matches 'loved' back by title, else by position."""
    lst = out.get("items", []) if isinstance(out, dict) else out if isinstance(out, list) else []
    by_title = {S.norm(it["title"]): it["key"] for it, _ in seeds}
    res = {}
    for i, x in enumerate(lst):
        if not isinstance(x, dict):
            continue
        key = by_title.get(S.norm(x.get("loved"))) or (seeds[i][0]["key"] if i < len(seeds) else None)
        if key and key not in res:
            res[key] = {f: x.get(f) if isinstance(x.get(f), dict) else {} for f in ("book", "album", "watch")}
    return res


def _book_pick(b, why):
    q = urllib.parse.quote_plus(f"{b['title']} {b['author']}")
    return {"type": "book", "icon": "📖", "title": b["title"], "by": b["author"], "year": b.get("year") or "",
            "why": why, "url": f"https://openlibrary.org/works/{b['id']}", "alt": f"https://www.goodreads.com/search?q={q}",
            "cover": f"https://covers.openlibrary.org/b/id/{b['cover']}-M.jpg" if b.get("cover") else "", "id": b["id"]}


async def _resolve(http, it, sug):
    """One loved title + Gemini's suggestion (maybe empty) → list of resolved picks."""
    book, album, watch = sug.get("book") or {}, sug.get("album") or {}, sug.get("watch") or {}
    b = await S.ol_find(http, str(book["title"]), str(book.get("author") or "")) if book.get("title") else None
    why_b = str(book.get("why") or "")[:160]
    if not b and S.based_on(it):                     # fallback: the book it came from
        b, why_b = await S.based_on_book(http, it), f"The book {it['title']} was adapted from."
    a = await S.mb_album(http, str(album["title"]), str(album.get("artist") or "")) if album.get("title") else None
    why_a = str(album.get("why") or "")[:160]
    if not a:                                        # fallback: its own soundtrack
        y = str(it.get("year") or "")
        a = await S.mb_album(http, it["title"], soundtrack=True, min_year=int(y) - 1 if y.isdigit() else None)
        why_a = f"The soundtrack of {it['title']}."
    w, why_w, kind = None, str(watch.get("why") or "")[:160], str(watch.get("type") or "").lower()
    if watch.get("title"):
        w = await (S.podcast(http, str(watch["title"])) if kind == "podcast" else S.tmdb_doc(http, str(watch["title"])))
    out = []
    if b:
        out.append(_book_pick(b, why_b))
    if a:
        out.append({"type": "album", "icon": "🎵", "title": a["title"], "by": a["artist"], "year": a["year"],
                    "why": why_a, "url": a["url"], "cover": a["cover"]})
    if w:
        out.append({"type": kind if kind == "podcast" else "documentary", "icon": "🎙️" if kind == "podcast" else "🎞️",
                    "title": w["title"], "by": w.get("by") or w.get("year") or "", "why": why_w, "url": w["url"],
                    "cover": w.get("cover") or ""})
    return out


async def compute(http, items, seeds_w):
    seeds = S.top_seeds(items, seeds_w, TOP)
    if not seeds:
        return []
    sugs = parse(await S.gemini_cached(http, "crossmedia", _prompt(seeds)) or {}, seeds)
    resolved = await asyncio.gather(*[_resolve(http, it, sugs.get(it["key"], {})) for it, _ in seeds],
                                    return_exceptions=True)
    out = []
    for (it, _), picks in zip(seeds, resolved):
        if isinstance(picks, Exception):
            log.warning("cross-media %s: %s", it["key"], picks)
            continue
        if picks:
            out.append({"loved": it["title"], "key": it["key"], "year": it.get("year", ""), "kind": it["kind"],
                        "poster": it.get("poster"), "picks": picks})
    return out


async def post_refresh(ctx):
    got = await compute(ctx["http"], ctx["items"], ctx["seeds_w"])
    if got or not rows():
        save(got)
    store.meta_put("cross:info", {"ts": time.time(), "n": len(got)})
    log.info("cross-media: %d rows", len(got))


if "/static/books.css" not in fx.CSS:
    fx.CSS.append("/static/books.css")
fx.PAGE_TOP.append("features/cross_top.html")
fx.POST_REFRESH.append(post_refresh)
