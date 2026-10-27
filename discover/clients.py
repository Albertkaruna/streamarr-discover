"""Thin async clients: Jellyfin, TMDB, Trakt (public), Sonarr/Radarr, Gemini. GETs are cached in SQLite."""
import asyncio
import datetime
import json
import logging

import httpx

from . import config, store

log = logging.getLogger("discover")
DAY = 86400


class Http:
    def __init__(self):
        self.c = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.limits = {}  # host → semaphore, so one slow API can't starve the others

    async def close(self):
        await self.c.aclose()

    async def req(self, method, url, *, params=None, headers=None, json_body=None, cache=0, conc=6, tries=4):
        key = url + ("?" + str(sorted(params.items())) if params else "")
        if method == "GET" and cache:
            hit = store.cache_get(key, cache)
            if hit is not None:
                return hit
        sem = self.limits.setdefault(httpx.URL(url).host, asyncio.Semaphore(conc))
        for attempt in range(tries):
            try:
                async with sem:
                    r = await self.c.request(method, url, params=params, headers=headers, json=json_body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError("retry", request=r.request, response=r)
                r.raise_for_status()
                body = r.json() if r.content else None
                if method == "GET" and cache:
                    store.cache_put(key, body)
                return body
            except (httpx.TransportError, httpx.HTTPStatusError) as ex:
                status = getattr(getattr(ex, "response", None), "status_code", 0)
                if attempt == tries - 1 or (status and status < 500 and status != 429):
                    raise
                await asyncio.sleep(min(30, 2 ** attempt * (5 if status == 429 else 1)))


# ---------------- Jellyfin: what you watched, how much, and what you own ----------------
async def jellyfin_library(http):
    h = {"Authorization": f'MediaBrowser Token="{config.JELLYFIN_KEY}"'}
    base = config.JELLYFIN_URL
    users = await http.req("GET", f"{base}/Users", headers=h)
    uid = next(u["Id"] for u in users if u["Name"].lower() == config.JELLYFIN_USER.lower())
    data = await http.req("GET", f"{base}/Users/{uid}/Items", headers=h, params={
        "IncludeItemTypes": "Movie,Series", "Recursive": "true", "Fields": "ProviderIds,Genres,DateCreated"})
    out = []

    async def episodes(series_id, played):
        p = {"ParentId": series_id, "IncludeItemTypes": "Episode", "Recursive": "true", "Limit": "0"}
        if played:
            p["IsPlayed"] = "true"
        return (await http.req("GET", f"{base}/Users/{uid}/Items", headers=h, params=p))["TotalRecordCount"]

    for it in data["Items"]:
        ud = it.get("UserData", {})
        row = {"kind": "movie" if it["Type"] == "Movie" else "tv", "name": it.get("Name"),
               "ids": {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()},
               "played": ud.get("Played", False), "playcount": ud.get("PlayCount", 0),
               "favorite": ud.get("IsFavorite", False), "last": ud.get("LastPlayedDate"), "added": it.get("DateCreated"),
               "percent": ud.get("PlayedPercentage") or 0}
        if row["kind"] == "tv":
            row["episodes"], row["watched"] = await asyncio.gather(episodes(it["Id"], False), episodes(it["Id"], True))
        out.append(row)
    return out


# ---------------- TMDB: metadata + "recommended"/"similar" per title ----------------
def _tmdb_auth():
    k = config.TMDB_KEY
    return ({"Authorization": f"Bearer {k}"}, {}) if len(k) > 40 else ({}, {"api_key": k})


async def tmdb(http, path, cache=7 * DAY, **params):
    h, p = _tmdb_auth()
    return await http.req("GET", f"https://api.themoviedb.org/3{path}", headers=h,
                          params={**p, "language": "en-US", **params}, cache=cache, conc=8)


async def tmdb_find(http, ext_id, source):
    r = await tmdb(http, f"/find/{ext_id}", cache=30 * DAY, external_source=source)
    for kind, field in (("movie", "movie_results"), ("tv", "tv_results")):
        if r.get(field):
            return kind, r[field][0]["id"]
    return None, None


def normalize(kind, d):
    """TMDB details (with appended sub-requests) → our compact item."""
    today = datetime.date.today().isoformat()
    date = d.get("release_date") if kind == "movie" else d.get("first_air_date")
    kw = (d.get("keywords") or {}).get("keywords" if kind == "movie" else "results") or []
    credits = d.get("credits") or {}
    people = [("creator", c["name"]) for c in d.get("created_by") or []]
    people += [("director" if c["job"] == "Director" else "writer", c["name"]) for c in credits.get("crew", [])
               if c.get("job") in ("Director", "Screenplay", "Writer", "Novel", "Story")]
    people += [("cast", c["name"]) for c in sorted(credits.get("cast", []), key=lambda c: c.get("order", 99))[:6]]
    ext = d.get("external_ids") or {}
    if kind == "movie":
        runtime = d.get("runtime")
    else:  # TMDB often leaves episode_run_time empty for newer shows → last aired episode's runtime
        runtime = (d.get("episode_run_time") or [None])[0] or (d.get("last_episode_to_air") or {}).get("runtime")
    countries = list(d.get("origin_country") or []) + [c["iso_3166_1"] for c in d.get("production_countries") or []
                                                        if c.get("iso_3166_1")]
    return {
        "key": f"{kind}:{d['id']}", "kind": kind, "tmdb": d["id"],
        "title": d.get("title") or d.get("name") or "", "year": (date or "")[:4],
        "overview": d.get("overview") or "", "poster": d.get("poster_path"), "backdrop": d.get("backdrop_path"),
        "genres": [g["name"] for g in d.get("genres", [])], "keywords": [k["name"] for k in kw][:25],
        "people": list(dict.fromkeys(people)), "lang": d.get("original_language"),
        "rating": d.get("vote_average") or 0, "votes": d.get("vote_count") or 0,
        "popularity": d.get("popularity") or 0, "adult": d.get("adult", False),
        "imdb": d.get("imdb_id") or ext.get("imdb_id"), "tvdb": ext.get("tvdb_id"),
        "released": bool(date) and date <= today,
        "seasons": d.get("number_of_seasons"),
        "runtime": runtime or None, "countries": list(dict.fromkeys(countries)),
        "recs": [r["id"] for r in (d.get("recommendations") or {}).get("results", [])],
        "similar": [r["id"] for r in (d.get("similar") or {}).get("results", [])],
    }


async def tmdb_item(http, kind, tmdb_id):
    d = await tmdb(http, f"/{kind}/{tmdb_id}",
                   append_to_response="recommendations,similar,keywords,credits,external_ids")
    item = normalize(kind, d)
    store.item_put(item["key"], item)
    return item


async def tmdb_person_works(http, name):
    """Other titles by a director/creator you love (top-voted, as director/creator/writer)."""
    r = await tmdb(http, "/search/person", cache=30 * DAY, query=name)
    if not r.get("results"):
        return []
    pid = r["results"][0]["id"]
    cr = await tmdb(http, f"/person/{pid}/combined_credits", cache=14 * DAY)
    works = [w for w in cr.get("crew", []) if w.get("job") in ("Director", "Creator", "Screenplay", "Writer")
             and w.get("media_type") in ("movie", "tv") and (w.get("vote_count") or 0) >= 50]
    works.sort(key=lambda w: -(w.get("vote_count") or 0))
    return [(w["media_type"], w["id"]) for w in works[:12]]


# ---------------- Trakt public "related" (needs only the app's client id) ----------------
async def trakt_related(http, kind, imdb):
    if not (config.TRAKT_CLIENT_ID and imdb):
        return []
    path = "movies" if kind == "movie" else "shows"
    try:
        r = await http.req("GET", f"https://api.trakt.tv/{path}/{imdb}/related", params={"limit": "15"}, cache=14 * DAY,
                           headers={"trakt-api-version": "2", "trakt-api-key": config.TRAKT_CLIENT_ID}, conc=3)
    except httpx.HTTPError as ex:
        log.warning("trakt related %s: %s", imdb, ex)
        return []
    return [x["ids"]["tmdb"] for x in r or [] if x.get("ids", {}).get("tmdb")]


# ---------------- Sonarr / Radarr: what's already wanted + instant list sync ----------------
async def arr(http, which, path, method="GET", body=None):
    url, key = (config.RADARR_URL, config.RADARR_KEY) if which == "radarr" else (config.SONARR_URL, config.SONARR_KEY)
    return await http.req(method, f"{url}/api/v3{path}", headers={"X-Api-Key": key}, json_body=body)


async def arr_wanted(http):
    """tmdb ids Radarr/Sonarr already track (downloaded or waiting) → don't recommend, but count as interest."""
    movies = await arr(http, "radarr", "/movie")
    series = await arr(http, "sonarr", "/series")
    return ({("movie", m["tmdbId"]): m.get("hasFile", False) for m in movies if m.get("tmdbId")} |
            {("tv", s["tmdbId"]): (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
             for s in series if s.get("tmdbId")})


async def arr_sync_discover_list(http, which):
    for lst in await arr(http, which, "/importlist"):
        if lst["name"] == "Streamarr Discover":
            await arr(http, which, "/command", "POST", {"name": "ImportListSync", "definitionId": lst["id"]})


# ---------------- Gemini: embeddings + one JSON re-rank call ----------------
GEM = "https://generativelanguage.googleapis.com/v1beta/models"


async def gemini_embed(http, texts):
    m = config.GEMINI_EMBED_MODEL
    r = await http.req("POST", f"{GEM}/{m}:batchEmbedContents", headers={"x-goog-api-key": config.GEMINI_KEY},
                       json_body={"requests": [{"model": f"models/{m}", "content": {"parts": [{"text": t}]},
                                                "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": 768}
                                               for t in texts]}, conc=1, tries=5)
    return [e["values"] for e in r["embeddings"]]


async def gemini_json(http, prompt):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    for i, model in enumerate(dict.fromkeys([config.GEMINI_MODEL, config.GEMINI_FALLBACK])):
        try:
            r = await http.req("POST", f"{GEM}/{model}:generateContent", headers={"x-goog-api-key": config.GEMINI_KEY},
                               json_body=body, conc=1, tries=3)
            return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
        except httpx.HTTPError as ex:  # not on your plan / out of free quota → try the lighter model once
            log.warning("gemini %s failed: %s", model, ex)
            if i:
                raise
