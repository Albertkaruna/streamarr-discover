"""Poster cache: /img/<size>/<file> serves TMDB images from disk, fetching each once. TMDB's CDN can take 20 s per
poster over the mobile hotspot; after the first view they load at LAN/Tailscale speed (and the phone caches them)."""
import asyncio
import re

import httpx
from fastapi import Response
from fastapi.responses import FileResponse

from discover import config
from discover import features as fx

SIZES = {"w92", "w154", "w185", "w342", "w500", "w780", "original"}
NAME = re.compile(r"^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp|svg)$")   # TMDB file names only → no path tricks
ROOT = config.DATA / "img"
_client = None
_locks = {}
_sem = asyncio.Semaphore(6)                    # don't open 60 connections when a page of posters loads at once
CACHE = {"Cache-Control": "public, max-age=2592000, immutable"}


async def _fetch(size, name, path):
    global _client
    _client = _client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    async with _sem:
        r = await _client.get(f"https://image.tmdb.org/t/p/{size}/{name}")
    if r.status_code != 200 or not r.content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(r.content)
    tmp.replace(path)                          # atomic: a half-written poster is never served
    return True


@fx.router.get("/img/{size}/{name}")
async def img(size: str, name: str):
    if size not in SIZES or not NAME.match(name):
        return Response(status_code=404)
    path = ROOT / size / name
    if not path.exists():
        lock = _locks.setdefault(path, asyncio.Lock())   # two cards with the same poster → one download
        async with lock:
            if not path.exists():
                try:
                    ok = await _fetch(size, name, path)
                except httpx.HTTPError:
                    ok = False
                if not ok:
                    _locks.pop(path, None)
                    return Response(status_code=502)
        _locks.pop(path, None)
    return FileResponse(path, headers=CACHE)


async def warm(ctx):
    """After a refresh, fetch the posters today's pages will show (picks + coming soon), so even the first look on
    the phone is instant. Bounded: ≤ ~300 small files, skips what's cached, never fails the refresh."""
    from discover import store
    keys = [p["key"] for k in ("movie", "tv") for p in store.picks(k)]
    keys += [c["key"] for c in (store.meta_get("radar") or {}).get("coming", [])]
    posters = {it.get("poster") for it in store.items_get(dict.fromkeys(keys)).values()} - {None, ""}
    jobs = [(size, p.lstrip("/")) for p in posters for size in ("w342", "w185")]
    todo = [(s, n) for s, n in jobs[:600] if NAME.match(n) and not (ROOT / s / n).exists()]

    async def one(s, n):
        try:
            await _fetch(s, n, ROOT / s / n)
        except httpx.HTTPError:
            pass
    await asyncio.gather(*[one(s, n) for s, n in todo])


fx.POST_REFRESH.append(warm)
