"""Music: free open-data lookups, polite and cached 30 days. Every function returns empty on failure.
MusicBrainz (artist ids, ≤1 req/s) · ListenBrainz Labs (similar artists) · Deezer (similar artists + fan counts,
no key) · iTunes Search (top songs, artwork, 30 s previews; ~20 req/min) · ListenBrainz top recordings (only
served for some artists without a token) · Last.fm (optional, LASTFM_KEY)."""
import asyncio
import logging
import os
import time

import httpx

from discover import store

from . import music_core as mc

log = logging.getLogger("discover")
DAY = 86400
UA = {"User-Agent": "StreamarrDiscover/1.0 ( self-hosted personal music recommender, one user, cached 30 days )"}
GAP = {"musicbrainz.org": 1.1, "itunes.apple.com": 3.0, "api.deezer.com": 0.15,
       "labs.api.listenbrainz.org": 0.3, "api.listenbrainz.org": 0.3, "ws.audioscrobbler.com": 0.25}
LB_ALGO = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
LASTFM_KEY = os.environ.get("LASTFM_KEY", "").strip()
_locks, _last = {}, {}
deadline = [float("inf")]          # uncached lookups stop after this (refresh time budget); cached ones always work


async def get(http, url, params=None, cache=30 * DAY):
    """Cached GET with a per-host gap. 4xx answers are remembered too (so a 401/404 isn't retried daily)."""
    key = url + ("?" + str(sorted(params.items())) if params else "")      # same key as clients.Http.req
    hit = store.cache_get(key, cache)
    if hit is not None:
        return None if isinstance(hit, dict) and "_err" in hit else hit
    if time.time() > deadline[0]:
        return None
    host = httpx.URL(url).host
    async with _locks.setdefault(host, asyncio.Lock()):
        wait = _last.get(host, 0) + GAP.get(host, 0.5) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await http.req("GET", url, params=params, headers=UA, cache=cache, conc=1, tries=3)
        except httpx.HTTPStatusError as ex:
            code = ex.response.status_code
            if code < 500 and code != 429:
                store.cache_put(key, {"_err": code})
            log.info("music: %s → %s", host, code)
        except Exception as ex:
            log.info("music: %s failed: %s", host, type(ex).__name__)
        finally:
            _last[host] = time.time()
    return None


async def mb_artist(http, name, lang=None):
    b = await get(http, "https://musicbrainz.org/ws/2/artist", {"query": name, "fmt": "json", "limit": "5"})
    return mc.pick_mb((b or {}).get("artists"), name, lang)


async def dz_artist(http, *names):
    for n in dict.fromkeys(x for x in names if x):
        b = await get(http, "https://api.deezer.com/search/artist", {"q": n, "limit": "5"})
        hit = mc.pick_dz((b or {}).get("data"), *names)
        if hit:
            return hit
    return None


async def lb_similar(http, mbid):
    b = await get(http, "https://labs.api.listenbrainz.org/similar-artists/json",
                  {"artist_mbids": mbid, "algorithm": LB_ALGO})
    return mc.parse_lb_similar(b)


async def dz_related(http, dz_id):
    return mc.parse_dz_related(await get(http, f"https://api.deezer.com/artist/{dz_id}/related", {"limit": "20"}))


async def lastfm_similar(http, name):
    if not LASTFM_KEY:
        return []
    b = await get(http, "https://ws.audioscrobbler.com/2.0/", {"method": "artist.getsimilar", "artist": name,
                                                               "api_key": LASTFM_KEY, "format": "json", "limit": "25"})
    return mc.parse_lastfm_similar(b)


async def top_songs(http, name, mbid=None):
    """An artist's best-known songs: iTunes (India storefront — best Tamil coverage), else ListenBrainz."""
    b = await get(http, "https://itunes.apple.com/search",
                  {"term": name, "entity": "song", "limit": "50", "country": "IN", "attribute": "artistTerm"})
    songs = mc.parse_itunes(b, name)
    if not songs and mbid:
        songs = mc.parse_lb_top(await get(http, f"https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/{mbid}"))
    return songs[:12]


async def find_song(http, title, artist):
    """Verify a suggested song exists (Gemini can invent titles) and get artwork/preview."""
    b = await get(http, "https://itunes.apple.com/search",
                  {"term": f"{title} {artist}", "entity": "song", "limit": "5", "country": "IN"})
    want = mc.title_core(title)
    return next((s for s in mc.parse_itunes(b) if mc.title_core(s["title"]) == want), None)
