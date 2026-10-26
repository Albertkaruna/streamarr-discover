"""Tonight's pick: one title a day from your top picks, for the Homepage tile (/api/tonight) and a hero card on
Movies. Weekdays prefer a movie under ~2h; weekends may pick a show. Stable for the day, rotates day to day."""
import datetime

from discover import clients, store
from discover import features as fx

TOP_MOVIES, TOP_SHOWS, SHORT_MIN, RECENT = 10, 5, 125, 7
CSS = "/static/alerts.css"
IMG = "https://image.tmdb.org/t/p/w342"


def choose(pool, today, recent=(), runtimes=None):
    """pool = ranked keys → one key, deterministic per date. Weekday: movies ≤ SHORT_MIN (unknown runtime is fine,
    unless shorter ones exist); weekend: shows too. Skips the last few days' picks while there are others."""
    runtimes = runtimes or {}
    weekend = today.weekday() >= 5
    pool = [k for k in pool if weekend or k.startswith("movie:")]
    if not weekend:
        short = [k for k in pool if 0 < runtimes.get(k, 0) <= SHORT_MIN]
        pool = short or pool
    fresh = [k for k in pool if k not in recent] or pool
    return fresh[today.toordinal() % len(fresh)] if fresh else None


def _pool():
    """Top movie (and show) picks you haven't wanted or ✕'d, ranked."""
    fb, wants = store.feedback(), store.wants()
    ok = lambda p: p["key"] not in wants and fb.get(p["key"]) != "dislike"  # noqa: E731
    return ([p["key"] for p in store.picks("movie") if ok(p)][:TOP_MOVIES] +
            [p["key"] for p in store.picks("tv") if ok(p)][:TOP_SHOWS])


def pick(today=None):
    """Today's pick as a card-ish dict (item + pick + runtime + wanted), or None before the first refresh.
    Stays put for the day even after you + Want it (only ✕ replaces it)."""
    today = today or datetime.date.today()
    state = store.meta_get("tonight", {})
    key = state.get("key") if state.get("date") == today.isoformat() else None
    if not key or store.feedback().get(key) == "dislike":
        recent = store.meta_get("tonight:recent", [])
        key = choose(_pool(), today, recent, store.meta_get("tonight:runtime", {}))
        if not key:
            return None
        store.meta_put("tonight", {"date": today.isoformat(), "key": key})
        store.meta_put("tonight:recent", ([key] + [k for k in recent if k != key])[:RECENT])
    it = store.items_get([key]).get(key)
    if not it:
        return None
    p = next((p for p in store.picks(it["kind"]) if p["key"] == key), {"because": []})
    why = p.get("why") or (f"Because you watched {' and '.join(p['because'][:2])}." if p.get("because") else "")
    return {**it, **p, "why": why, "wanted": key in store.wants(),
            "runtime": store.meta_get("tonight:runtime", {}).get(key)}


async def runtimes(ctx):
    """Runtimes of the top movie picks — read from the TMDB details tmdb_item already cached (same request)."""
    out = {}
    for p in store.picks("movie")[:TOP_MOVIES * 2]:
        try:
            d = await clients.tmdb(ctx["http"], f"/movie/{p['key'].split(':')[1]}",
                                   append_to_response="recommendations,similar,keywords,credits,external_ids")
            out[p["key"]] = d.get("runtime") or 0
        except Exception:
            pass
    store.meta_put("tonight:runtime", out)


fx.POST_REFRESH.append(runtimes)
fx.PAGE_TOP.append("features/tonight_top.html")
if CSS not in fx.CSS:
    fx.CSS.append(CSS)


@fx.router.get("/api/tonight")
async def api_tonight():
    t = pick()
    if not t:
        return {"title": "No pick yet", "year": "", "kind": "", "why": "", "rating": "", "poster": "", "url": ""}
    return {"title": t["title"], "year": t["year"], "kind": "Movie" if t["kind"] == "movie" else "Show",
            "why": t["why"], "rating": round(t.get("rating") or 0, 1), "poster": IMG + t["poster"] if t["poster"] else "",
            "url": f"https://www.themoviedb.org/{t['kind']}/{t['tmdb']}"}
