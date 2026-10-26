"""+ Want options sheet (seasons · Tamil audio · 720p/1080p), download badge + size on cards, ▶ trailer, "More like".
Arr side lives in want_arr.py, trailer + /like in want_like.py. Old POST /act/want still works (default options)."""
import asyncio
import logging

from fastapi import BackgroundTasks, Request
from fastapi.responses import JSONResponse

from discover import clients, store
from discover import features as fx
from discover.features import want_arr as wa

log = logging.getLogger("discover")

fx.CARD_SLOTS.append("features/want_slot.html")
fx.CARD_ACTIONS.append("features/want_act.html")
fx.CSS.append("/static/want.css?v=1")
fx.JS.append("/static/want.js?v=1")


def _card_status(cards):
    """Badge from the in-memory arr cache (never waits on Sonarr/Radarr; the page JS fills in fresh data)."""
    wa.kick_refresh()
    lib, opts = set(store.meta_get("library", [])), wa.opts_all()
    for c in cards:
        c["dl"] = wa.card_status(c, opts.get(c["key"]), lib)


fx.CARD_DATA.append(_card_status)


def _defaults():
    return {"quality": "1080", "tamil": False, **(store.pref("want_last") or {})}


@fx.router.get("/want/options/{key}")
async def options(key: str):
    it = store.items_get([key]).get(key)
    if not it:
        return JSONResponse({"error": "unknown title"}, 404)
    seasons = []
    if it["kind"] == "tv":
        http = clients.Http()
        try:
            counts = await wa.season_counts(http, it)
            seasons = [{"n": n, "eps": e} for n, e in sorted(counts.items())]
        except Exception as ex:                  # sheet still works: "all seasons" only
            log.warning("seasons %s: %s", key, ex)
        finally:
            await http.close()
    saved = wa.opts_get(key) if key in store.wants() else None
    d = _defaults()
    return {"key": key, "kind": it["kind"], "title": it["title"], "year": it.get("year"),
            "tamil_choice": it.get("lang") != "ta", "seasons": seasons,
            "opts": {**{"pick": "all", "seasons": [], "quality": d["quality"], "tamil": d["tamil"]}, **(saved or {})},
            "gb": {"movie": wa.MOVIE_GB, "ep": wa.EP_GB}}


@fx.router.post("/want")
async def want(req: Request, bg: BackgroundTasks):
    body = await req.json()
    key = body.get("key", "")
    it = store.items_get([key]).get(key)
    if not it:
        return JSONResponse({"error": "unknown title"}, 404)
    opts = wa.clean_opts(body, it)
    wa.opts_put(key, opts)                      # before add_want: the list feed must see the variant
    store.add_want(key)
    store.set_pref("want_last", {"quality": opts["quality"], "tamil": bool(body.get("tamil"))})
    bg.add_task(wa.after_want, key)
    return {"ok": True, "variant": wa.variant(opts, it) or "default"}


@fx.router.get("/want/status")
async def status(keys: str = ""):
    """{key: {state, label, size}} for the cards on screen (fresh arr data, ≤60 s old)."""
    ks = [k for k in keys.split(",") if ":" in k][:150]
    items = store.items_get(ks)
    await wa.status_refresh()
    tv = [it for it in items.values() if it["kind"] == "tv" and it["key"] not in wa._eps]
    if tv:                                      # episode counts for size estimates (TMDB, cached 7 days)
        http = clients.Http()
        try:
            await asyncio.gather(*[wa.season_counts(http, it) for it in tv], return_exceptions=True)
        finally:
            await http.close()
    lib, opts, wants = set(store.meta_get("library", [])), wa.opts_all(), store.wants()
    return {k: wa.card_status({**it, "wanted": k in wants}, opts.get(k), lib) for k, it in items.items()}
