"""Themed shelves: 4-6 Netflix-style rows on Movies/Shows ("Tamil thrillers", "Short & sweet", "Because you loved X"…)
built from the whole candidate pool (up to 300 per kind), not just the ~60 ranked picks — so they surface titles the
main grid never shows.

One batched Gemini call per kind proposes shelves from ~120 pool titles; it's cached and reused while the pool barely
changes (free tier). Rule-based shelves (genres/moods/keywords/language/decade/runtime) fill in or take over entirely
without Gemini. Rules everywhere: each shelf ≥ 6 titles, ≤ 12, no title on more than 2 shelves, nothing owned/wanted/
disliked, and a Tamil shelf whenever the pool has ≥ 5 Tamil titles (the house language).
"""
import json
import logging
import math
import re
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from discover import clients, config, store
from discover import features as fx

log = logging.getLogger("discover")

MIN_N, MAX_N, MAX_SHELVES, MAX_REPEAT = 6, 12, 6, 2
POOL_N = 120                       # titles sent to Gemini (~6k tokens)
REUSE_OVERLAP, REUSE_DAYS = 0.8, 7  # reuse Gemini's shelves while ≥ 80% of the pool is the same, for up to a week
SOUTH = ("te", "ml", "kn")         # pads a thin Tamil shelf — closest in taste, and honest in the title
LANG_NAMES = {"ta": "Tamil", "hi": "Hindi", "te": "Telugu", "ml": "Malayalam", "kn": "Kannada", "ko": "Korean",
              "ja": "Japanese", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian", "zh": "Chinese",
              "da": "Danish", "sv": "Swedish", "no": "Norwegian", "tr": "Turkish", "th": "Thai", "bn": "Bengali"}
PREF_GROUPS = ("ta", "hi", "te", "ml", "ko", "ja", "en")   # language-pref groups (Settings); the rest is "other"

THRILL = {"Thriller", "Crime", "Mystery"}
MIND_G = {"Science Fiction", "Sci-Fi & Fantasy", "Mystery"}
MIND_W = ("time travel", "twist", "psychological", "dream", "parallel", "puzzle", "simulation", "time loop", "mind")
LIGHT_G = {"Comedy", "Family", "Music", "Animation", "Kids"}
DARK_G = {"Crime", "Horror", "War", "War & Politics"}


# ---------------- storage (own table) ----------------
def _db():
    con = store.db()
    con.execute("CREATE TABLE IF NOT EXISTS shelves (kind TEXT, pos INTEGER, id TEXT, title TEXT, subtitle TEXT, "
                "items TEXT, src TEXT, PRIMARY KEY (kind, id))")
    return con


def save(kind, shelves):
    with _db() as c:
        c.execute("DELETE FROM shelves WHERE kind=?", (kind,))
        c.executemany("INSERT INTO shelves VALUES (?,?,?,?,?,?,?)",
                      [(kind, i, s["id"], s["title"], s["subtitle"], json.dumps(s["items"]), s.get("src", "rules"))
                       for i, s in enumerate(shelves)])


def load(kind):
    rows = _db().execute("SELECT id, title, subtitle, items, src FROM shelves WHERE kind=? ORDER BY pos", (kind,))
    return [{"id": i, "title": t, "subtitle": s, "items": json.loads(it), "src": src} for i, t, s, it, src in rows]


# ---------------- pure helpers (tested) ----------------
def slug(title, taken=()):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "shelf"
    out, n = s, 2
    while out in taken:
        out, n = f"{s}-{n}", n + 1
    return out


def norm_id(raw, kind, pool):
    """Lighter models drop the "movie:" prefix (or invent "tv:" for movies) → match on the tmdb number."""
    k = str(raw).strip()
    return k if k in pool else f"{kind}:{k.rsplit(':', 1)[-1]}"


def _extra_score(c, support):
    """Order for pool titles past the ranked head: support + Bayesian-ish quality (no engine score for them)."""
    return math.log1p(support.get(c["key"], 0)) + 0.15 * (c.get("rating") or 0) * min(1, (c.get("votes") or 0) / 500)


def pool_of(ctx):
    """Ranked picks first (their order already blends engine + Gemini), then the rest of the candidates."""
    cands, blocked = ctx["cands"], set(ctx.get("exclude") or ()) | set(ctx.get("owned") or ())
    blocked |= set(store.wants()) | {k for k, s in store.feedback().items() if s == "dislike"}
    prefs = store.pref("languages") or {}
    off = {g for g, v in prefs.items() if v == "off"}

    def ok(k):
        c = cands.get(k)
        lang = (c or {}).get("lang")
        return c and k not in blocked and (lang if lang in PREF_GROUPS else "other") not in off
    head = [r["key"] for r in ctx["ranked"] if ok(r["key"])]
    seen = set(head)
    rest = sorted((k for k in cands if k not in seen and ok(k)), key=lambda k: -_extra_score(cands[k], ctx["support"]))
    return [cands[k] for k in head + rest]


def moods_map(ranked):
    return {r["key"]: (r.get("extra") or {}).get("moods") or [] for r in ranked}


def _kw(c):
    return " | ".join(c.get("keywords") or []).lower()


def rule_shelves(pool, kind, moods, because, loved):
    """Every rule shelf that could apply, best-fit titles first (pool order). finalize() decides which survive."""
    is_ = lambda c, m: m in moods.get(c["key"], ())                             # noqa: E731
    g = lambda c: set(c.get("genres") or [])                                    # noqa: E731
    what = "movies" if kind == "movie" else "series"
    out = []

    def add(title, sub, keys, sid=None):
        out.append({"title": title, "subtitle": sub, "keys": keys, "sid": sid})

    ta = [c for c in pool if c.get("lang") == "ta"]
    ta_thr = [c for c in ta if g(c) & THRILL or is_(c, "thrilling") or is_(c, "dark")]
    twist = [c["key"] for c in ta_thr if "twist" in _kw(c)]
    if len(twist) >= MIN_N:     # only claim a twist when TMDB's keywords say so
        add("Tamil thrillers with a twist", "Crime and suspense that pull the rug out", twist, "tamil-twist")
    elif len(ta_thr) >= MIN_N:
        ta_thr = [c["key"] for c in ta_thr]
        add("Tamil thrillers", f"Crime, mystery and suspense — Tamil {what} you haven't seen", ta_thr, "tamil-thrillers")
    if len(ta) >= MIN_N:
        add(f"Tamil {what} for you", "Picked from what you watch", [c["key"] for c in ta], "tamil")
    elif len(ta) >= 5:          # the promise: a Tamil shelf from 5 titles — pad with the nearest neighbours, say so
        south = [c["key"] for c in pool if c.get("lang") in SOUTH]
        add("Tamil & South Indian picks", "Tamil first, then Telugu, Malayalam and Kannada",
            [c["key"] for c in ta] + south[:len(ta)], "tamil")     # Tamil stays the majority
    for seed in loved[:4]:
        keys = [c["key"] for c in pool if seed in (because.get(c["key"]) or ())]
        add(f"Because you loved {seed}", "Same pull, different story", keys, "because-" + slug(seed))
    if kind == "movie":
        add("Short & sweet: under 100 min", "For when you've got an evening, not a weekend",
            [c["key"] for c in pool if c.get("runtime") and c["runtime"] < 100], "short")
    else:
        add("Binge in a weekend", "One or two seasons, done by Sunday",
            [c["key"] for c in pool if c.get("seasons") and c["seasons"] <= 2], "binge")
    add("Mind-bending " + ("sci-fi & mysteries" if kind == "movie" else "series"), "Twists, loops and puzzles",
        [c["key"] for c in pool if is_(c, "mind") or (g(c) & MIND_G and any(w in _kw(c) for w in MIND_W))], "mind")
    add("Dark & gripping", "Crime, dread and high stakes",
        [c["key"] for c in pool if is_(c, "dark") or g(c) & DARK_G], "dark")
    add("Light & feel-good", "Easy, warm, funny",
        [c["key"] for c in pool if is_(c, "light") or is_(c, "feelgood") or (g(c) & LIGHT_G and not g(c) & DARK_G)],
        "light")
    gems = [c for c in pool if (c.get("votes") or 0) < 2000 and (c.get("rating") or 0) >= 7]
    decades = {}
    for c in gems:
        y = int(c["year"]) if str(c.get("year") or "").isdigit() else 0
        if 1960 <= y < 2010:
            decades.setdefault(y // 10 * 10, []).append(c["key"])
    d, keys = max(decades.items(), key=lambda x: len(x[1]), default=(0, []))
    if len(keys) >= MIN_N:      # one gems shelf only: an era is the more interesting theme when there are enough
        add(f"Hidden {str(d)[2:]}s gems", "Well rated, little known, from before the streaming era", keys, f"gems-{d}")
    else:
        add("Hidden gems", "Great ratings, few votes — your taste", [c["key"] for c in gems], "gems")
    langs = {}
    for c in pool:
        if c.get("lang") not in ("en", "ta", None):
            langs.setdefault(c["lang"], []).append(c["key"])
    for lang, keys in sorted(langs.items(), key=lambda x: -len(x[1]))[:1]:
        if lang in LANG_NAMES:
            add(f"{LANG_NAMES[lang]} {what} worth a look", "Subtitles on, you'll forget about them", keys, f"lang-{lang}")
    add("Highly rated, still unseen", "Big audiences, strong scores",
        [c["key"] for c in pool if (c.get("rating") or 0) >= 7.8 and (c.get("votes") or 0) >= 5000], "acclaimed")
    return out


def is_tamil_shelf(keys, items):
    return keys and sum((items.get(k) or {}).get("lang") == "ta" for k in keys) * 2 >= len(keys)


def finalize(proposed, items, want_tamil=False, max_shelves=MAX_SHELVES):
    """Proposed shelves (best first) → kept shelves. Titles not yet shown elsewhere are preferred, so a shelf of
    12 thrillers doesn't repeat the 12 from the shelf above; within a shelf the proposed order is kept. A shelf that
    would mostly repeat one already kept (Gemini's "Quick watches" vs the rule "Short & sweet") is skipped."""
    uses, out, taken = {}, [], set()
    order = list(proposed)
    first_ta = next((i for i, s in enumerate(order) if is_tamil_shelf(s["keys"], items)), None)
    if want_tamil and first_ta:      # the Tamil shelf goes first so it gets first claim on Tamil titles
        order.insert(0, order.pop(first_ta))
    for s in order:
        if len(out) >= max_shelves:
            break
        keys = list(dict.fromkeys(k for k in s["keys"] if k in items and uses.get(k, 0) < MAX_REPEAT))
        fresh = [k for k in keys if not uses.get(k)]
        chosen = set((fresh + [k for k in keys if uses.get(k)])[:MAX_N])
        keys = [k for k in keys if k in chosen]
        if len(keys) < MIN_N or s.get("sid") in taken or any(_overlap(keys, o["keys"]) >= 0.6 for o in out):
            continue
        sid = slug(s.get("sid") or s["title"], taken)
        taken.add(sid)
        for k in keys:
            uses[k] = uses.get(k, 0) + 1
        out.append({"id": sid, "title": s["title"][:70], "subtitle": (s.get("subtitle") or "")[:110],
                    "keys": keys, "src": s.get("src", "rules")})
    return out


def parse_gemini(out, kind, items):
    """Gemini JSON → proposed shelves; anything malformed or outside the pool is dropped, never fatal."""
    rows = out.get("shelves") if isinstance(out, dict) else out if isinstance(out, list) else []
    res = []
    for s in rows or []:
        if not isinstance(s, dict) or not str(s.get("title") or "").strip() or not isinstance(s.get("ids"), list):
            continue
        keys = [norm_id(i, kind, items) for i in s["ids"] if isinstance(i, (str, int))]
        res.append({"title": str(s["title"]).strip(), "subtitle": str(s.get("subtitle") or "").strip(),
                    "keys": [k for k in keys if k in items], "src": "gemini"})
    return res


def _line(c, moods):
    size = f"{c['runtime']}m" if c.get("kind") == "movie" and c.get("runtime") else \
        f"{c['seasons']}s" if c.get("seasons") else "?"
    return (f"{c['key']} | {c['title']} ({c.get('year', '')}) | {c.get('lang') or '?'} | "
            f"{', '.join((c.get('genres') or [])[:3])} | {', '.join(moods.get(c['key'], [])[:3]) or '-'} | {size} | "
            f"{(c.get('overview') or '').split('. ')[0][:110]}")


def prompt(kind, pool, moods, loved, tamil):
    what = "movies" if kind == "movie" else "TV series"
    lines = "\n".join(_line(c, moods) for c in pool)
    ta = ("\n- At least ONE shelf must be mostly Tamil-language (lang ta) titles — this viewer is Tamil."
          if tamil else "")
    return f"""You curate themed shelves of {what} for one viewer, like rows on a streaming home screen.

They loved: {", ".join(loved[:15]) or "(unknown)"}

Pool (id | title (year) | lang | genres | moods | runtime m or seasons s | plot):
{lines}

Make 5 shelves. Rules:
- Each shelf: 8-12 ids, ONLY from the pool; a title may appear on at most 2 shelves.
- Themes should be specific and varied (language + genre, mood, length, era, "Because you loved <one of their
  titles>"), not five genre buckets.
- title: catchy but honest, max 6 words; subtitle: one plain line, max 12 words. No hype ("masterpiece",
  "must-watch", "epic"), no spoilers.{ta}
Return JSON: {{"shelves": [{{"title": "...", "subtitle": "...", "ids": ["<id>", ...]}}]}}"""


def _overlap(a, b):
    return len(set(a) & set(b)) / (min(len(set(a)), len(set(b))) or 1)


# ---------------- refresh hook ----------------
async def shelves_hook(ctx):
    kind = ctx["kind"]
    pool = pool_of(ctx)
    items = {c["key"]: c for c in pool}
    moods, because = moods_map(ctx["ranked"]), ctx.get("because") or {}
    loved = list(dict.fromkeys(s["title"] for s, w in ctx.get("pos") or [] if w > 0 and s.get("title")))
    tamil = sum(c.get("lang") == "ta" for c in pool) >= 5
    send = pool[:POOL_N]
    if tamil:   # make sure Gemini actually sees the Tamil titles, even if they rank low
        extra = [c for c in pool[POOL_N:] if c.get("lang") == "ta"][:20]
        send = send[:POOL_N - len(extra)] + extra

    gem, cache_k = [], f"shelves:gem:{kind}"
    if config.GEMINI_KEY and send:
        cache = store.meta_get(cache_k) or {}
        keys = [c["key"] for c in send]
        if cache.get("shelves") and time.time() - cache.get("ts", 0) < REUSE_DAYS * 86400 \
                and _overlap(cache.get("pool", []), keys) >= REUSE_OVERLAP:
            gem = cache["shelves"]                       # pool barely changed → keep the call for tomorrow
        else:
            try:
                out = await clients.gemini_json(ctx["http"], prompt(kind, send, moods, loved, tamil))
                gem = parse_gemini(out, kind, {c["key"]: c for c in send})
                store.meta_put(cache_k, {"ts": time.time(), "pool": keys, "shelves": gem})
            except Exception as ex:
                log.warning("gemini shelves (%s) failed: %s", kind, ex)
    rules = rule_shelves(pool, kind, moods, because, loved)
    kept = finalize(gem, items, want_tamil=tamil)        # cached shelves are re-checked against today's exclusions
    if len(kept) < 5 or (tamil and not any(is_tamil_shelf(s["keys"], items) for s in kept)):
        kept = finalize(gem + rules, items, want_tamil=tamil)   # Gemini short/absent/no Tamil → rules fill in
    save(kind, [{**s, "items": [{"key": k, "because": (because.get(k) or [])[:2]} for k in s["keys"]]}
                for s in kept])
    log.info("shelves %s: %d (%s)", kind, len(kept), ",".join(s["src"][0] for s in kept))


fx.REFRESH_HOOKS.append(shelves_hook)
fx.PAGE_TOP.append("features/shelves_top.html")
fx.CSS.append("/static/shelves.css?v=1")


# ---------------- view ----------------
KINDS = {"movies": "movie", "shows": "tv"}


def _live(keys):
    """Drop what you wanted or ✕'d since the refresh, so rows never offer it again."""
    wants, fb = store.wants(), store.feedback()
    return [k for k in keys if k not in wants and fb.get(k) != "dislike"]


def view(tab):
    kind = KINDS.get(tab)
    if not kind:
        return []
    shelves = load(kind)
    items = store.items_get({i["key"] for s in shelves for i in s["items"]})
    out = []
    for s in shelves:
        tiles = [items[k] for k in _live([i["key"] for i in s["items"]]) if k in items]
        if len(tiles) >= MIN_N - 2:         # a couple of ✕s shouldn't make a shelf vanish mid-day
            out.append({**s, "kind": kind, "tiles": tiles})
    return out


@fx.router.get("/shelf/{kind}/{sid}", response_class=HTMLResponse)
async def shelf_page(req: Request, kind: str, sid: str):
    from discover.app import cards_for, render_page
    s = next((s for s in load(kind) if s["id"] == sid), None) if kind in ("movie", "tv") else None
    if not s:
        return Response(status_code=404)
    ranked = {p["key"]: p for p in store.picks(kind)}         # ranked picks carry Gemini's "why"
    live = set(_live([i["key"] for i in s["items"]]))
    picks = [ranked.get(i["key"]) or {"key": i["key"], "score": 0, "why": "", "because": i.get("because") or [],
                                      "extra": {}} for i in s["items"] if i["key"] in live]
    return render_page(req, "shelf", cards_for(picks), template="features/shelf.html", shelf={**s, "kind": kind},
                       back="/movies" if kind == "movie" else "/shows")
