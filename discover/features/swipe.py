"""Swipe: a one-card-at-a-time deck for rating picks fast (👍 / ✕ / + Want / Seen it) with library titles mixed in
(Loved / OK / Meh), and a "why not?" sheet behind every card's ✕ (Seen it, or Not interested because…).

Reasons feed the next refresh lightly: they only re-order picks (never add or drop), with a capped push-down."""
import datetime
import random
import time

from fastapi import Request, Response
from fastapi.responses import HTMLResponse

from discover import store
from discover import features as fx
from discover.features import taste

SCHEMA = """
CREATE TABLE IF NOT EXISTS swipe_log (key TEXT PRIMARY KEY, action TEXT, ts REAL);  -- last swipe per title (for undo)
CREATE TABLE IF NOT EXISTS swipe_reasons (key TEXT PRIMARY KEY, reason TEXT, lang TEXT, genre TEXT, ts REAL);
"""
ACTIONS = ("like", "dislike", "want", "loved", "ok", "meh", "skip")
REASONS = {"slow": "🐢 Too slow", "lang": "🗣️ Not my language", "genre": "🎭 Not my genre",
           "enough": "🔁 Seen enough like it", "no": "✋ Just no"}
LIB_EVERY = 4                  # every 4th card is one of your own titles (rate what you've seen)
SKIP_DAYS = 7                  # a skipped card comes back after a week
HALF_LIFE = 180 * 86400        # reasons fade: one from 6 months ago counts half
# push-down per (decayed) use of a reason, and the cap — on a 1 → 0 position scale, like browse's language boost
STEP = {"lang": 0.06, "slow": 0.04, "genre": 0.05}
CAP = {"lang": 0.25, "slow": 0.2, "genre": 0.2}
MAX_PUSH = 0.35                # all reasons together: a #1 of 60 never falls below ~#22
_ready = set()


def _db():
    con = store.db()
    if id(con) not in _ready:
        con.executescript(SCHEMA)
        _ready.add(id(con))
    return con


def _midnight():
    return datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()


def today_count():
    return _db().execute("SELECT COUNT(*) FROM swipe_log WHERE ts>=? AND action!='skip'", (_midnight(),)).fetchone()[0]


def log(key, action):
    with _db() as c:
        c.execute("REPLACE INTO swipe_log VALUES (?,?,?)", (key, action, time.time()))


def apply(key, action):
    """Record one swipe. Want itself goes through POST /act/want (so its defaults + list sync apply); we only log it."""
    if action in ("like", "dislike"):
        store.set_feedback(key, action)
    elif action in ("loved", "ok", "meh"):
        taste.set_rating(key, action)
    log(key, action)


def undo(key):
    """Reverse the last swipe on this title. Cards only reach the deck unrated/unwanted, so "before" = nothing."""
    row = _db().execute("SELECT action FROM swipe_log WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    a = row[0]
    if a in ("like", "dislike"):
        store.set_feedback(key, None)
    elif a == "want":
        store.remove_want(key)
    elif a in ("loved", "ok", "meh"):
        taste.set_rating(key, "")
    with _db() as c:
        c.execute("DELETE FROM swipe_log WHERE key=?", (key,))
    return a


def set_reason(key, r):
    """Remember why you ✕'d it, with the title's language + top genre as they are now (items can change later)."""
    with _db() as c:
        if r not in REASONS:
            c.execute("DELETE FROM swipe_reasons WHERE key=?", (key,))
            return
        it = store.items_get([key]).get(key) or {}
        c.execute("REPLACE INTO swipe_reasons VALUES (?,?,?,?,?)",
                  (key, r, it.get("lang") or "", (it.get("genres") or [""])[0], time.time()))


def reason_rows():
    return _db().execute("SELECT key, reason, lang, genre, ts FROM swipe_reasons").fetchall()


# ---------------- pure helpers (tested) ----------------
def reason_weights(rows, now=None):
    """[(key, reason, lang, genre, ts)] → {"lang": {code: w}, "genre": {name: w}, "slow": w}, each use decayed."""
    now = now or time.time()
    out = {"lang": {}, "genre": {}, "slow": 0.0}
    for _, r, lang, genre, ts in rows:
        w = 0.5 ** (max(0, now - ts) / HALF_LIFE)
        if r == "lang" and lang:
            out["lang"][lang] = out["lang"].get(lang, 0) + w
        elif r == "genre" and genre:
            out["genre"][genre] = out["genre"].get(genre, 0) + w
        elif r == "slow":
            out["slow"] += w
    return out


def push(item, moods, wts):
    """How far down (0..MAX_PUSH on the 1 → 0 position scale) your ✕ reasons move one title.
    - "not my language": proportional to how often you said it about titles in this language
    - "too slow": every pick tagged slow-burn, by how often you said it at all
    - "not my genre": titles whose top genre is the one you rejected (half if it's only a secondary genre)
    "Seen enough like it" / "Just no" are plain dislikes — the engine's dislike profile already handles likeness."""
    p = min(CAP["lang"], STEP["lang"] * wts["lang"].get(item.get("lang") or "", 0))
    if "slowburn" in (moods or ()):
        p += min(CAP["slow"], STEP["slow"] * wts["slow"])
    gs = item.get("genres") or []
    g = max([wts["genre"].get(gs[0], 0) if gs else 0] + [wts["genre"].get(x, 0) / 2 for x in gs[1:]])
    p += min(CAP["genre"], STEP["genre"] * g)
    return min(MAX_PUSH, p)


def reason_rerank(rows, item_of, wts):
    """Re-order only: position value 1 → 0 minus push, stable. Nothing is added or dropped."""
    n = len(rows) or 1
    val = {id(r): 1 - i / n - push(item_of(r), (r.get("extra") or {}).get("moods"), wts) for i, r in enumerate(rows)}
    return sorted(rows, key=lambda r: -val[id(r)])


# ---------------- refresh hook + Movies/Shows filter ----------------
async def refresh_hook(ctx):
    """Runs after browse (alphabetical), so moods are set. Titles you've rated are seen → drop them; the pipeline only
    excludes owned / wanted / ✕'d, and a rating on something you watched elsewhere isn't any of those."""
    rated = set(taste.ratings())
    ranked = [r for r in ctx["ranked"] if r["key"] not in rated]
    rows = reason_rows()
    if rows:
        ranked = reason_rerank(ranked, lambda r: ctx["cands"].get(r["key"]) or {}, reason_weights(rows))
    ctx["ranked"] = ranked


def seen_filter(cards, q):
    """Seen-it from a card should stick before the next refresh too."""
    rated = taste.ratings()
    return [c for c in cards if c["key"] not in rated]


fx.REFRESH_HOOKS.append(refresh_hook)
fx.FILTERS.append(seen_filter)
fx.NAV.insert(next((i + 1 for i, (p, _) in enumerate(fx.NAV) if p == "/search"), 0), ("/swipe", "🔥 Swipe"))
fx.PAGE_TOP.insert(0, "features/swipe_top.html")
fx.CSS.append("/static/swipe.css?v=1")
fx.JS.append("/static/swipe.js?v=1")


# ---------------- deck ----------------
def _card(it, pick=None):
    why = (pick or {}).get("why") or ""
    if pick and not why and pick.get("because"):
        why = "Because you watched " + " and ".join(pick["because"][:2]) + "."
    return {"key": it["key"], "kind": it["kind"], "title": it.get("title", ""), "year": it.get("year") or "",
            "genres": (it.get("genres") or [])[:3], "poster": it.get("poster"), "rating": it.get("rating"),
            "lib": pick is None, "why": why if pick else "In your library — how was it?"}


def deck(kind="", skip=(), n=24):
    """Unrated picks (movies and shows interleaved, best first) with an unrated library title every LIB_EVERY cards."""
    fb, wants, rated = store.feedback(), store.wants(), taste.ratings()
    logged = {k: (a, ts) for k, a, ts in _db().execute("SELECT key, action, ts FROM swipe_log")}
    gone = set(skip) | set(fb) | set(wants) | set(rated)
    gone |= {k for k, (a, ts) in logged.items() if a != "skip" or time.time() - ts < SKIP_DAYS * 86400}
    lists = [store.picks(k) for k in ("movie", "tv") if kind in ("", k)]
    picks, seen = [], set()
    for i in range(max(map(len, lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i]["key"] not in gone and lst[i]["key"] not in seen:
                picks.append(lst[i])
                seen.add(lst[i]["key"])
    lib = [k for k in store.meta_get("library", []) if k not in gone and (not kind or k.startswith(kind + ":"))]
    lib = random.sample(lib, min(len(lib), n // LIB_EVERY + 1))
    items = store.items_get([p["key"] for p in picks[:n]] + lib)
    picks = [p for p in picks if p["key"] in items]
    lib = [k for k in lib if k in items]
    out = []
    while len(out) < n and (picks or lib):
        if lib and (len(out) % LIB_EVERY == LIB_EVERY - 1 or not picks):
            out.append(_card(items[lib.pop()]))
        else:
            p = picks.pop(0)
            out.append(_card(items[p["key"]], p))
    return out


def left_count(kind):
    """Unswiped picks of one kind, for the Quick-rate button on Movies/Shows."""
    fb, wants, rated = store.feedback(), store.wants(), taste.ratings()
    logged = {k for (k,) in _db().execute("SELECT key FROM swipe_log")}
    return sum(1 for p in store.picks(kind) if p["key"] not in fb and p["key"] not in wants
               and p["key"] not in rated and p["key"] not in logged)


def reason_summary():
    """Counts for the bottom of /swipe, plus the languages / genres behind those reasons."""
    try:
        from discover.features.browse import NAMES          # nicer language names; codes are fine without
    except Exception:
        NAMES = {}
    rows = reason_rows()
    counts = {r: sum(1 for x in rows if x[1] == r) for r in REASONS}
    tops = {}
    for r, col in (("lang", 2), ("genre", 3)):
        c = {}
        for x in rows:
            if x[1] == r and x[col]:
                name = NAMES.get(x[col], x[col]) if r == "lang" else x[col]
                c[name] = c.get(name, 0) + 1
        tops[r] = sorted(c.items(), key=lambda kv: -kv[1])[:4]
    return {"counts": counts, "tops": tops, "total": len(rows)}


# ---------------- routes ----------------
@fx.router.get("/swipe", response_class=HTMLResponse)
async def swipe_page(req: Request):
    from discover import app
    kind = req.query_params.get("kind", "")
    kind = kind if kind in ("movie", "tv") else ""
    return app.render_page(req, "swipe", [], template="features/swipe.html", deck=deck(kind), kind=kind,
                           today=today_count(), reasons=REASONS, summary=reason_summary())


@fx.router.get("/swipe/deck")
async def swipe_deck(kind: str = "", skip: str = ""):
    return {"cards": deck(kind if kind in ("movie", "tv") else "", [k for k in skip.split(",") if k][:300]),
            "today": today_count()}


@fx.router.post("/swipe/act/{action}/{key}")
async def swipe_act(action: str, key: str):
    if action not in ACTIONS or ":" not in key:
        return Response(status_code=404)
    apply(key, action)
    return {"ok": True, "today": today_count()}


@fx.router.post("/swipe/undo/{key}")
async def swipe_undo(key: str):
    return {"ok": True, "undone": undo(key), "today": today_count()}


@fx.router.post("/swipe/reason/{key}")
async def swipe_reason(key: str, r: str = ""):
    set_reason(key, r)
    return {"ok": True}
