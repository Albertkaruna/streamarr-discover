"""Search everything that exists (TMDB, Open Library, iTunes), not just your library — and mark what you already
have: movies/shows via the normal card status (In library / Downloading / Wanted), books via Chaptarr, songs via
your Jellyfin music + Tunarr."""
import asyncio
import logging
import time
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse

from discover import clients, store
from discover import features as fx
from discover.features import books_sources as bs
from discover.features import music_core as mc

log = logging.getLogger("discover")
KINDS = ("all", "movie", "tv", "book", "music")
DAY = 86400
_chap = {"ts": 0, "books": {}}


# ---------------- movies + shows ----------------
async def search_titles(http, q, kind):
    """TMDB multi search → hydrated items (cached). A person in the results brings their best-known titles."""
    r = await clients.tmdb(http, "/search/multi", cache=DAY, query=q, include_adult="false")
    keys, people = [], []
    for x in r.get("results", []):
        if x.get("media_type") in ("movie", "tv"):
            keys.append((x["media_type"], x["id"]))
        elif x.get("media_type") == "person":
            keys += [(k["media_type"], k["id"]) for k in x.get("known_for", []) if k.get("media_type") in ("movie", "tv")]
            if len(people) < 6 and not x.get("adult"):
                people.append({"id": x["id"], "name": x.get("name") or "", "photo": x.get("profile_path"),
                               "dept": x.get("known_for_department") or ""})
    http.people = people          # People row for run(); kept off the return value so the titles list stays simple
    keys = [k for k in dict.fromkeys(keys) if kind in ("all", k[0])][:18]
    items = await asyncio.gather(*[clients.tmdb_item(http, k, i) for k, i in keys], return_exceptions=True)
    return [it for it in items if isinstance(it, dict) and not it.get("adult")]


# ---------------- books ----------------
async def chaptarr_books(http):
    """Chaptarr library, normalised title → state ("lib" has files / "want" monitored). Cached a minute."""
    from discover.features import chaptarr
    if not chaptarr.KEY or time.time() - _chap["ts"] < 60:
        return _chap["books"]
    try:
        rows = await chaptarr._api(http, "/book")
    except Exception as ex:
        log.warning("search: chaptarr books: %s", ex)
        return _chap["books"]
    out = {}
    for b in rows or []:
        k = bs.norm(bs.short_title(b.get("title") or ""))
        state = "lib" if (b.get("statistics") or {}).get("bookFileCount") or b.get("hasFiles") else \
            "want" if b.get("monitored") else ""
        if state and out.get(k) != "lib":
            out[k] = state
    _chap.update(ts=time.time(), books=out)
    return out


async def search_books(http, q):
    from discover.features import books
    docs = await bs.ol_search(http, 12, q=q)
    have, wants = await chaptarr_books(http), {w["id"]: w for w in books.wants()}
    out = []
    for d in docs:
        b = bs.book(d)
        st = have.get(bs.norm(bs.short_title(b["title"])))
        b.update(wanted=b["id"] in wants, why="", tag="✅ In library" if st == "lib" else "⏳ In Chaptarr" if st == "want" else "")
        out.append(b)
    return out


# ---------------- music ----------------
async def search_songs(http, q, base):
    # direct (not music_sources.get): that one stops new lookups once the nightly refresh's time budget is spent
    body = await http.req("GET", "https://itunes.apple.com/search", params={"term": q, "entity": "song", "limit": "25",
                                                                            "country": "IN"}, cache=DAY, conc=2, tries=2)
    owned = set(store.meta_get("music:owned", []))       # title cores of your Jellyfin + Tunarr songs (music refresh)
    out = []
    for s in mc.parse_itunes(body)[:20]:
        enc = urllib.parse.quote(f"{s['artist']} {s['title']}")
        s.update(have=mc.is_owned(s["title"], owned), q=f"{s['artist']} {s['title']}",
                 tunarr=f"{base}/?q={enc}#search", ytm=f"https://music.youtube.com/search?q={enc}")
        out.append(s)
    return out


# ---------------- page ----------------
async def run(req, q, kind):
    from discover import app
    if not q:
        return {"titles": [], "books": [], "songs": [], "people": []}
    base = f"{req.url.scheme}://{req.url.hostname}:5040"
    http = clients.Http()
    try:
        want = {"titles": kind in ("all", "movie", "tv"), "books": kind in ("all", "book"), "songs": kind in ("all", "music")}
        jobs = {"titles": search_titles(http, q, kind), "books": search_books(http, q), "songs": search_songs(http, q, base)}
        names = [n for n in jobs if want[n]]
        res = await asyncio.gather(*[jobs[n] for n in names], return_exceptions=True)
        for n in jobs:
            if n not in names:
                jobs[n].close()                      # never awaited → avoid "coroutine was never awaited"
    finally:
        await http.close()
    out = {"titles": [], "books": [], "songs": [], "people": getattr(http, "people", [])}
    for n, r in zip(names, res):
        if isinstance(r, Exception):
            log.warning("search %s: %s", n, r)
        else:
            out[n] = r
    if out["titles"]:
        out["titles"] = app.cards_for([{"key": it["key"], "score": 0, "why": "", "because": []} for it in out["titles"]])
        out["titles"].sort(key=lambda c: (c.get("dl") or {}).get("state") == "lib")   # new-to-you first, owned after
    if kind == "all":
        out["books"], out["songs"] = out["books"][:6], out["songs"][:8]
    return out


@fx.router.get("/search", response_class=HTMLResponse)
async def search_page(req: Request):
    from discover import app
    q, kind = req.query_params.get("q", "").strip()[:100], req.query_params.get("type", "all")
    kind = kind if kind in KINDS else "all"
    res = await run(req, q, kind)
    tpl = "features/search_results.html" if req.query_params.get("partial") else "features/search.html"
    return app.render_page(req, "search", [], template=tpl, sq=q, skind=kind, kinds=KINDS, **res)


fx.NAV.insert(0, ("/search", "Search"))
fx.CSS.append("/static/search.css?v=1")
fx.JS.append("/static/search.js?v=1")
