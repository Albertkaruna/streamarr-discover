"""▶ Trailer (best TMDB YouTube video) and "More like this" (/like/{key}: recommendations seeded by one title)."""
import asyncio
import logging
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from discover import clients, config, engine, store
from discover import features as fx

log = logging.getLogger("discover")
DAY = 86400
LIKE_N, MIN_VOTES = 24, 30
_trailer = {}                      # key → youtube url | None (TMDB answer is also cached 7 days in SQLite)


def best_video(results, lang=None):
    """Official YouTube trailer first (original language, then English), then teasers."""
    vids = [v for v in results or [] if v.get("site") == "YouTube" and v.get("key")
            and v.get("type") in ("Trailer", "Teaser")]
    vids.sort(key=lambda v: (v["type"] != "Trailer", not v.get("official"),
                             v.get("iso_639_1") not in (lang, "en"), v.get("iso_639_1") != lang, -(v.get("size") or 0)))
    return f"https://www.youtube.com/watch?v={vids[0]['key']}" if vids else None


async def trailer_url(http, key):
    if key not in _trailer:
        kind, tid = key.split(":")
        it = store.items_get([key]).get(key) or {}
        lang = it.get("lang") or "en"
        try:
            r = await clients.tmdb(http, f"/{kind}/{tid}/videos", include_video_language=f"{lang},en,ta,null")
            _trailer[key] = best_video(r.get("results"), lang)
        except Exception as ex:
            log.warning("trailer %s: %s", key, ex)
            return None                 # don't remember failures
    return _trailer[key]


def _known(cards):
    for c in cards:
        if c["key"] in _trailer:
            c["trailer"] = bool(_trailer[c["key"]])


fx.CARD_DATA.append(_known)


@fx.router.get("/trailer/{key}")
async def trailer(key: str):
    http = clients.Http()
    try:
        url = await trailer_url(http, key)
    finally:
        await http.close()
    if url:
        return RedirectResponse(url, status_code=302)
    return HTMLResponse("<meta name=viewport content='width=device-width'><body style='background:#0b1020;color:#9aa3c0;"
                        "font:15px system-ui;text-align:center;padding:60px 16px'>No trailer found on TMDB.", 404)


@fx.router.get("/want/trailers")
async def trailers(keys: str = ""):
    """{key: has trailer} for the cards on screen — the page greys out ▶ where there's none."""
    ks = [k for k in keys.split(",") if ":" in k][:120]
    http = clients.Http()
    try:
        urls = await asyncio.gather(*[trailer_url(http, k) for k in ks])
    finally:
        await http.close()
    return {k: bool(u) for k, u in zip(ks, urls)}


# ---------------- More like this ----------------
def _excluded():
    fb = store.feedback()
    return set(store.meta_get("library", [])) | set(store.wants()) | {k for k, s in fb.items() if s == "dislike"}


async def build_like(http, key):
    """Candidates from this one title only (TMDB recommended ×2 pages + similar, Trakt related), ranked with
    it as the single seed (weight 1.0) and whatever embeddings are already cached (no Gemini calls here)."""
    kind, tid = key.split(":")
    seed = await clients.tmdb_item(http, kind, tid)
    page2, rel = await asyncio.gather(clients.tmdb(http, f"/{kind}/{tid}/recommendations", page=2),
                                      clients.trakt_related(http, kind, seed.get("imdb")), return_exceptions=True)
    page2 = [] if isinstance(page2, Exception) else [r["id"] for r in page2.get("results", [])]
    rel = [] if isinstance(rel, Exception) else rel
    support, skip = {}, _excluded() | {key}
    for ids, w in ((seed["recs"] + page2, 1.0), (seed["similar"], 0.5), (rel, 0.8)):   # same weights as pipeline
        for r, t in enumerate(ids):
            k = f"{kind}:{t}"
            if k not in skip:
                support[k] = support.get(k, 0) + w / (1 + 0.08 * r)
    keys = sorted(support, key=lambda k: -support[k])[:80]
    got = await asyncio.gather(*[clients.tmdb_item(http, kind, k.split(":")[1]) for k in keys], return_exceptions=True)
    cands = [c for c in got if isinstance(c, dict) and c["released"] and not c["adult"] and c["votes"] >= MIN_VOTES]
    vecs = store.embeddings_get([c["key"] for c in cands] + [key], config.GEMINI_EMBED_MODEL)
    ranked = engine.rank(cands, support, {}, [(seed, 1.0)], [], vecs, limit=LIKE_N * 2)
    by = {c["key"]: c for c in cands}
    return seed, [{**r, "why": link_why(seed, by[r["key"]])} for r in ranked]


def link_why(seed, c):
    """What this title concretely shares with the seed — the page header already says "More like <seed>",
    so repeating "Because you watched <seed>" on every card says nothing. No real link → no line."""
    sp, cp = {tuple(p) for p in seed.get("people", [])}, {tuple(p) for p in c.get("people", [])}
    makers = [n for r, n in sp & cp if r in ("director", "creator", "writer")]
    if makers:
        return f"Also by {makers[0]}"
    cast = [n for r, n in sp & cp if r == "cast"]
    if cast:
        return "Also with " + " and ".join(sorted(cast)[:2])
    kw = [k for k in c.get("keywords", []) if k in set(seed.get("keywords", []))]
    return "Also: " + ", ".join(kw[:3]) if kw else ""


@fx.router.get("/like/{key}", response_class=HTMLResponse)
async def like_page(req: Request, key: str):
    from discover import app as web            # late import: app imports the features
    if ":" not in key or key.split(":")[0] not in ("movie", "tv"):
        return HTMLResponse("Not found", 404)
    hit = store.meta_get(f"like2:{key}")   # like2: cached picks carry why, not because
    seed, picks, err = None, [], ""
    if hit and time.time() - hit["ts"] < DAY:
        seed, picks = store.items_get([key]).get(key), hit["picks"]
    if not seed:
        http = clients.Http()
        try:
            seed, picks = await build_like(http, key)
            store.meta_put(f"like2:{key}", {"ts": time.time(), "picks": picks})
        except Exception as ex:
            log.warning("more like %s: %s", key, ex)
            err = "Couldn't reach TMDB — try again in a minute."
            seed = store.items_get([key]).get(key) or {"title": key}
        finally:
            await http.close()
    skip = _excluded()                         # wants/dislikes may have changed since it was cached
    cards = web.cards_for([p for p in picks if p["key"] not in skip])[:LIKE_N]
    return web.tpl.TemplateResponse(req, "features/like.html", {
        "tab": "like", "cards": cards, "seed": seed, "err": err, "status": web._status(), "q": {},
        "wanted_n": len(store.wants())})
