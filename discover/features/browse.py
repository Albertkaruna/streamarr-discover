"""Browse: filter chips on Movies/Shows (genre · language · mood · moment · hidden gems), a "Hidden gems" strip,
and a Settings page for how much of each language you want.

Filters are plain links (?genre=Drama&genre=Comedy&lang=ta&mood=dark&pick=short). Within one group values are OR'd
— picking Drama + Comedy means "either is fine tonight"; AND would leave almost nothing out of ~60 picks and TMDB's
genre tags are sparse. Across groups it's AND (Tamil AND Thriller), and the moment chips (pick=…) all must hold.
"""
import logging
import urllib.parse

from fastapi import Request
from fastapi.responses import RedirectResponse

from discover import clients, config, store
from discover import features as fx

log = logging.getLogger("discover")

# Tamil first: it's the house language. Everything not listed is "other".
LANGS = {"ta": "Tamil", "hi": "Hindi", "te": "Telugu", "ml": "Malayalam", "ko": "Korean", "ja": "Japanese",
         "en": "English", "other": "Other languages"}
LEVELS = ("off", "some", "lots")
NAMES = {**LANGS, "kn": "Kannada", "bn": "Bengali", "mr": "Marathi", "fr": "French", "es": "Spanish", "de": "German",
         "it": "Italian", "zh": "Chinese", "cn": "Cantonese", "th": "Thai", "tr": "Turkish", "da": "Danish",
         "sv": "Swedish", "no": "Norwegian", "pt": "Portuguese", "ru": "Russian", "pl": "Polish", "nl": "Dutch",
         "fa": "Persian", "ar": "Arabic", "id": "Indonesian", "tl": "Tagalog", "he": "Hebrew", "is": "Icelandic"}
LOTS_BOOST = 0.25          # added to a pick's position value (1 → 0) for "Lots": #30 of 60 → ~#15
CAP_TOP, CAP_SHARE = 30, 0.6

MOODS = {"light": "😂 Light & funny", "mind": "🌀 Mind-bending", "dark": "🌑 Dark & intense", "feelgood": "☀️ Feel-good",
         "romantic": "💕 Romantic", "thrilling": "⚡ Thrilling", "slowburn": "🕯️ Slow-burn", "family": "👪 Family"}
PICKS = {"short": "⏱ Short", "binge": "📺 Binge-able", "new": "🧭 New to me", "gems": "💎 Hidden gems"}
GROUPS = ("genre", "lang", "mood", "pick")

# Fallback mood tags (no Gemini, or titles past the top 80): TMDB genres + keyword hints.
MOOD_GENRES = {"light": {"Comedy"}, "mind": {"Science Fiction", "Mystery", "Sci-Fi & Fantasy"},
               "dark": {"Horror", "Crime", "War", "War & Politics"}, "feelgood": {"Family", "Music", "Kids"},
               "romantic": {"Romance"}, "thrilling": {"Thriller", "Action", "Action & Adventure"},
               "slowburn": {"History"}, "family": {"Family", "Kids", "Animation"}}
MOOD_WORDS = {"light": ("parody", "satire", "spoof", "slapstick", "sitcom"),
              "mind": ("time travel", "twist", "psychological", "dream", "parallel", "puzzle", "simulation", "loop"),
              "dark": ("serial killer", "revenge", "murder", "violence", "gangster", "dystopia", "abuse"),
              "feelgood": ("friendship", "feel-good", "underdog", "coming of age", "heartwarming", "wholesome"),
              "romantic": ("love", "romance", "romantic"), "thrilling": ("heist", "chase", "suspense", "hostage"),
              "slowburn": ("slow burn", "character study", "melancholy", "loneliness", "grief"), "family": ("family",)}


# ---------------- pure helpers (tested) ----------------
def group(lang):
    return lang if lang in LANGS else "other"


def lang_prefs():
    p = store.pref("languages") or {}
    return {g: p.get(g) if p.get(g) in LEVELS else "some" for g in LANGS}


def lang_rerank(rows, lang_of, prefs, top=CAP_TOP, share=CAP_SHARE):
    """Drop "Off" languages, lift "Lots", and keep one language from taking > share of the top N unless it's the
    only "Lots" one. Works on positions, not scores, so Gemini's reorder survives."""
    lvl = {id(r): prefs.get(group(lang_of(r)), "some") for r in rows}
    keep = [r for r in rows if lvl[id(r)] != "off"] or rows      # everything off → ignore, beats an empty page
    n = len(keep) or 1
    val = {id(r): 1 - i / n + (LOTS_BOOST if lvl[id(r)] == "lots" else 0) for i, r in enumerate(keep)}
    pool = sorted(keep, key=lambda r: -val[id(r)])
    lots, limit, out, seen = {g for g, v in prefs.items() if v == "lots"}, int(share * top), [], {}
    while pool:
        pick = 0
        if len(out) < top:
            for i, r in enumerate(pool):
                lang = lang_of(r) or "?"
                if seen.get(lang, 0) < limit or lots == {group(lang)}:
                    pick = i
                    break
        r = pool.pop(pick)
        seen[lang_of(r) or "?"] = seen.get(lang_of(r) or "?", 0) + 1
        out.append(r)
    return out


def heuristic_moods(it):
    g, kw = set(it.get("genres") or []), " | ".join(it.get("keywords") or []).lower()
    out = [m for m in MOODS if g & MOOD_GENRES[m] or any(w in kw for w in MOOD_WORDS[m])]
    if "Drama" in g and not g & {"Action", "Thriller", "Comedy", "Action & Adventure"} and "slowburn" not in out:
        out.append("slowburn")                    # pure drama tends to take its time
    if "Comedy" in g and not g & {"Horror", "Crime", "Thriller"} and "feelgood" not in out:
        out.append("feelgood")
    return out


def moods_of(c):
    return (c.get("extra") or {}).get("moods") or heuristic_moods(c)


def is_gem(c):
    return (c.get("votes") or 0) < 2000 and (c.get("rating") or 0) >= 7


def usual(seeds):
    """What you normally watch: languages/genres with a real share of your positive seed weight."""
    lw, gw, tot = {}, {}, sum(w for _, w in seeds) or 1
    for it, w in seeds:
        lw[it.get("lang")] = lw.get(it.get("lang"), 0) + w
        for g in it.get("genres") or []:
            gw[g] = gw.get(g, 0) + w
    return {"langs": sorted(k for k, v in lw.items() if k and v / tot >= 0.08),
            "genres": sorted(k for k, v in gw.items() if v / tot >= 0.06)}


def is_new(c, u):
    if not u or not (u["langs"] or u["genres"]):
        return False
    return c.get("lang") not in u["langs"] or not set((c.get("genres") or [])[:2]) & set(u["genres"])


def match(c, sel, u=None):
    """sel: {group: [values]} — OR inside a group, AND across groups."""
    if sel.get("genre") and not set(sel["genre"]) & set(c.get("genres") or []):
        return False
    if sel.get("lang") and c.get("lang") not in sel["lang"]:
        return False
    if sel.get("mood") and not set(sel["mood"]) & set(moods_of(c)):
        return False
    for p in sel.get("pick") or []:
        if p == "short" and not (c.get("kind") == "movie" and c.get("runtime") and c["runtime"] < 100):
            return False
        if p == "binge" and not (c.get("kind") == "tv" and c.get("seasons") and c["seasons"] <= 3):
            return False
        if p == "gems" and not is_gem(c):
            return False
        if p == "new" and not is_new(c, u):
            return False
    return True


def selection(q):
    return {g: [v for v in q.getlist(g) if v] for g in GROUPS} if hasattr(q, "getlist") else \
        {g: list(q.get(g) or []) for g in GROUPS}


def href(q, grp=None, val=None):
    """Link that toggles one value (or clears a group with val=None, or everything with grp=None), keeping the rest."""
    pairs = list(q.multi_items()) if hasattr(q, "multi_items") else [(k, v) for k, vs in q.items() for v in vs]
    if grp is None:
        pairs = [(k, v) for k, v in pairs if k not in GROUPS]
    elif val is None:
        pairs = [(k, v) for k, v in pairs if k != grp]
    elif (grp, val) in pairs:
        pairs.remove((grp, val))
    else:
        pairs.append((grp, val))
    return "?" + urllib.parse.urlencode(pairs)


# ---------------- view time ----------------
def _prefs_view(cards):
    """Language prefs at view time: Off disappears right away; if prefs changed since the last refresh, redo the
    re-rank on the saved order so Lots shows up without waiting a day."""
    if not cards:
        return cards
    prefs = lang_prefs()
    if store.meta_get(f"browse:langprefs:{cards[0]['kind']}") == prefs:
        return [c for c in cards if prefs[group(c.get("lang"))] != "off"] or cards
    return lang_rerank(cards, lambda c: c.get("lang"), prefs)


def browse_filter(cards, q):
    cards = _prefs_view(cards)
    sel = selection(q)
    if not any(sel.values()):
        return cards
    u = store.meta_get(f"browse:usual:{cards[0]['kind']}") if cards else None
    return [c for c in cards if match(c, sel, u)]


def ui(tab, q, cards):
    """Chip rows + gems strip for page_top. Counts are faceted: each group counts against the other groups' filters."""
    from discover.app import KINDS, cards_for
    kind = KINDS.get(tab)
    if not kind:
        return None
    base = _prefs_view(cards_for(store.picks(kind)))
    sel, u = selection(q), store.meta_get(f"browse:usual:{kind}")

    def chips(grp, counts, labels=None, order=None):
        keys = order or sorted(counts, key=lambda k: (-counts[k], k))
        return [{"label": (labels or {}).get(k, k), "n": counts.get(k, 0), "on": k in sel[grp], "href": href(q, grp, k)}
                for k in keys if counts.get(k) or k in sel[grp]]

    def facet(grp):
        others = {g: v for g, v in sel.items() if g != grp}
        return [c for c in base if match(c, others, u)]

    gc, lc, mc = {}, {}, {}
    for c in facet("genre"):
        for g in c.get("genres") or []:
            gc[g] = gc.get(g, 0) + 1
    for c in facet("lang"):
        lc[c.get("lang") or "?"] = lc.get(c.get("lang") or "?", 0) + 1
    for c in facet("mood"):
        for m in moods_of(c):
            mc[m] = mc.get(m, 0) + 1
    pick_pool = facet("pick")
    pc = {p: sum(match(c, {"pick": [*sel["pick"], p]}, u) for c in pick_pool)
          for p in PICKS if not (p == "short" and kind == "tv") and not (p == "binge" and kind == "movie")}
    gems = [] if "gems" in sel["pick"] else sorted([c for c in cards if is_gem(c)], key=lambda c: -c["score"])[:12]
    return {"active": any(sel.values()), "all_href": href(q), "total": len(base),
            "moments": chips("pick", pc, PICKS, list(pc)), "moods": chips("mood", mc, MOODS, list(MOODS)),
            "langs": chips("lang", lc, NAMES), "genres": chips("genre", gc), "gems": gems}


# ---------------- refresh hooks ----------------
async def lang_hook(ctx):
    prefs, items = lang_prefs(), ctx["cands"]
    ctx["ranked"] = lang_rerank(ctx["ranked"], lambda r: (items.get(r["key"]) or {}).get("lang"), prefs)
    store.meta_put(f"browse:langprefs:{ctx['kind']}", prefs)
    store.meta_put(f"browse:usual:{ctx['kind']}", usual([(s, w) for s, w in ctx["pos"] if w > 0]))


async def mood_hook(ctx):
    """One batched Gemini call per kind tags the top 80 with moods (cached per title, so later refreshes only ask
    about new titles). Anything Gemini didn't tag falls back to the genre/keyword heuristic."""
    items, head = ctx["cands"], ctx["ranked"][:80]
    cache = store.meta_get("browse:moods", {})
    todo = [r["key"] for r in head if r["key"] not in cache and r["key"] in items]
    if todo and config.GEMINI_KEY:
        lines = "\n".join(f"{k} | {items[k]['title']} ({items[k].get('year', '')}) | "
                          f"{', '.join(items[k].get('genres', [])[:3])} | {items[k].get('overview', '')[:160]}"
                          for k in todo)
        prompt = f"""Tag each title with 1-3 moods that describe how it FEELS to watch, from exactly this list:
{", ".join(MOODS)}
(light = light & funny, mind = mind-bending, dark = dark & intense, feelgood = feel-good, slowburn = slow-burn,
family = fine for a family watch)

Titles (id | title (year) | genres | plot):
{lines}

Return JSON: {{"<id>": ["<mood>", ...]}} with every id."""
        try:
            out = await clients.gemini_json(ctx["http"], prompt)
            for k, ms in (out if isinstance(out, dict) else {}).items():
                if k in items and isinstance(ms, list):
                    cache[k] = [m for m in ms if m in MOODS][:3]
            cache = dict(list(cache.items())[-3000:])          # keep the cache bounded
            store.meta_put("browse:moods", cache)
        except Exception as ex:
            log.warning("gemini moods (%s) failed: %s", ctx["kind"], ex)
    for r in ctx["ranked"]:
        it = items.get(r["key"]) or {}
        r.setdefault("extra", {})["moods"] = cache.get(r["key"]) or heuristic_moods(it)


fx.REFRESH_HOOKS.append(lang_hook)       # first: drops "Off" so the mood call isn't spent on them
fx.REFRESH_HOOKS.append(mood_hook)
fx.FILTERS.append(browse_filter)
fx.PAGE_TOP.append("features/browse_top.html")
fx.NAV.append(("/settings", "Settings"))
fx.CSS.append("/static/browse.css")
fx.JS.append("/static/browse.js")


# ---------------- settings page ----------------
def _render(req, tpl_name, **extra):
    from discover import app
    return app.tpl.TemplateResponse(req, tpl_name, {"tab": "settings", "cards": [], "status": app._status(),
                                                    "q": req.query_params, "wanted_n": len(store.wants()), **extra})


@fx.router.get("/settings")
async def settings_page(req: Request):
    return _render(req, "features/settings.html", langs=LANGS, prefs=lang_prefs(), levels=LEVELS,
                   saved=req.query_params.get("saved"))


@fx.router.post("/settings")
async def settings_save(req: Request):
    f = urllib.parse.parse_qs((await req.body()).decode())
    store.set_pref("languages", {g: f.get(g, ["some"])[0] if f.get(g, ["some"])[0] in LEVELS else "some"
                                 for g in LANGS})
    return RedirectResponse("/settings?saved=1", status_code=303)
