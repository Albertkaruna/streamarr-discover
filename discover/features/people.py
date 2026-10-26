"""Person pages (/person/{tmdb id}): photo, bio, and a filmography split into Directed / Music / Acted —
each title on the normal card so library status and + Want work, with "You have X of Y" per section.
Composers matter here (Tamil film music: TMDB credits them as "Music" / "Original Music Composer").
Entry points: search "People" row, explain-sheet names (via /person/by-name), nothing on cards."""
import asyncio
import datetime
import logging
import re
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from discover import clients, store
from discover import features as fx

log = logging.getLogger("discover")
DAY = 86400
HYDRATE = 40         # TMDB detail calls per page view; the rest are light rows you can tap to load
CARDS = 24           # cards per section before falling back to light rows
LIGHT = 150          # light rows per section (a prolific actor has 300+ credits)
MIN_VOTES, PRIOR_N, PRIOR_MEAN = 10, 50, 6.5   # small prior: Tamil films often have 20–200 votes, not 20k

DIRECT = {"Director", "Creator", "Writer", "Screenplay", "Story"}
MUSIC = {"Original Music Composer", "Music", "Music Director", "Composer", "Songs", "Original Score Composer"}
SECTIONS = (("direct", "Directed / Created"), ("music", "Music"), ("cast", "Acted in"), ("prod", "Produced"))
FIRST = {"Directing": "direct", "Writing": "direct", "Creator": "direct", "Sound": "music", "Acting": "cast",
         "Production": "prod"}
NOISE_TV = {10763, 10764, 10767}                     # news, reality, talk: award shows/interviews, not their work
SELF = re.compile(r"\b(him|her|them)?self\b|\bguest\b|\bhost\b", re.I)
FILTERS = (("all", "All"), ("missing", "Missing"), ("lib", "In library"))
SORTS = (("best", "Best rated"), ("new", "Newest"), ("old", "Oldest"))


# ---------------- pure helpers (tested) ----------------
def bayes(r):
    v = r.get("votes") or 0
    return (v * (r.get("rating") or 0) + PRIOR_N * PRIOR_MEAN) / (v + PRIOR_N)


def split_credits(cr, dept=""):
    """combined_credits → {section: [row]}; one row per title per section, roles merged ("Director · Writer")."""
    secs = {s: {} for s, _ in SECTIONS}

    def add(sec, w, role):
        kind = w.get("media_type")
        if kind not in ("movie", "tv") or w.get("adult"):
            return
        key = f"{kind}:{w['id']}"
        row = secs[sec].get(key)
        if row:
            if role and role not in row["roles"]:
                row["roles"].append(role)
            return
        secs[sec][key] = {"key": key, "kind": kind, "tmdb": w["id"], "title": w.get("title") or w.get("name") or "",
                          "date": w.get("release_date") or w.get("first_air_date") or "", "poster": w.get("poster_path"),
                          "rating": w.get("vote_average") or 0, "votes": w.get("vote_count") or 0,
                          "popularity": w.get("popularity") or 0, "roles": [role] if role else []}

    for w in cr.get("crew") or []:
        job = w.get("job") or ""
        if job in DIRECT:
            add("direct", w, job)
        elif job in MUSIC or (w.get("department") == "Sound" and "Compos" in job):
            add("music", w, "Music")
        elif job == "Producer" and dept == "Production":      # producing credits only for actual producers
            add("prod", w, job)
    for w in cr.get("cast") or []:
        ch = (w.get("character") or "").strip()
        if SELF.search(ch) or NOISE_TV & set(w.get("genre_ids") or []):
            continue
        add("cast", w, f"as {ch}" if ch else "")
    return {s: list(rows.values()) for s, rows in secs.items() if rows}


def order(rows, sort):
    if sort == "new":                                   # undated (announced) titles first, they're the newest
        return sorted(rows, key=lambda r: r["date"] or "9999", reverse=True)
    if sort == "old":
        return sorted(rows, key=lambda r: r["date"] or "9999")
    rated = sorted((r for r in rows if r["votes"] >= MIN_VOTES), key=lambda r: -bayes(r))
    return rated + sorted((r for r in rows if r["votes"] < MIN_VOTES), key=lambda r: -r["popularity"])


def keep(rows, f):
    if f == "lib":
        return [r for r in rows if r["state"] == "lib"]
    if f == "missing":
        return [r for r in rows if r["state"] != "lib"]
    return rows


def section_order(secs, dept):
    first = FIRST.get(dept)
    return sorted(secs, key=lambda s: (s != first, -len(secs[s])))


# ---------------- data ----------------
def _status(rows):
    """Library / download state for every row from the want feature's in-memory arr cache (no TMDB calls)."""
    lib, wants = set(store.meta_get("library", [])), store.wants()
    try:
        from discover.features import want_arr as wa
        wa.kick_refresh()
        opts = wa.opts_all()
        for r in rows:
            r["state"] = wa.card_status({**r, "wanted": r["key"] in wants}, opts.get(r["key"]), lib)["state"]
    except Exception as ex:                             # want feature missing/broken → library + wants only
        log.warning("people status: %s", ex)
        for r in rows:
            r["state"] = "lib" if r["key"] in lib else "want" if r["key"] in wants else ""
    for r in rows:
        r["label"] = {"lib": "✅ In library", "dl": "Downloading", "queued": "Queued", "want": "Wanted"}.get(r["state"], "")


async def _hydrate(http, rows):
    res = await asyncio.gather(*[clients.tmdb_item(http, r["kind"], r["tmdb"]) for r in rows], return_exceptions=True)
    for r, x in zip(rows, res):
        if isinstance(x, Exception):
            log.warning("person hydrate %s: %s", r["key"], x)


def _qs(q, **changes):
    d = {**dict(q), **changes}
    return "?" + urllib.parse.urlencode({k: v for k, v in d.items() if v not in ("", None, "all", "best")})


@fx.router.get("/person/by-name")
async def by_name(name: str = ""):
    """Explain sheets only know names → TMDB person search (cached 30 days), exact name preferred."""
    name = name.strip()[:100]
    if not name:
        return Response(status_code=404)
    http = clients.Http()
    try:
        r = await clients.tmdb(http, "/search/person", cache=30 * DAY, query=name)
    except Exception as ex:                     # TMDB unreachable (hotspot blip) → let search try again
        log.warning("person by-name: %s", ex)
        r = {}
    finally:
        await http.close()
    res = r.get("results") or []
    hit = next((p for p in res if (p.get("name") or "").lower() == name.lower()), res[0] if res else None)
    if not hit:
        return RedirectResponse(f"/search?q={urllib.parse.quote(name)}", status_code=303)
    return RedirectResponse(f"/person/{hit['id']}", status_code=303)


@fx.router.get("/person/card/{key}", response_class=HTMLResponse)
async def light_card(req: Request, key: str):
    """Tap on a light row → fetch details once (cached) and return the normal card, so + Want works."""
    from discover import app
    kind, _, tid = key.partition(":")
    if kind not in ("movie", "tv") or not tid.isdigit():
        return Response(status_code=404)
    if key not in store.items_get([key]):
        http = clients.Http()
        try:
            await clients.tmdb_item(http, kind, int(tid))
        finally:
            await http.close()
    cards = app.cards_for([{"key": key, "score": 0, "why": req.query_params.get("why", "")[:80], "because": []}])
    if not cards:
        return Response(status_code=404)
    return app.tpl.TemplateResponse(req, "_card.html", {"c": cards[0]})


@fx.router.get("/person/{pid}", response_class=HTMLResponse)
async def person_page(req: Request, pid: int):
    from discover import app
    http = clients.Http()
    try:
        try:
            p, cr = await asyncio.gather(clients.tmdb(http, f"/person/{pid}", cache=14 * DAY),
                                         clients.tmdb(http, f"/person/{pid}/combined_credits", cache=14 * DAY))
        except Exception as ex:                         # unknown id / TMDB down: say so instead of a 500
            log.warning("person %s: %s", pid, ex)
            return app.render_page(req, "person", [], template="features/person.html", p={}, born="", secs=[],
                                   filters=FILTERS, sorts=SORTS)
        dept = p.get("known_for_department") or ""
        secs = split_credits(cr, dept)
        _status([r for rows in secs.values() for r in rows])
        q, budget, out, todo = req.query_params, HYDRATE, [], []
        have = set(store.items_get(r["key"] for rows in secs.values() for r in rows))
        for s in section_order(secs, dept):
            f, sort = q.get(s, "all"), q.get(f"{s}_sort", "best")
            f, sort = (f if f in dict(FILTERS) else "all"), (sort if sort in dict(SORTS) else "best")
            rows = order(keep(secs[s], f), sort)
            top = rows[:CARDS]
            need = [r for r in top if r["key"] not in have][:budget]
            budget -= len(need)
            todo += need
            out.append({"id": s, "label": dict(SECTIONS)[s], "f": f, "sort": sort, "top": top, "rest": rows[CARDS:],
                        "total": len(secs[s]), "have": sum(r["state"] == "lib" for r in secs[s]),
                        "n": {k: len(keep(secs[s], k)) for k, _ in FILTERS},
                        "links": {"f": {k: _qs(q, **{s: k}) + f"#{s}" for k, _ in FILTERS},
                                  "sort": {k: _qs(q, **{f"{s}_sort": k}) + f"#{s}" for k, _ in SORTS}}})
        await _hydrate(http, todo)
    finally:
        await http.close()
    have = set(store.items_get(r["key"] for sec in out for r in sec["top"]))
    for sec in out:   # what got hydrated → normal cards; anything that failed drops to the light rows
        cards = app.cards_for([{"key": r["key"], "score": 0, "why": " · ".join(r["roles"])[:80], "because": []}
                               for r in sec["top"] if r["key"] in have])
        shown = {c["key"] for c in cards}
        sec["cards"] = cards
        sec["rest"] = ([r for r in sec["top"] if r["key"] not in shown] + sec["rest"])[:LIGHT]
        sec["more"] = max(0, sec["n"][sec["f"]] - len(cards) - len(sec["rest"]))
    born = p.get("birthday") or ""
    try:
        born = datetime.date.fromisoformat(born).strftime("%-d %b %Y") if born else ""
    except ValueError:
        pass
    return app.render_page(req, "person", [], template="features/person.html", p=p, born=born, secs=out,
                           filters=FILTERS, sorts=SORTS)


fx.CSS.append("/static/people.css?v=1")
fx.JS.append("/static/people.js?v=1")
