"""New for you (🔔): new / announced seasons of series you own or track, and fresh work from the people you love.
Computed once per refresh (POST_REFRESH) into meta "alerts", rendered from storage. "Get season N" is the only
thing here that writes to Sonarr, and only when you tap it."""
import asyncio
import datetime
import logging

from fastapi import Request
from fastapi.responses import HTMLResponse

from discover import clients, config, store
from discover import features as fx

log = logging.getLogger("discover")
DAY = clients.DAY
OUT_DAYS, LATE_DAYS = 90, 365         # "out" = aired ≤90 days ago, or ≤1 year ago and after the last season you have
PEOPLE_DAYS, AHEAD_DAYS = 120, 365    # people: released in the last ~4 months, or dated up to a year ahead
MAX_SERIES, MAX_WORKS = 500, 30       # keep the refresh bounded
CREW_JOBS = {"Director", "Creator", "Writer", "Screenplay"}
NOT_SHOWS = {10763, 10764, 10767}     # news / reality / talk — "new work" there is just noise
CSS = "/static/alerts.css"


def _d(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def nice(iso, today=None):
    """'2026-11-03' → '3 Nov' (adds the year when it isn't this year); '' → 'TBA'."""
    d, today = _d(iso), today or datetime.date.today()
    if not d:
        return "TBA"
    return f"{d.day} {d:%b}" + (f" {d.year}" if d.year != today.year else "")


# ---------------- pure bits (tested) ----------------
def season_alerts(d, have, today):
    """TMDB tv details + season numbers you have (None = unknown) → [{season, state: out|coming, date, airing}]."""
    top = max(have) if have else 0
    nxt = d.get("next_episode_to_air") or {}
    live = d.get("status") in ("Returning Series", "In Production", "Planned")
    out = []
    for s in d.get("seasons") or []:
        n, air = s.get("season_number") or 0, _d(s.get("air_date"))
        if n < 1 or (have is not None and n in have):
            continue
        if air and air <= today:
            age = (today - air).days
            recent = age <= OUT_DAYS and (have is not None or n > 1)   # unknown library: assume you have S1
            late = have is not None and n > top and age <= LATE_DAYS
            if recent or late:
                out.append({"season": n, "state": "out", "date": air.isoformat(),
                            "airing": nxt.get("season_number") == n})
        elif air or live:                                  # announced: dated, or listed on a running show
            date = air or (_d(nxt.get("air_date")) if nxt.get("season_number") == n else None)
            out.append({"season": n, "state": "coming", "date": date.isoformat() if date else ""})
    return out


def fresh_works(cr, today, with_cast=False):
    """TMDB combined_credits → {(kind, id): date} released recently or dated ahead. Cast only if with_cast (top 4)."""
    rows = [c for c in cr.get("crew", []) if c.get("job") in CREW_JOBS]
    if with_cast:
        rows += [c for c in cr.get("cast", []) if c.get("media_type") == "movie" and (c.get("order") or 0) <= 3]
    out = {}
    for c in rows:
        kind, date = c.get("media_type"), _d(c.get("release_date") or c.get("first_air_date"))
        if (kind in ("movie", "tv") and date and not c.get("adult") and not NOT_SHOWS & set(c.get("genre_ids") or [])
                and -PEOPLE_DAYS <= (date - today).days <= AHEAD_DAYS):
            out.setdefault((kind, c["id"]), date)
    return out


def cast_affinity(items, seeds_w):
    """Actors you keep watching: top-3-billed in titles you liked, weighted like the seeds."""
    aff = {}
    for k, w in seeds_w.items():
        if w > 0 and k in items:
            for name in [n for role, n in items[k].get("people", []) if role == "cast"][:3]:
                aff[name] = aff.get(name, 0) + w
    return aff


def badge_count(data, wants=()):
    """Things you can act on now: seasons out (not already on their way) + released new work not yet wanted."""
    today = datetime.date.today().isoformat()
    n = sum(1 for r in data.get("seasons", []) for a in r["alerts"] if a["state"] == "out" and not a.get("tracking"))
    return n + sum(1 for w in data.get("people", []) if w["date"] <= today and w["key"] not in wants)


class _Label(str):
    """Tab label that renders its own count badge (page.html prints {{ label }}; __html__ skips escaping)."""
    def __html__(self):
        try:
            n = badge_count(store.meta_get("alerts", {}), store.wants())
        except Exception:
            n = 0
        return str(self) + (f' <i class="badge">{n}</i>' if n else "")


fx.TABS.append(("/new", _Label("New"), "bell"))
if CSS not in fx.CSS:
    fx.CSS.append(CSS)


# ---------------- what you have: Sonarr seasons + Jellyfin season folders ----------------
async def _sonarr(http):
    """{tmdb id: series} from Sonarr ({} when it's not configured / down)."""
    if not config.SONARR_KEY:
        return {}
    try:
        return {s["tmdbId"]: s for s in await clients.arr(http, "sonarr", "/series") if s.get("tmdbId")}
    except Exception as ex:
        log.warning("alerts: sonarr: %s", ex)
        return {}


async def _jellyfin_seasons(http):
    """{tmdb id: {season numbers on disk}} — Jellyfin series → their real (non-virtual) season folders."""
    if not config.JELLYFIN_KEY:
        return {}
    h, base = {"Authorization": f'MediaBrowser Token="{config.JELLYFIN_KEY}"'}, config.JELLYFIN_URL
    try:
        users = await http.req("GET", f"{base}/Users", headers=h)
        uid = next(u["Id"] for u in users if u["Name"].lower() == config.JELLYFIN_USER.lower())
        get = lambda t: http.req("GET", f"{base}/Users/{uid}/Items", headers=h, params={  # noqa: E731
            "IncludeItemTypes": t, "Recursive": "true", "Fields": "ProviderIds"})
        series, seasons = await asyncio.gather(get("Series"), get("Season"))
    except Exception as ex:
        log.warning("alerts: jellyfin seasons: %s", ex)
        return {}
    tmdb_of = {}
    for s in series.get("Items", []):
        ids = {k.lower(): v for k, v in (s.get("ProviderIds") or {}).items()}
        tid = ids.get("tmdb")
        if not tid and ids.get("tvdb"):                    # rare: only a TVDB id → TMDB /find (cached a month)
            try:
                kind, tid = await clients.tmdb_find(http, ids["tvdb"], "tvdb_id")
                tid = tid if kind == "tv" else None
            except Exception:
                tid = None
        if tid:
            tmdb_of[s["Id"]] = int(tid)
    have = {}
    for s in seasons.get("Items", []):
        tid = tmdb_of.get(s.get("SeriesId"))
        if tid and s.get("IndexNumber") is not None and s.get("LocationType") != "Virtual":
            have.setdefault(tid, set()).add(s["IndexNumber"])
    return have


async def _new_seasons(ctx, today):
    http, seeds_w = ctx["http"], ctx["seeds_w"]
    sonarr, jf = await asyncio.gather(_sonarr(ctx["http"]), _jellyfin_seasons(ctx["http"]))
    ids = {int(k.split(":")[1]) for k in store.meta_get("library", []) if k.startswith("tv:")} | set(sonarr) | set(jf)
    ids = [i for i in ids if seeds_w.get(f"tv:{i}", 0) >= 0][:MAX_SERIES]     # skip shows you bailed on
    ended = set(store.meta_get("alerts:ended", []))

    async def details(i):        # running shows daily, ended ones monthly (they rarely change)
        return await clients.tmdb(http, f"/tv/{i}", cache=30 * DAY if i in ended else DAY,
                                  append_to_response="external_ids")
    res = await asyncio.gather(*[details(i) for i in ids], return_exceptions=True)
    rows = []
    for i, d in zip(ids, res):
        if isinstance(d, Exception) or not d:
            continue
        if d.get("status") in ("Ended", "Canceled"):
            ended.add(i)
        s = sonarr.get(i)
        have = set(jf.get(i, ()))
        coming = set()             # seasons Sonarr is already set to grab
        for se in (s or {}).get("seasons", []):
            if (se.get("statistics") or {}).get("episodeFileCount"):
                have.add(se["seasonNumber"])
            elif s.get("monitored") and se.get("monitored"):
                coming.add(se["seasonNumber"])
        known = bool(have) or i in jf or s is not None
        alerts = season_alerts(d, have if known else None, today)
        for a in alerts:
            a["tracking"] = a["season"] in coming
        if alerts:
            rows.append({"key": f"tv:{i}", "tmdb": i, "title": d.get("name", ""), "poster": d.get("poster_path"),
                         "tvdb": (d.get("external_ids") or {}).get("tvdb_id"), "in_sonarr": s is not None,
                         "alerts": alerts})
    store.meta_put("alerts:ended", sorted(ended))
    return rows


async def _person_credits(http, name):
    r = await clients.tmdb(http, "/search/person", cache=30 * DAY, query=name)   # same cache row as the pipeline
    if not r.get("results"):
        return {}
    return await clients.tmdb(http, f"/person/{r['results'][0]['id']}/combined_credits", cache=2 * DAY)


async def _new_from_people(ctx, today):
    http, exclude = ctx["http"], ctx["exclude"]
    crew = [n for n, w in sorted(ctx.get("people", {}).items(), key=lambda x: -x[1])[:6] if w >= 1.5]
    cast = [n for n, w in sorted(cast_affinity(ctx["items"], ctx["seeds_w"]).items(), key=lambda x: -x[1])[:3]
            if w >= 3.0 and n not in crew]                  # only actors you really follow
    who = crew + cast
    credits = await asyncio.gather(*[_person_credits(http, n) for n in who], return_exceptions=True)
    found = {}
    for name, cr in zip(who, credits):
        if isinstance(cr, Exception) or not cr:
            continue
        for (kind, tid), date in fresh_works(cr, today, with_cast=name in cast).items():
            key = f"{kind}:{tid}"
            if key not in exclude and key not in found:
                found[key] = {"key": key, "who": name, "date": date.isoformat()}
    # newest released first, then soonest upcoming
    t = today.isoformat()
    picked = sorted(found.values(), key=lambda w: (w["date"] > t, -_d(w["date"]).toordinal() if w["date"] <= t
                                                   else _d(w["date"]).toordinal()))[:MAX_WORKS]
    items = await asyncio.gather(*[clients.tmdb_item(http, *w["key"].split(":")) for w in picked],
                                 return_exceptions=True)
    return [w for w, it in zip(picked, items) if not isinstance(it, Exception) and not it.get("adult")]


async def compute(ctx):
    today = datetime.date.today()
    seasons, people = await asyncio.gather(_new_seasons(ctx, today), _new_from_people(ctx, today))
    seasons.sort(key=lambda r: min((0 if a["state"] == "out" else 1,
                                    -_d(a["date"]).toordinal() if a["state"] == "out"
                                    else (_d(a["date"]) or datetime.date.max).toordinal()) for a in r["alerts"]))
    store.meta_put("alerts", {"seasons": seasons, "people": people, "day": today.isoformat()})
    log.info("alerts: %d series with new seasons, %d new titles from people you love", len(seasons), len(people))


fx.POST_REFRESH.append(compute)


# ---------------- page + the one Sonarr action ----------------
@fx.router.get("/new", response_class=HTMLResponse)
async def new_page(req: Request):
    from discover import app       # lazy: app imports the feature registry
    data, today = store.meta_get("alerts", {}), datetime.date.today()
    t = today.isoformat()
    got = set(store.meta_get("alerts:got", []))
    seasons = []
    for r in data.get("seasons", []):
        alerts = []
        for a in r["alerts"]:
            if a["state"] == "coming" and a["date"] and a["date"] <= t:
                a = {**a, "state": "out"}                     # it came out since the last refresh
            label = (f"Season {a['season']} {'airing' if a.get('airing') else 'out'} · {nice(a['date'], today)}"
                     if a["state"] == "out" else f"Season {a['season']} coming {nice(a['date'], today)}")
            alerts.append({**a, "label": label, "got": a.get("tracking") or f"{r['tmdb']}:{a['season']}" in got})
        seasons.append({**r, "alerts": alerts})
    picks = [{"key": w["key"], "score": 0, "because": [],
              "why": (f"New from {w['who']} · {nice(w['date'], today)}" if w["date"] <= t
                      else f"Coming {nice(w['date'], today)} · from {w['who']}")} for w in data.get("people", [])]
    return app.render_page(req, "new", app.cards_for(picks), template="features/new.html", seasons=seasons)


async def get_season(http, tmdb_id, season, tvdb=None):
    """Monitor + search one season. Series already in Sonarr: monitor series/season/episodes, then SeasonSearch.
    Not in Sonarr: add it (Discover list's folder + quality) with only that season monitored, search on add."""
    arr = lambda path, method="GET", body=None: clients.arr(http, "sonarr", path, method, body)  # noqa: E731
    s = next((x for x in await arr("/series") if x.get("tmdbId") == tmdb_id or (tvdb and x.get("tvdbId") == tvdb)),
             None)
    if s:
        s = await arr(f"/series/{s['id']}")
        s["monitored"] = True
        for se in s["seasons"]:
            if se["seasonNumber"] == season:
                se["monitored"] = True
        await arr(f"/series/{s['id']}", "PUT", s)
        eps = await arr(f"/episode?seriesId={s['id']}&seasonNumber={season}")
        if eps:
            await arr("/episode/monitor", "PUT", {"episodeIds": [e["id"] for e in eps], "monitored": True})
        await arr("/command", "POST", {"name": "SeasonSearch", "seriesId": s["id"], "seasonNumber": season})
        return f"Sonarr is searching for {s['title']} season {season}"
    if not tvdb:
        return None
    found = await arr(f"/series/lookup?term=tvdb:{tvdb}")
    if not found:
        return None
    new = found[0]
    lst = next((x for x in await arr("/importlist") if x["name"] == "Streamarr Discover"), None)
    root = lst["rootFolderPath"] if lst else (await arr("/rootfolder"))[0]["path"]
    qp = lst["qualityProfileId"] if lst else (await arr("/qualityprofile"))[0]["id"]
    new.update(monitored=True, rootFolderPath=root, qualityProfileId=qp, seasonFolder=True,
               seasons=[{"seasonNumber": x["seasonNumber"], "monitored": x["seasonNumber"] == season}
                        for x in new.get("seasons", [])],
               addOptions={"monitor": "unknown", "searchForMissingEpisodes": True})   # unknown = use season flags
    await arr("/series", "POST", new)
    return f"Added {new.get('title', 'it')} to Sonarr — searching season {season}"


@fx.router.post("/alerts/season/{tmdb_id}/{season}")
async def get_season_route(tmdb_id: int, season: int):
    row = next((r for r in store.meta_get("alerts", {}).get("seasons", []) if r["tmdb"] == tmdb_id), {})
    http = clients.Http()
    try:
        msg = await get_season(http, tmdb_id, season, row.get("tvdb"))
    except Exception as ex:
        log.warning("alerts: get season %s/%s: %s", tmdb_id, season, ex)
        return {"ok": False, "msg": "Sonarr didn't take it — try again later"}
    finally:
        await http.close()
    if not msg:
        return {"ok": False, "msg": "Can't find this show on Sonarr"}
    if f"tv:{tmdb_id}" not in store.wants():   # on a list → Sonarr's "unmonitor if on no list" cleanup leaves it be
        from discover.features import want_arr
        store.add_want(f"tv:{tmdb_id}")
        want_arr.mark_applied(f"tv:{tmdb_id}")   # seasons were just set here; the want flow mustn't redo them
    got = set(store.meta_get("alerts:got", [])) | {f"{tmdb_id}:{season}"}
    store.meta_put("alerts:got", sorted(got))
    return {"ok": True, "msg": msg}
