"""Wishlist feeds Sonarr ("Custom List", tvdbId) and Radarr ("StevenLu Custom", imdb_id) follow as import lists
named "Streamarr Discover" (+ " (Tamil)", " (720p)", " (Tamil 720p)" → ?variant=tamil|720|tamil720, each with its
own quality profile). Each want is on exactly one list. Protected by LIST_KEY (derived from the password).
A list fetch = a sync is running → afterwards apply seasons/profile for new wants (want_arr.reconcile)."""
import hmac

from fastapi import BackgroundTasks, Request, Response

from discover import config, store
from discover import features as fx
from discover.features import want_arr


def _ok(req):
    return hmac.compare_digest(req.query_params.get("key", ""), config.LIST_KEY)


def _items(req, kind, app):
    v = req.query_params.get("variant", "")
    items = store.items_get(k for k in store.wants() if k.startswith(kind + ":"))
    return want_arr.feed_items(items, want_arr.opts_all(), v if v in want_arr.VARIANTS else "", want_arr.live_variants(app))


@fx.router.get("/lists/sonarr")
async def list_sonarr(req: Request, bg: BackgroundTasks):
    if not _ok(req):
        return Response(status_code=403)
    bg.add_task(want_arr.reconcile, "tv")
    return [{"tvdbId": it["tvdb"]} for it in _items(req, "tv", "sonarr") if it.get("tvdb")]


@fx.router.get("/lists/radarr")
async def list_radarr(req: Request, bg: BackgroundTasks):
    if not _ok(req):
        return Response(status_code=403)
    bg.add_task(want_arr.reconcile, "movie")
    return [{"title": it["title"], "imdb_id": it["imdb"]} for it in _items(req, "movie", "radarr") if it.get("imdb")]
