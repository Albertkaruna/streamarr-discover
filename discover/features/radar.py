"""Release radar (📅): movies coming in the next ~4 months or in theatres now but not yet digital, the films you're
waiting for (wanted before release), "🔔 Notify me" tracking without downloading, and arrival alerts.

Why: new Tamil films get CAM rips within days (blocked by a -10000 custom format) and a proper WEB-DL ~4–8 weeks
after theatrical. Radarr's Discover lists use minimumAvailability "released", so a Want on an unreleased film just
makes Radarr wait for the digital release — this page shows that wait instead of leaving you guessing.

Computed once per refresh (POST_REFRESH) into meta "radar"; arrivals are also re-checked on render from the want
status cache (≤60 s old), so a film that lands at 03:00 shows up in the morning without waiting for the refresh.
"""
import asyncio
import datetime
import logging
import math
import time

from fastapi import Request
from fastapi.responses import HTMLResponse

from discover import clients, config, store
from discover import features as fx
from discover.features import want_arr as wa

log = logging.getLogger("discover")
DAY = clients.DAY
AHEAD, BACK = 0, 75                 # only films already in theatres (≤ 75 days ago) and not yet on OTT/digital
HYDRATE, STRIP = 40, 14             # full TMDB details for the top 40; Movies strip shows 14
MONTH = 30                          # "This month" = within 30 days (late in the month a calendar month is ~empty)
EST_IN, EST_OTHER = 42, 45          # theatrical → digital estimate (Indian films ≈ 6 weeks, others ≈ 45 days)
STALE = 21                          # estimated digital passed 3+ weeks ago → assume it's out, drop from "coming"
ARRIVED_DAYS = 14                   # how long an arrival stays on the banner / in "You're waiting for"
INDIAN = {"ta", "hi", "te", "ml", "kn", "bn", "mr", "pa", "gu", "or"}
QUERY_LANGS = ("ta", "hi", "te", "ml", "ko", "ja", "en")    # browse.py language groups TMDB can filter on
GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary", 18: "Drama",
          10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery",
          10749: "Romance", 878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western"}

fx.NAV.append(("/coming", "Coming"))
fx.PAGE_TOP.append("features/radar_top.html")
fx.CARD_SLOTS.append("features/radar_slot.html")
fx.CSS.append("/static/radar.css?v=1")
fx.JS.append("/static/radar.js?v=1")


# ---------------- notify-only tracking (own table) ----------------
def _db():
    con = store.db()
    con.execute("CREATE TABLE IF NOT EXISTS radar_notify (key TEXT PRIMARY KEY, ts REAL, done REAL DEFAULT 0)")
    return con


def notify_keys():
    return dict(_db().execute("SELECT key, done FROM radar_notify"))


def notify_toggle(key):
    with _db() as c:
        if c.execute("SELECT 1 FROM radar_notify WHERE key=?", (key,)).fetchone():
            c.execute("DELETE FROM radar_notify WHERE key=?", (key,))
            return False
        c.execute("INSERT INTO radar_notify VALUES (?,?,0)", (key, time.time()))
        return True


def _notify_done(keys):
    with _db() as c:
        c.executemany("UPDATE radar_notify SET done=? WHERE key=?", [(time.time(), k) for k in keys])


# ---------------- pure bits (tested) ----------------
def _d(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def nice(iso, today, month_only=False):
    """'2026-12-12' → '12 Dec' (year added when not this year); month_only → 'Dec'."""
    d = _d(iso)
    if not d:
        return "TBA"
    s = f"{d:%b}" if month_only else f"{d.day} {d:%b}"
    return s + (f" {d.year}" if d.year != today.year else "")


def release_info(rd, lang, fallback=""):
    """TMDB /movie/{id}/release_dates + original language → {theatrical, digital, est, physical, note}.
    Theatrical: India first (type 3, then 2), else earliest anywhere, else the primary date. Digital/physical are
    worldwide-earliest (an OTT premiere is usually global); no digital date → theatrical + 6 weeks / 45 days."""
    by = {}                                          # (country, type) → [(date, note)]
    for r in (rd or {}).get("results", []):
        for x in r.get("release_dates", []):
            if _d(x.get("release_date")):
                by.setdefault((r.get("iso_3166_1"), x.get("type")), []).append((x["release_date"][:10], x.get("note") or ""))

    def first(country, *types):
        rows = sorted(v for (c, t), vs in by.items() for v in vs if t in types and (country is None or c == country))
        return rows[0] if rows else ("", "")
    th = first("IN", 3)[0] or first("IN", 2)[0] or first(None, 3)[0] or first(None, 2)[0] or (fallback or "")[:10]
    dig, note = first("IN", 4)
    if not dig:
        dig, note = first(None, 4)
    est = not dig and bool(_d(th))
    if est:
        dig = (_d(th) + datetime.timedelta(days=EST_IN if lang in INDIAN else EST_OTHER)).isoformat()
    return {"theatrical": th, "digital": dig, "est": est, "physical": first(None, 5)[0], "note": note}


def group_of(info, today):
    """theatres | month | later | None (already out digitally → not "coming" any more)."""
    th, dig = _d(info.get("theatrical")), _d(info.get("digital"))
    if dig and dig <= today and (not info.get("est") or (today - dig).days > STALE):
        return None
    if th and th <= today:
        return "theatres"
    if th and (th - today).days <= MONTH:
        return "month"
    return "later"


def badge(info, today):
    th = info.get("theatrical")
    if _d(th) and _d(th) <= today:
        return "In theatres"
    return f"Out {nice(th, today)}" if _d(th) else "Date TBA"


def digital_label(info, today):
    dig = info.get("digital")
    if not _d(dig):
        return ""
    if info.get("est"):
        return f"Digital expected ~{nice(dig, today, month_only=True)}"
    return f"Digital {nice(dig, today)}" + (f" · {info['note']}" if info.get("note") else "")


def genre_weights(seeds_w, items, n=40):
    """Genres of your top movie seeds, weighted like the seeds, max → 1."""
    gw = {}
    top = sorted(((k, w) for k, w in seeds_w.items() if w > 0 and k.startswith("movie:") and k in items),
                 key=lambda x: -x[1])[:n]
    for k, w in top:
        for g in items[k].get("genres") or []:
            gw[g] = gw.get(g, 0) + w
    m = max(gw.values(), default=0) or 1
    return {g: w / m for g, w in gw.items()}


def score(r, gw, prefs, max_pop):
    """Light taste fit: best two genres vs your seeds (half) + popularity (half) + a nudge for Tamil / "Lots".
    max_pop is per language: Hollywood popularity dwarfs a big Tamil release, and that's not what "big" means here."""
    fits = sorted((gw.get(GENRES.get(g, ""), 0) for g in r.get("genre_ids") or []), reverse=True)[:2]
    fit = sum(fits) / 2 if fits else 0
    lang = r.get("original_language")
    pop = math.log1p(r.get("popularity") or 0) / math.log1p(max_pop.get(lang) or 1)
    return 0.5 * fit + 0.5 * pop + (0.15 if prefs.get(lang) == "lots" else 0) + (0.1 if lang == "ta" else 0)


def langs_to_query(prefs):
    """Languages set to Some/Lots in Settings → TMDB with_original_language; Tamil always (house language)."""
    return ["ta"] + [g for g in QUERY_LANGS if g != "ta" and prefs.get(g, "some") in ("some", "lots")]


def lang_ok(lang, prefs):
    g = lang if lang in QUERY_LANGS else "other"
    return lang == "ta" or prefs.get(g, "some") != "off"


def detect_arrivals(prev_waiting, wanted, fresh, movies, notify, dates, today):
    """→ (arrivals [(key, how)], waiting keys, notify keys now done).
    wanted: wanted movie keys; fresh: those not out yet (TMDB unreleased / Radarr not available); movies: {tmdb id:
    Radarr movie}; notify: {key: done ts}; dates: {key: release_info}.
    A want arrives when Radarr has the file and we saw it waiting before — so old wants that simply finish
    downloading, and everything on the very first run, don't alert. It stays "waiting" from pre-release through
    Radarr's search/download. A notify-only film arrives when its real (not estimated) digital/physical date passed."""
    arrivals, waiting = [], set()
    for k in wanted:
        m = movies.get(int(k.split(":")[1]))
        if m and m.get("hasFile"):
            if k in prev_waiting:
                arrivals.append((k, "downloaded"))
        elif k in prev_waiting or k in fresh:
            waiting.add(k)
    done = []
    for k, was in notify.items():
        info = dates.get(k) or {}
        real = [d for d in (None if info.get("est") else _d(info.get("digital")), _d(info.get("physical"))) if d]
        if not was and real and min(real) <= today:
            arrivals.append((k, "digital"))
            done.append(k)
    return arrivals, waiting, done


def waiting_status(m, dl, info, item, today):
    """One line for a wanted-before-release film: where it is between Radarr and your library."""
    if m is None:
        return ("Radarr can't take it yet — no IMDb id on TMDB" if not (item or {}).get("imdb")
                else "On its way to Radarr (next list sync)")
    if m.get("hasFile"):
        return "Arrived ✅"
    if dl.get("state") in ("dl", "queued"):
        return f"{dl['label']} · full speed 22:00–09:00"
    if not m.get("monitored"):
        return "Not monitored in Radarr"
    if m.get("isAvailable"):
        return "Out — Radarr is searching for a proper release (CAM/TS skipped)"
    rdig = (m.get("digitalRelease") or "")[:10]
    d = {"digital": rdig, "est": False} if _d(rdig) else (info or {})
    when = digital_label(d, today)
    return "Radarr waits for the digital release" + (f" · {when[8:]}" if when else "")


# ---------------- refresh: gather, rank, hydrate, dates, arrivals ----------------
def _prefs():
    p = store.pref("languages") or {}
    return {g: v for g, v in p.items() if v in ("off", "some", "lots")}


async def _candidates(http, prefs, today):
    # released in theatres only (not future films): those are the ones whose HD/OTT release is next
    lo, hi = (today - datetime.timedelta(days=BACK)).isoformat(), today.isoformat()
    calls = []
    for lang in langs_to_query(prefs):
        for page in ((1, 2) if lang == "ta" else (1,)):
            calls.append(clients.tmdb(http, "/discover/movie", cache=DAY, region="IN", with_original_language=lang,
                                      sort_by="popularity.desc", include_adult="false", page=page,
                                      **{"primary_release_date.gte": lo, "primary_release_date.lte": hi}))
    out = {}
    for r in await asyncio.gather(*calls, return_exceptions=True):
        if isinstance(r, Exception):
            log.warning("radar: tmdb list: %s", r)
            continue
        for x in r.get("results") or []:
            if x.get("id") and not x.get("adult") and lang_ok(x.get("original_language"), prefs):
                out.setdefault(x["id"], x)
    return list(out.values())


async def _radarr_movies(http):
    if not config.RADARR_KEY:
        return None
    try:
        return {m["tmdbId"]: m for m in await clients.arr(http, "radarr", "/movie") if m.get("tmdbId")}
    except Exception as ex:
        log.warning("radar: radarr: %s", ex)
        return None


async def compute(ctx):
    http, today = ctx["http"], datetime.date.today()
    prefs = _prefs()
    gw = genre_weights(ctx.get("seeds_w") or store.meta_get("seeds_w") or {}, ctx.get("items") or {})
    raw = await _candidates(http, prefs, today)
    skip = set(ctx.get("owned") or ()) | set(ctx.get("exclude") or ())
    max_pop = {}
    for r in raw:
        lang = r.get("original_language")
        max_pop[lang] = max(max_pop.get(lang, 20), r.get("popularity") or 0)   # floor: a lone obscure film isn't "the biggest"
    raw = [r for r in raw if f"movie:{r['id']}" not in skip]
    raw.sort(key=lambda r: -score(r, gw, prefs, max_pop))
    primary = {f"movie:{r['id']}": r.get("release_date") or "" for r in raw}
    scores = {f"movie:{r['id']}": round(score(r, gw, prefs, max_pop), 3) for r in raw[:HYDRATE]}
    items = {i["key"]: i for i in await _gather([clients.tmdb_item(http, "movie", k.split(":")[1]) for k in scores])}

    movies = await _radarr_movies(http)
    wanted = [k for k in store.wants() if k.startswith("movie:")]
    witems = store.items_get(wanted)
    track = [k for k in wanted if k in witems and (not witems[k].get("released")
             or (movies is not None and not (movies.get(witems[k]["tmdb"]) or {}).get("isAvailable", True)))]
    track += [k for k in notify_keys() if k not in track]
    keys = list(dict.fromkeys(list(items) + track))
    meta = {**items, **store.items_get(k for k in keys if k not in items)}

    async def rd(k):
        return k, await clients.tmdb(http, f"/movie/{k.split(':')[1]}/release_dates", cache=DAY)
    dates = {}
    for k, r in await _gather([rd(k) for k in keys if k in meta]):
        it = meta[k]
        # the list's release_date (primary/earliest) is the fallback when release_dates is empty
        dates[k] = release_info(r, it.get("lang"), primary.get(k, ""))
    coming = [{"key": k, "score": scores[k]} for k in scores
              if k in items and not items[k].get("adult") and k in dates and group_of(dates[k], today) == "theatres"]
    store.meta_put("radar", {"day": today.isoformat(), "coming": coming, "dates": dates})
    check_arrivals(movies, today)
    log.info("radar: %d coming, %d tracked", len(coming), len(track))


async def _gather(coros):
    res = await asyncio.gather(*coros, return_exceptions=True)
    return [r for r in res if not isinstance(r, Exception)]


fx.POST_REFRESH.append(compute)


def check_arrivals(movies, today=None):
    """Record new arrivals (meta "radar:arrivals"). movies=None (Radarr unknown) → only notify-only checks."""
    today = today or datetime.date.today()
    dates = (store.meta_get("radar") or {}).get("dates", {})
    prev = set(store.meta_get("radar:waiting", []))
    wanted = [k for k in store.wants() if k.startswith("movie:")] if movies is not None else []
    items = store.items_get(wanted)
    fresh = {k for k in wanted if (k in items and not items[k].get("released"))
             or not ((movies or {}).get(int(k.split(":")[1])) or {}).get("isAvailable", True)}
    arr, waiting, done = detect_arrivals(prev, wanted, fresh, movies or {}, notify_keys(), dates, today)
    if movies is not None and waiting != prev:
        store.meta_put("radar:waiting", sorted(waiting))
    if done:
        _notify_done(done)
    if arr:
        items = store.items_get(k for k, _ in arr)
        old = [a for a in store.meta_get("radar:arrivals", []) if time.time() - a["ts"] < ARRIVED_DAYS * DAY]
        old += [{"key": k, "title": (items.get(k) or {}).get("title", k), "how": how, "ts": time.time()}
                for k, how in arr if k not in {a["key"] for a in old}]
        store.meta_put("radar:arrivals", old)
        log.info("radar: %d arrived", len(arr))


def _live_check():
    """Render-time arrival check from the want status cache (never waits on Radarr)."""
    wa.kick_refresh()
    if wa._st["ts"] and wa._st["movies"]:
        try:
            check_arrivals(wa._st["movies"])
        except Exception as ex:
            log.warning("radar: arrivals: %s", ex)


def arrivals():
    """Undismissed recent arrivals for the banner."""
    gone = store.meta_get("radar:dismissed", 0)
    return [a for a in store.meta_get("radar:arrivals", [])
            if a["ts"] > gone and time.time() - a["ts"] < ARRIVED_DAYS * DAY]


# ---------------- card data + Movies strip ----------------
def _card_data(cards):
    """Unreleased / not-yet-digital movies get their dates + the 🔔 Notify toggle (slot renders only for these)."""
    if not any(c.get("kind") == "movie" for c in cards):
        return
    dates, today, nk = (store.meta_get("radar") or {}).get("dates", {}), datetime.date.today(), None
    for c in cards:
        info = dates.get(c["key"])
        if c.get("kind") != "movie" or (c.get("released") and not (info and group_of(info, today))):
            continue
        nk = notify_keys() if nk is None else nk
        c["radar"] = {"badge": badge(info, today) if info else "Coming soon",
                      "digital": digital_label(info, today) if info else "", "notify": c["key"] in nk}


fx.CARD_DATA.append(_card_data)


def top():
    """Movies-page strip + arrivals banner. Cheap: two meta reads + one items read."""
    _live_check()
    data, today = store.meta_get("radar") or {}, datetime.date.today()
    dates, wants = data.get("dates", {}), store.wants()
    keys = [c["key"] for c in data.get("coming", []) if group_of(dates.get(c["key"], {}), today)][:STRIP]
    items = store.items_get(keys)
    tiles = []
    for k in keys:
        if k in items:
            info = dates[k]
            sub = digital_label(info, today).replace("Digital expected ", "Digital ") if badge(info, today) == "In theatres" else ""
            tiles.append({**items[k], "badge": badge(info, today), "sub": sub, "wanted": k in wants})
    return {"tiles": tiles, "arrivals": arrivals()}


# ---------------- /coming page + actions ----------------
def _waiting(today):
    """Wanted films that aren't out yet (or arrived in the last 2 weeks) with their Radarr status line."""
    dates = (store.meta_get("radar") or {}).get("dates", {})
    wants = store.wants()
    keys = [k for k in sorted(wants, key=lambda k: -wants[k]) if k.startswith("movie:")]
    items = store.items_get(keys)
    recent = {a["key"] for a in store.meta_get("radar:arrivals", []) if time.time() - a["ts"] < ARRIVED_DAYS * DAY}
    lib, opts, out = set(store.meta_get("library", [])), wa.opts_all(), []
    for k in keys:
        it = items.get(k)
        if not it:
            continue
        m = wa._st["movies"].get(it["tmdb"]) if wa._st["ts"] else None
        info = dates.get(k)
        unreleased = not it.get("released") or (info and group_of(info, today)) or \
            (m and not m.get("hasFile") and not m.get("isAvailable"))
        if not (unreleased or k in recent):
            continue
        if m is None and not wa._st["ts"]:
            line = "Checking Radarr…"
        else:
            line = waiting_status(m, wa.card_status({**it, "wanted": True}, opts.get(k), lib), info, it, today)
        when = badge(info, today) if info else ""
        out.append({"key": k, "score": 0, "because": [], "why": f"{when} · {line}" if when else line,
                    "arrived": line == "Arrived ✅"})
    return out


@fx.router.get("/coming", response_class=HTMLResponse)
async def coming_page(req: Request):
    from discover import app       # lazy: app imports the feature registry
    await wa.status_refresh()      # ≤60 s cache; one Radarr round-trip at most
    _live_check()
    data, today = store.meta_get("radar") or {}, datetime.date.today()
    dates, wants = data.get("dates", {}), store.wants()
    groups = {"theatres": [], "month": [], "later": []}
    for c in data.get("coming", []):
        info = dates.get(c["key"]) or {}
        g = group_of(info, today)
        if g and c["key"] not in wants:
            groups[g].append({"key": c["key"], "score": c["score"], "because": [], "_d": info.get("theatrical") or "9",
                              "why": ""})      # the radar card slot already shows the dates (was printed twice)
    for g in ("month", "later"):                 # soonest first; theatres keep taste order
        groups[g].sort(key=lambda p: p["_d"])
    waiting = app.cards_for(_waiting(today))
    sections = [("waiting", "You're waiting for", "Wanted before release — Radarr grabs the proper WEB-DL when it's out "
                 "(CAM/TS rips are skipped).", waiting),
                ("theatres", "In theatres · waiting for OTT", "Out in cinemas, not on streaming yet — the HD release "
                 "usually lands 4–8 weeks after the theatrical date.", app.cards_for(groups["theatres"]))]
    return app.render_page(req, "coming", [], template="features/coming.html", sections=sections,
                           arrivals=arrivals(), computed=bool(data))


@fx.router.post("/radar/notify/{key}")
async def notify(key: str):
    if not key.startswith("movie:") or not store.items_get([key]):
        return {"ok": False}
    return {"ok": True, "on": notify_toggle(key)}


@fx.router.post("/radar/dismiss")
async def dismiss():
    store.meta_put("radar:dismissed", time.time())
    return {"ok": True}


@fx.router.get("/api/radar")
async def api():
    _live_check()
    data, today = store.meta_get("radar") or {}, datetime.date.today()
    dates, wants = data.get("dates", {}), store.wants()
    n = sum(1 for c in data.get("coming", []) if c["key"] not in wants and group_of(dates.get(c["key"], {}), today))
    return {"coming": n, "waiting": sum(not w["arrived"] for w in _waiting(today)),
            "arrived": [a["title"] for a in arrivals()]}
