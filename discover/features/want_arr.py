"""+ Want → Sonarr/Radarr: per-want options (seasons, Tamil audio, quality), which variant import list carries a
want, post-sync season monitoring, download status for cards, and idempotent arr setup (ensure_*).

Setup is NOT run automatically (AUTO_SETUP=False). Review, then:
    python -m discover.features.want_arr            # dry run: prints what it would create/change
    python -m discover.features.want_arr --apply    # do it
Until the variant lists exist, every want is served on the plain "Streamarr Discover" list (nothing gets lost).
"""
import asyncio
import json
import logging
import sys
import time
import urllib.parse

from discover import clients, config, store

log = logging.getLogger("discover")

LIST = "Streamarr Discover"
VARIANTS = {"": "", "tamil": " (Tamil)", "720": " (720p)", "tamil720": " (Tamil 720p)"}
PROFILE_DEFAULT, PROFILE_720 = 6, 3                    # "HD - 720p/1080p", "HD-720p" (same ids in both arrs)
TAMIL_PROFILE = {"tamil": "HD - Tamil preferred", "tamil720": "HD - Tamil preferred (720p)"}
CF_TAMIL, CF_1080 = "Tamil audio", "Resolution 1080p"
TAMIL_SCORE, P1080_SCORE = 1000, 100   # Tamil beats any quality inside the HD group; 1080p only breaks ties
MIN_UPGRADE = 500                      # only a Tamil release may replace a file (no 720p→1080p churn)
TAMIL_LANG = 43                        # language id "Tamil" in Sonarr v4 and Radarr v5
TAMIL_RE = r"\b(tamil|tam)\b"
# Indian multi-audio tags: "[Ta + Te + Hi]", "Hin-Tam", "TA.EN" …  (tam/tamil alone is already TAMIL_RE)
MULTI_RE = (r"\bta\b[^a-z0-9]{1,3}\b(te|tel|hi|hin|ml|mal|kn|kan|en|eng)\b"
            r"|\b(te|tel|hi|hin|ml|mal|kn|kan|en|eng)\b[^a-z0-9]{1,3}\bta\b")
AUTO_SETUP = False                     # True → ensure_all() once per process on the first want
POLL_S, POLL_N = 5, 24                 # look for the newly added title for ~2 min after a list sync
TTL = 60                               # card status cache


def list_name(v):
    return LIST + VARIANTS[v]


# ---------------- options per want (own table) ----------------
def _db():
    con = store.db()
    con.execute("CREATE TABLE IF NOT EXISTS want_opts (key TEXT PRIMARY KEY, opts TEXT, applied REAL DEFAULT 0)")
    return con


def opts_get(key):
    row = _db().execute("SELECT opts FROM want_opts WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def opts_all():
    return {k: json.loads(o) for k, o in _db().execute("SELECT key, opts FROM want_opts")}


def opts_put(key, opts):
    with _db() as c:
        c.execute("REPLACE INTO want_opts VALUES (?,?,0)", (key, json.dumps(opts)))


def mark_applied(key):
    with _db() as c:
        c.execute("INSERT INTO want_opts VALUES (?, '{}', ?) ON CONFLICT(key) DO UPDATE SET applied=excluded.applied",
                  (key, time.time()))


def clean_opts(raw, item):
    """Request body → stored options. pick: all | latest | first | custom (custom uses the season list)."""
    q = "720" if str(raw.get("quality")) == "720" else "1080"
    pick = raw.get("pick") if raw.get("pick") in ("all", "latest", "first", "custom") else "all"
    seasons = sorted({int(s) for s in raw.get("seasons") or [] if str(s).isdigit() and int(s) > 0})
    if pick == "custom" and not seasons:
        pick = "all"
    return {"quality": q, "tamil": bool(raw.get("tamil")) and item.get("lang") != "ta",
            "pick": pick if item.get("kind") == "tv" else "all", "seasons": seasons if pick == "custom" else []}


def variant(opts, item):
    """Which list a want rides on. Each want is on exactly one."""
    opts = opts or {}
    v = ("tamil" if opts.get("tamil") and item.get("lang") != "ta" else "") + ("720" if opts.get("quality") == "720" else "")
    return v


def feed_items(items, all_opts, v, live):
    """Items for variant list v. A variant whose list doesn't exist in the arr (setup not run) falls back to ""."""
    out = []
    for k, it in items.items():
        iv = variant(all_opts.get(k), it)
        if (iv if iv in live else "") == v:
            out.append(it)
    return out


def live_variants(app):
    return set(store.meta_get(f"want:variants:{app}", [""])) | {""}


def _epoch():
    """Wants made before this feature existed are left alone (don't re-monitor everything on first run)."""
    e = store.meta_get("want:epoch")
    if e is None:
        e = time.time()
        store.meta_put("want:epoch", e)
    return e


def pending(kind):
    wants, ep = store.wants(), _epoch()
    applied = dict(_db().execute("SELECT key, applied FROM want_opts"))
    return [k for k, ts in wants.items() if k.startswith(kind + ":") and ts > max(applied.get(k) or 0, ep)]


# ---------------- after a sync: seasons / profile / re-monitor ----------------
def plan_series(s, opts, profile_id):
    """Sonarr series JSON + options → (updated series, season numbers to search). Pure."""
    opts = opts or {}
    nums = sorted(x["seasonNumber"] for x in s["seasons"] if x["seasonNumber"] > 0)
    pick = opts.get("pick") or "all"
    if not nums:
        want = set()
    elif pick == "latest":
        want = {nums[-1]}
    elif pick == "first":
        want = {nums[0]}
    elif pick == "custom":
        want = set(opts.get("seasons") or []) & set(nums) or set(nums)   # TMDB/TVDB numbering mismatch → all
    else:
        want = set(nums)
    keep_up = pick in ("all", "latest") or (nums and nums[-1] in want)   # new seasons only if you follow the end
    out = {**s, "monitored": True, "qualityProfileId": profile_id, "monitorNewItems": "all" if keep_up else "none",
           "seasons": [{**x, "monitored": x["seasonNumber"] in want} for x in s["seasons"]]}
    search = []
    for x in s["seasons"]:
        st = x.get("statistics") or {}
        if x["seasonNumber"] in want and (not st or st.get("episodeFileCount", 0) < st.get("totalEpisodeCount", 1)):
            search.append(x["seasonNumber"])
    return out, sorted(search)


def plan_movie(m, profile_id):
    """Radarr movie JSON → (updated movie, search?). Re-monitors a re-wanted movie, applies a changed profile."""
    changed = not m.get("monitored") or m.get("qualityProfileId") != profile_id
    return {**m, "monitored": True, "qualityProfileId": profile_id}, changed


async def _profile_ids(http, app):
    ids = {p["name"]: p["id"] for p in await clients.arr(http, app, "/qualityprofile")}
    out = {"": PROFILE_DEFAULT, "720": PROFILE_720}
    for v, name in TAMIL_PROFILE.items():
        out[v] = ids.get(name, PROFILE_720 if v.endswith("720") else PROFILE_DEFAULT)
    return out


async def _find(http, app, item, tries):
    q = f"/series?tvdbId={item['tvdb']}" if app == "sonarr" else f"/movie?tmdbId={item['tmdb']}"
    for i in range(tries):
        r = await clients.arr(http, app, q)
        r = [x for x in r or [] if x.get("tvdbId" if app == "sonarr" else "tmdbId") == item["tvdb" if app == "sonarr" else "tmdb"]]
        if r:
            return r[0]
        if i < tries - 1:
            await asyncio.sleep(POLL_S)
    return None


_inflight = set()                      # a want's own pass and a list-feed pass must not both search


async def apply_one(http, key, tries=POLL_N, pids=None):
    if key in _inflight:
        return False
    _inflight.add(key)
    try:
        return await _apply_one(http, key, tries, pids)
    finally:
        _inflight.discard(key)


async def _apply_one(http, key, tries, pids):
    item = store.items_get([key]).get(key)
    if not item or key not in store.wants():
        return False
    app = "sonarr" if item["kind"] == "tv" else "radarr"
    if app == "sonarr" and not item.get("tvdb"):
        return False
    found = await _find(http, app, item, tries)
    if not found:
        return False
    opts = opts_get(key) or {}
    pid = (pids or await _profile_ids(http, app))[variant(opts, item)]
    if app == "sonarr":
        new, seasons = plan_series(found, opts, pid)
        await clients.arr(http, app, f"/series/{found['id']}", "PUT", new)
        for n in seasons:
            await clients.arr(http, app, "/command", "POST", {"name": "SeasonSearch", "seriesId": found["id"], "seasonNumber": n})
    else:
        new, search = plan_movie(found, pid)
        if search:
            await clients.arr(http, app, f"/movie/{found['id']}", "PUT", new)
            if not found.get("hasFile") or pid != found.get("qualityProfileId"):
                await clients.arr(http, app, "/command", "POST", {"name": "MoviesSearch", "movieIds": [found["id"]]})
    mark_applied(key)
    log.info("want applied: %s (%s)", key, variant(opts, item) or "default")
    return True


_busy = set()


async def reconcile(kind, keys=None, tries=POLL_N):
    """Apply options for pending wants once Sonarr/Radarr have added them. Debounced per kind."""
    if kind in _busy:
        return
    _busy.add(kind)
    http = clients.Http()
    try:
        keys = keys or pending(kind)
        if not keys:
            return
        app = "sonarr" if kind == "tv" else "radarr"
        pids = await _profile_ids(http, app)
        res = await asyncio.gather(*[apply_one(http, k, tries, pids) for k in keys], return_exceptions=True)
        for k, r in zip(keys, res):
            if isinstance(r, Exception):
                log.warning("want apply %s: %s", k, r)
    finally:
        _busy.discard(kind)
        await http.close()


_setup_done = False


async def after_want(key):
    """Background task for POST /want: sync our lists, then apply seasons/profile when the title shows up."""
    global _setup_done
    kind = key.split(":")[0]
    app = "sonarr" if kind == "tv" else "radarr"
    http = clients.Http()
    try:
        if AUTO_SETUP and not _setup_done:
            _setup_done = True
            await ensure_all(http)
        for lst in await clients.arr(http, app, "/importlist"):
            if lst["name"].startswith(LIST):
                await clients.arr(http, app, "/command", "POST", {"name": "ImportListSync", "definitionId": lst["id"]})
        await apply_one(http, key)
    except Exception as ex:
        log.warning("after want %s: %s", key, ex)
    finally:
        await http.close()


# ---------------- download status for cards (Radarr/Sonarr, cached ~60 s) ----------------
# Size heuristics (GB) — typical x264/x265 WEB-DL/BluRay encodes the HD profiles grab: a ~2 h movie,
# a ~45 min episode. Real releases range ×0.5–×3 (remuxes are excluded by the profiles).
MOVIE_GB = {"1080": 2.5, "720": 1.2}
EP_GB = {"1080": 1.0, "720": 0.5}

_st = {"ts": 0, "movies": {}, "series": {}, "mq": {}, "sq": {}}
_eps = {}                                  # "tv:id" → {season: episode count} from TMDB
_lock = None


def queue_by(records, field):
    """Queue rows → {id: {size, left, active}}. A season pack appears once per episode → count each download once."""
    out, seen = {}, set()
    for r in records or []:
        oid = r.get(field)
        dl = (oid, r.get("downloadId") or r.get("id"))
        if not oid or dl in seen:
            continue
        seen.add(dl)
        q = out.setdefault(oid, {"size": 0, "left": 0, "active": False})
        q["size"] += r.get("size") or 0
        q["left"] += r.get("sizeleft") or 0
        q["active"] |= r.get("status") == "downloading"
    return out


async def status_refresh(force=False):
    global _lock
    if not force and time.time() - _st["ts"] < TTL:
        return
    _lock = _lock or asyncio.Lock()
    async with _lock:
        if not force and time.time() - _st["ts"] < TTL:
            return
        _st["ts"] = time.time()
        http = clients.Http()
        try:
            async def one(app, path):
                key = config.SONARR_KEY if app == "sonarr" else config.RADARR_KEY
                return await clients.arr(http, app, path) if key else None
            movies, series, mq, sq, rl, sl = await asyncio.gather(
                one("radarr", "/movie"), one("sonarr", "/series"), one("radarr", "/queue?pageSize=500"),
                one("sonarr", "/queue?pageSize=1000"), one("radarr", "/importlist"), one("sonarr", "/importlist"),
                return_exceptions=True)
            ok = lambda x: x is not None and not isinstance(x, Exception)
            if ok(movies):
                _st["movies"] = {m["tmdbId"]: m for m in movies if m.get("tmdbId")}
            if ok(series):
                _st["series"] = {("tvdb", s["tvdbId"]): s for s in series if s.get("tvdbId")} | \
                                {("tmdb", s["tmdbId"]): s for s in series if s.get("tmdbId")}
            if ok(mq):
                _st["mq"] = queue_by(mq.get("records"), "movieId")
            if ok(sq):
                _st["sq"] = queue_by(sq.get("records"), "seriesId")
            for app, lists in (("radarr", rl), ("sonarr", sl)):
                if ok(lists):
                    names = {x["name"] for x in lists}
                    store.meta_put(f"want:variants:{app}", [v for v in VARIANTS if list_name(v) in names])
            for x in (movies, series, mq, sq):
                if isinstance(x, Exception):
                    log.warning("arr status: %s", x)
        finally:
            await http.close()


def kick_refresh():
    """From sync code (card render): refresh in the background if stale, never wait for it."""
    if time.time() - _st["ts"] < TTL:
        return
    try:
        asyncio.get_running_loop().create_task(status_refresh())
    except RuntimeError:
        pass


async def season_counts(http, item):
    if item["key"] not in _eps:
        d = await clients.tmdb(http, f"/tv/{item['tmdb']}")
        _eps[item["key"]] = {s["season_number"]: s.get("episode_count") or 0
                             for s in d.get("seasons", []) if s.get("season_number")}
    return _eps[item["key"]]


def _gb(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _pct(q):
    return round(100 * (1 - q["left"] / q["size"])) if q["size"] else 0


def card_status(c, opts=None, library=(), st=None):
    """Card dict → {"state", "label", "size"}; state ∈ lib | dl | queued | want | "" . Pure given st."""
    st = st or _st
    opts = opts or {}
    q = opts.get("quality") or "1080"
    state = label = size = ""
    if c["kind"] == "movie":
        m = st["movies"].get(c.get("tmdb"))
        qu = m and st["mq"].get(m["id"])
        if qu:
            state, label = ("dl", f"Downloading {_pct(qu)}%") if qu["active"] else ("queued", "Queued")
        elif c["key"] in library or (m and m.get("hasFile")):
            state, label = "lib", "In library"
        elif c.get("wanted") or (m and m.get("monitored")):
            state, label = "want", "Wanted"
        on_disk = (m or {}).get("sizeOnDisk") or 0
        size = f"{_gb(on_disk / 1e9)} GB" if state == "lib" and on_disk else f"~{_gb(MOVIE_GB[q])} GB"
    else:
        s = st["series"].get(("tvdb", c.get("tvdb"))) or st["series"].get(("tmdb", c.get("tmdb")))
        stats = (s or {}).get("statistics") or {}
        qu = s and st["sq"].get(s["id"])
        if qu:
            state, label = ("dl", f"Downloading {_pct(qu)}%") if qu["active"] else ("queued", "Queued")
        elif c["key"] in library or (stats.get("episodeCount") and stats.get("episodeFileCount", 0) >= stats["episodeCount"]):
            state, label = "lib", "In library"
        elif stats.get("episodeFileCount"):
            state, label = "lib", f"{stats['episodeFileCount']}/{stats.get('episodeCount') or '?'} eps"
        elif c.get("wanted") or (s and s.get("monitored")):
            state, label = "want", "Wanted"
        counts = _eps.get(c["key"]) or {}
        if counts:
            pick = opts.get("pick") or "all"
            nums = sorted(counts)
            chosen = {"latest": nums[-1:], "first": nums[:1], "custom": opts.get("seasons") or nums}.get(pick, nums)
            eps = sum(counts.get(n, 0) for n in chosen)
        else:
            eps = stats.get("totalEpisodeCount") or 0
        size = f"~{_gb(EP_GB[q])} GB/ep" + (f" × {eps} eps" if eps else "")
    return {"state": state, "label": label, "size": size}


# ---------------- setup: idempotent, dry-run by default ----------------
def _spec(name, impl, value):
    fields = [{"name": "value", "value": value}]
    if impl == "LanguageSpecification":
        fields.append({"name": "exceptLanguage", "value": False})
    return {"name": name, "implementation": impl, "negate": False, "required": False, "fields": fields}


def custom_formats():
    return [{"name": CF_TAMIL, "includeCustomFormatWhenRenaming": False, "specifications": [
                _spec("Tamil audio track", "LanguageSpecification", TAMIL_LANG),
                _spec("Tamil in title", "ReleaseTitleSpecification", TAMIL_RE),
                _spec("Multi-audio incl. ta", "ReleaseTitleSpecification", MULTI_RE)]},
            {"name": CF_1080, "includeCustomFormatWhenRenaming": False, "specifications": [
                _spec("1080p", "ResolutionSpecification", 1080)]}]


def tamil_profile(base, name, cfs, only720, app):
    """Clone of profile 6: all allowed HD qualities in ONE group (the arrs compare quality before CF score, so a
    group makes the Tamil score decide), 1080p CF breaks ties, upgrades only towards a Tamil release."""
    items, leaves, pos = [], [], None
    gid = max([i.get("id") or 0 for i in base["items"]] + [1000]) + 1
    for it in base["items"]:
        if not it.get("allowed"):
            items.append(it)
            continue
        lv = [{"quality": x["quality"], "items": [], "allowed": True} for x in (it.get("items") or [it])]
        if only720 and any(x["quality"]["resolution"] > 720 for x in lv):
            items.append({**it, "allowed": False, "items": [{**x, "allowed": False} for x in it.get("items") or []]})
            continue
        pos = len(items) if pos is None else pos
        leaves += lv
    items.insert(len(items) if pos is None else pos, {"id": gid, "name": "HD (Tamil first)", "items": leaves, "allowed": True})
    old = {f["name"]: f["score"] for f in base.get("formatItems", [])}
    score = {CF_TAMIL: TAMIL_SCORE, CF_1080: 0 if only720 else P1080_SCORE}
    p = {k: v for k, v in base.items() if k != "id"}
    p.update(name=name, items=items, upgradeAllowed=True, cutoff=gid, cutoffFormatScore=TAMIL_SCORE,
             minUpgradeFormatScore=MIN_UPGRADE,
             formatItems=[{"format": c.get("id"), "name": c["name"], "score": score.get(c["name"], old.get(c["name"], 0))}
                          for c in cfs])
    if app == "radarr":
        p["language"] = {"id": -1, "name": "Any"}   # "Original" would reject a Tamil-only dub
    return p


async def ensure_tamil(http, app, dry=True, say=print):
    """Custom formats "Tamil audio" + "Resolution 1080p" and the two "HD - Tamil preferred" profiles."""
    cfs = await clients.arr(http, app, "/customformat")
    for cf in custom_formats():
        if cf["name"] not in {c["name"] for c in cfs}:
            say(f"{app}: CREATE custom format '{cf['name']}' ({len(cf['specifications'])} specs, any one matches)")
            cfs.append(cf if dry else await clients.arr(http, app, "/customformat", "POST", cf))
    profiles = await clients.arr(http, app, "/qualityprofile")
    base = next(p for p in profiles if p["id"] == PROFILE_DEFAULT)
    for v, name in TAMIL_PROFILE.items():
        want = tamil_profile(base, name, cfs, v == "tamil720", app)
        have = next((p for p in profiles if p["name"] == name), None)
        allowed = [x["quality"]["name"] for x in want["items"][[i.get("id") for i in want["items"]].index(want["cutoff"])]["items"]]
        if not have:
            say(f"{app}: CREATE quality profile '{name}' (clone of id {PROFILE_DEFAULT}): one group {allowed}; "
                f"scores {[(f['name'], f['score']) for f in want['formatItems'] if f['score']]}; upgrades on until CF "
                f"score {TAMIL_SCORE}, min upgrade step {MIN_UPGRADE}" + ("; language Any" if app == "radarr" else ""))
            if not dry:
                await clients.arr(http, app, "/qualityprofile", "POST", want)
            continue
        fix = {**have, "upgradeAllowed": True, "cutoffFormatScore": TAMIL_SCORE, "minUpgradeFormatScore": MIN_UPGRADE,
               "formatItems": [{**f, "score": next((w["score"] for w in want["formatItems"] if w["name"] == f["name"]), f["score"])}
                               for f in have["formatItems"]]}
        if app == "radarr":
            fix["language"] = want["language"]
        if fix != have:
            say(f"{app}: UPDATE quality profile '{name}' scores/upgrade settings (qualities untouched)")
            if not dry:
                await clients.arr(http, app, f"/qualityprofile/{have['id']}", "PUT", fix)


def _with_variant(url, v):
    u = urllib.parse.urlsplit(url)
    q = [(k, x) for k, x in urllib.parse.parse_qsl(u.query) if k != "variant"] + ([("variant", v)] if v else [])
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def _redact(url):
    u = urllib.parse.urlsplit(url)
    q = [(k, "***" if k == "key" else x) for k, x in urllib.parse.parse_qsl(u.query)]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q, safe="*")))


SONARR_LIST = {"shouldMonitor": "none", "searchForMissingEpisodes": False, "enableAutomaticAdd": True}


def list_body(base, app, v, profile_id):
    url_field = "baseUrl" if app == "sonarr" else "link"
    body = {k: x for k, x in base.items() if k != "id"}
    body.update(name=list_name(v), qualityProfileId=profile_id,
                fields=[{**f, "value": _with_variant(f["value"], v)} if f["name"] == url_field else f for f in base["fields"]])
    if app == "sonarr":
        body.update(SONARR_LIST)
    return body


async def _save_list(http, app, have, body, dry, say):
    if have:
        new = {**have, **{k: v for k, v in body.items() if k != "fields"},
               "fields": body["fields"], "id": have["id"]}
        diff = sorted(k for k in ("qualityProfileId", "shouldMonitor", "searchForMissingEpisodes", "enableAutomaticAdd")
                      if new.get(k) != have.get(k))
        diff += ["url"] if [f.get("value") for f in new["fields"]] != [f.get("value") for f in have["fields"]] else []
        if diff:
            say(f"{app}: UPDATE import list '{body['name']}' (id {have['id']}): {', '.join(diff)}")
            if not dry:
                await clients.arr(http, app, f"/importlist/{have['id']}?forceSave=true", "PUT", new)
    else:
        url = next(f["value"] for f in body["fields"] if f["name"] in ("baseUrl", "link"))
        say(f"{app}: CREATE import list '{body['name']}' → {_redact(url)}, profile id {body['qualityProfileId']}"
            + (f", monitor {body['shouldMonitor']}, search on add {body['searchForMissingEpisodes']}" if app == "sonarr" else ""))
        if not dry:
            await clients.arr(http, app, "/importlist?forceSave=true", "POST", body)


async def ensure_lists(http, app, dry=True, say=print):
    """Variant import lists (Tamil / 720p / Tamil 720p) cloned from "Streamarr Discover", each with its profile."""
    lists = await clients.arr(http, app, "/importlist")
    base = next((x for x in lists if x["name"] == LIST), None)
    if not base:
        say(f"{app}: no '{LIST}' import list — nothing to clone")
        return
    pids = await _profile_ids(http, app)
    for v in ("tamil", "720", "tamil720"):
        if dry and TAMIL_PROFILE.get(v) and pids[v] in (PROFILE_DEFAULT, PROFILE_720):
            pids[v] = f"<new '{TAMIL_PROFILE[v]}'>"
        await _save_list(http, app, next((x for x in lists if x["name"] == list_name(v)), None),
                         list_body(base, app, v, pids[v]), dry, say)


async def ensure_sonarr_lists(http, dry=True, say=print):
    """Sonarr "Streamarr Discover": add with monitor none, no search, automatic add on — Discover then monitors and
    searches only the seasons you picked (apply_one)."""
    lists = await clients.arr(http, "sonarr", "/importlist")
    base = next((x for x in lists if x["name"] == LIST), None)
    if base:
        await _save_list(http, "sonarr", base, {**base, **SONARR_LIST}, dry, say)


async def ensure_all(http, dry=False, say=log.info):
    for app, key in (("sonarr", config.SONARR_KEY), ("radarr", config.RADARR_KEY)):
        if not key:
            continue
        if app == "sonarr":
            await ensure_sonarr_lists(http, dry, say)
        await ensure_tamil(http, app, dry, say)
        await ensure_lists(http, app, dry, say)
        if not dry:
            names = {x["name"] for x in await clients.arr(http, app, "/importlist")}
            store.meta_put(f"want:variants:{app}", [v for v in VARIANTS if list_name(v) in names])


async def _main(apply):
    http = clients.Http()
    try:
        print("APPLYING" if apply else "DRY RUN (nothing is changed; --apply to do it)")
        await ensure_all(http, dry=not apply, say=print)
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(_main("--apply" in sys.argv))
