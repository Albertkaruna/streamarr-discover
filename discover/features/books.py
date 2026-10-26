"""Books tab (#12): book picks from what you watch (and from your Hardcover shelves when HARDCOVER_TOKEN is set).

Sources, merged and scored like the movie engine does it (support × quality, max 2 per author):
  - subjects  — your top seeds' genres + recurring TMDB keywords → Open Library subjects → popular books there
  - based on  — the novels behind films/shows you loved (TMDB keyword "based on novel or book")
  - Gemini    — one call: "given these loved films/shows (and books), suggest 15 books", resolved on Open Library
  - Hardcover — your Read/Want shelves → their subjects + authors' other books (optional)
"Want" is stored here until Chaptarr exists; then wants() is what gets sent."""
import asyncio
import json
import logging
import math
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from discover import features as fx
from discover import store
from discover.features import books_sources as S

log = logging.getLogger("discover")
MAX_PICKS, PER_AUTHOR = 60, 2

# TMDB genre → Open Library subject keys. Leaves out what doesn't translate to books (Animation, TV Movie, Reality).
GENRE_SUBJECTS = {
    "Science Fiction": ["science_fiction"], "Sci-Fi & Fantasy": ["science_fiction", "fantasy"],
    "Fantasy": ["fantasy"], "Mystery": ["mystery_and_detective_stories"], "Crime": ["crime"],
    "Thriller": ["thrillers", "suspense"], "Horror": ["horror"], "Romance": ["love_stories"],
    "Comedy": ["humorous_fiction"], "Drama": ["psychological_fiction"], "History": ["historical_fiction"],
    "War": ["war_stories"], "War & Politics": ["political_fiction", "war_stories"], "Western": ["western_stories"],
    "Adventure": ["adventure_stories"], "Action & Adventure": ["adventure_stories", "thrillers"],
    "Action": ["thrillers"], "Family": ["juvenile_fiction"], "Kids": ["juvenile_fiction"],
    "Documentary": ["history"], "Music": ["musicians"],
}
# TMDB keyword → OL subject where plain slugging misses; everything else passes through as-is.
KEYWORD_SUBJECTS = {
    "dystopia": "dystopias", "serial killer": "serial_murderers", "post-apocalyptic future": "end_of_the_world",
    "artificial intelligence (a.i.)": "artificial_intelligence", "time loop": "time_travel",
    "spy": "spy_stories", "espionage": "spy_stories", "detective": "detectives", "heist": "robbery",
    "coming of age": "coming_of_age", "superhero": "superheroes", "zombie": "zombies", "vampire": "vampires",
    "alien": "extraterrestrial_beings", "space travel": "space_flight", "murder": "murder",
    "parallel world": "parallel_universes", "alternate history": "alternative_histories",
}
# keywords that describe production, not story
STOP = {"duringcreditsstinger", "aftercreditsstinger", "woman director", "sequel", "remake", "anime",
        "based on comic", "biography", "based on true story", "miniseries", "live action remake", "3d",
        "female protagonist", "male protagonist", "friends", "family", "love", "violence",
        "new york city", "los angeles, california", "london, england", "lgbt", "sports", "independent film"}
# subjects too generic to mean anything (mostly from book metadata via Hardcover)
GENERIC = {"fiction", "general", "fiction_general", "accessible_book", "protected_daisy", "in_library",
           "large_type_books", "english_fiction", "american_fiction", "novel", "novels", "literature",
           "open_library_staff_picks", "lending_library", "long_now_manual_for_civilization", "reading_level-grade_11",
           "reading_level-grade_12", "american_literature", "english_literature", "fiction_literary"}


# ---------------- storage (own tables) ----------------
_ready = set()


def _db():
    con = store.db()
    if id(con) not in _ready:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS book_picks (pos INTEGER PRIMARY KEY, id TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS book_fb (id TEXT PRIMARY KEY, signal TEXT, data TEXT, ts REAL); -- want | dislike
        """)
        _ready.add(id(con))
    return con


def save_picks(books):
    with _db() as c:
        c.execute("DELETE FROM book_picks")
        c.executemany("INSERT INTO book_picks VALUES (?,?,?)", [(i, b["id"], json.dumps(b)) for i, b in enumerate(books)])


def picks():
    return [json.loads(d) for (d,) in _db().execute("SELECT data FROM book_picks ORDER BY pos")]


def feedback():
    return dict(_db().execute("SELECT id, signal FROM book_fb"))


def set_feedback(bid, signal, data=None):
    with _db() as c:
        if signal:
            c.execute("REPLACE INTO book_fb VALUES (?,?,?,?)", (bid, signal, json.dumps(data or {}), time.time()))
        else:
            c.execute("DELETE FROM book_fb WHERE id=?", (bid,))


def wants():
    """Wanted books, newest first: [{id, title, author, year, cover}] — the hand-off list for Chaptarr later."""
    rows = _db().execute("SELECT id, data FROM book_fb WHERE signal='want' ORDER BY ts DESC").fetchall()
    return [{**json.loads(d), "id": i} for i, d in rows]


# ---------------- taste → subjects ----------------
def slug(s):
    return S.norm(s).replace(" ", "_")


def label(subject):
    return subject.replace("_", " ").capitalize()


def taste_subjects(seeds, max_genre=5, max_kw=8):
    """[(item, w)] → {subject: {"w": 0..1, "from": [titles]}}. Keywords only count when ≥2 seeds share them —
    one film's quirky keyword isn't a taste. Genres are broad, so they weigh less than shared keywords."""
    gen, kw = {}, {}
    for it, w in seeds:
        for g in it.get("genres") or []:
            for s in GENRE_SUBJECTS.get(g, []):
                e = gen.setdefault(s, {"w": 0, "from": [], "n": 0})
                e["w"] += w
                e["n"] += 1
                if len(e["from"]) < 3:
                    e["from"].append(it["title"])
        for k in it.get("keywords") or []:
            k = k.lower()
            if k in STOP or k.startswith("based on") or "," in k:     # "paris, france": places aren't tastes
                continue
            s = KEYWORD_SUBJECTS.get(k) or slug(k)
            e = kw.setdefault(s, {"w": 0, "from": [], "n": 0})
            if it["title"] not in e["from"]:
                e["w"] += w
                e["n"] += 1
                if len(e["from"]) < 3:
                    e["from"].append(it["title"])
    out = {}
    for pool, n, mult, min_n in ((gen, max_genre, 0.7, 1), (kw, max_kw, 1.0, 2)):
        top = sorted(((s, e) for s, e in pool.items() if e["n"] >= min_n and s), key=lambda x: -x[1]["w"])[:n]
        mx = max((e["w"] for _, e in top), default=1) or 1
        for s, e in top:
            if s not in out or out[s]["w"] < mult * e["w"] / mx:
                out[s] = {"w": round(mult * e["w"] / mx, 3), "from": e["from"]}
    return out


def shelf_subjects(found, max_n=8):
    """Hardcover seed books (resolved on OL, with w) → {subject: {"w", "from"}} (subjects shared by ≥2 books,
    unless the shelf is tiny)."""
    agg = {}
    for b, w in found:
        for s in b.get("subjects") or []:
            if s in GENERIC or ":" in s or len(s) > 40 or "fiction_general" in s:
                continue
            e = agg.setdefault(s, {"w": 0, "from": [], "n": 0})
            e["w"] += w
            e["n"] += 1
            if len(e["from"]) < 3:
                e["from"].append(b["title"])
    min_n = 2 if len(found) >= 4 else 1
    top = sorted(((s, e) for s, e in agg.items() if e["n"] >= min_n), key=lambda x: -x[1]["w"])[:max_n]
    mx = max((e["w"] for _, e in top), default=1) or 1
    return {s: {"w": round(e["w"] / mx, 3), "from": e["from"]} for s, e in top}


# ---------------- merge + score ----------------
def merge(entries, skip_ids=(), skip_titles=()):
    """entries: [(book, support, why, tag)] from all sources → ranked unique books.
    Same work via several sources adds support; the most specific reason wins (Gemini/based-on > subjects).
    Final = support × (0.6 + 0.4·popularity), so a well-read book edges out an obscure one at equal fit."""
    skip_ids, skip_titles = set(skip_ids), set(skip_titles)
    by = {}
    for b, sup, why, tag in entries:
        if not b or not b.get("title") or b["id"] in skip_ids:
            continue
        tkey = (S.short_title(b["title"]), (S.norm(b.get("author")).split() or [""])[-1])   # dedupe editions
        if tkey[0] in skip_titles:
            continue
        cur = by.get(tkey)
        if not cur:
            by[tkey] = {**b, "support": sup, "why": why, "tag": tag, "_best": sup}
            continue
        cur["support"] += sup
        if sup > cur["_best"]:
            cur.update(why=why, tag=tag, _best=sup)
    out = []
    for b in by.values():
        pop = min(1.0, math.log1p(b.get("readers") or 0) / math.log1p(3000))
        b["score"] = round(b["support"] * (0.6 + 0.4 * pop), 4)
        b.pop("_best", None)
        b.pop("subjects", None)                 # not needed for display; keeps the table small
        out.append(b)
    out.sort(key=lambda b: -b["score"])
    per, final = {}, []
    for b in out:                                # variety: max 2 per author
        a = S.norm(b.get("author")) or b["id"]
        if per.get(a, 0) < PER_AUTHOR:
            per[a] = per.get(a, 0) + 1
            final.append(b)
    return final[:MAX_PICKS]


def subject_entries(subjects, results, min_readers=10, kids_ok=False):
    """Each subject's ranked books → entries; a book in several of your subjects collects support from each.
    Children's books dominate many OL subjects (Beast Quest under 'monsters'), so they count far less unless
    family/kids titles are part of your taste."""
    hits = {}
    for s, books in zip(subjects, results):
        info = subjects[s]
        for r, b in enumerate(books):
            if (b.get("readers") or 0) < min_readers:
                continue
            kid = any(x.startswith(("juvenile", "children")) for x in b.get("subjects") or [])
            h = hits.setdefault(b["id"], {"b": b, "sup": 0, "subs": []})
            h["sup"] += info["w"] / (1 + 0.04 * r) * (0.35 if kid and not kids_ok else 1)
            h["subs"].append((info["w"], s, info["from"]))
    out = []
    for h in hits.values():
        h["subs"].sort(key=lambda x: -x[0])
        labels = " & ".join(label(s) for _, s, _ in h["subs"][:2])
        names = h["subs"][0][2][:2]
        why = f"{labels} — like {' and '.join(names)}." if names else f"{labels}."
        out.append((h["b"], h["sup"], why, ""))
    return out


def parse_gemini_books(out):
    """Gemini JSON → [{title, author, why}] (tolerates a bare list or missing fields)."""
    lst = out.get("books", []) if isinstance(out, dict) else out if isinstance(out, list) else []
    return [{"title": str(x["title"])[:200], "author": str(x.get("author") or "")[:120], "why": str(x.get("why") or "")[:200]}
            for x in lst if isinstance(x, dict) and x.get("title")][:15]


def _gemini_prompt(seeds, shelf, avoid):
    loved = "\n".join(f"- {it['title']} ({it.get('year', '')}, {'film' if it['kind'] == 'movie' else 'series'}) — "
                      f"{', '.join(it.get('genres', [])[:3])}" for it, _ in seeds[:20])
    read = [f"- {b['title']} by {b['author']}" for b in shelf if b["status"] in ("read", "reading") and b["w"] > 0.3]
    read_txt = ("\nBooks they read and liked:\n" + "\n".join(read[:12])) if read else ""
    avoid_txt = ("\nDon't suggest: " + "; ".join(avoid[:25])) if avoid else ""
    return f"""You recommend BOOKS to one person, based on the films and series they love.

Loved (strongest first):
{loved}{read_txt}{avoid_txt}

Suggest 15 books (novels or gripping narrative non-fiction) they would likely love: match tone, themes, pacing and
storytelling — not just genre. Mix well-known and lesser-known; at most one per author. Skip the books these exact
titles were adapted from (we find those separately). Real, published books only, with the author's usual name.
"why": max 18 words, concrete, name one of their loved titles, no spoilers, no hype words.
Return JSON: {{"books": [{{"title": "", "author": "", "why": ""}}]}}"""


async def compute(http, items, seeds_w):
    """Everything for one refresh → (ranked books, info). Each source fails soft."""
    seeds = S.top_seeds(items, seeds_w, 30)
    fb = feedback()
    entries = []

    # Hardcover shelves (optional): resolve seed books on OL for their subjects; their authors' other books
    shelf = await S.hardcover_shelf(http)
    skip_titles = {S.short_title(b["title"]) for b in shelf}      # already read / on your list
    liked = sorted([b for b in shelf if b["w"] > 0], key=lambda b: -b["w"])[:20]
    found = await asyncio.gather(*[S.ol_find(http, b["title"], b["author"]) for b in liked])
    found = [(f, b["w"]) for f, b in zip(found, liked) if f]
    skip_ids = {f["id"] for f, _ in found} | {i for i, s in fb.items() if s == "dislike"}

    subjects = taste_subjects(seeds)
    for s, e in shelf_subjects(found).items():                    # books you read speak louder than films
        subjects[s] = {"w": max(subjects.get(s, {}).get("w", 0), e["w"] * 1.2), "from": e["from"]}
    results = await asyncio.gather(*[S.ol_subject(http, s) for s in subjects])
    entries += subject_entries(subjects, results, kids_ok="juvenile_fiction" in subjects)

    authors = {}
    for f, w in found:
        authors[f["author"]] = authors.get(f["author"], 0) + w
    top_authors = sorted(authors.items(), key=lambda x: -x[1])[:5]
    for (a, w), works in zip(top_authors, await asyncio.gather(*[S.ol_author_works(http, a) for a, _ in top_authors])):
        entries += [(b, 0.6 + 0.2 * min(w, 2), f"More from {a}, whom you've read.", "Author you read") for b in works]

    # the books behind films/shows you loved
    adapted = [(it, w) for it, w in seeds if S.based_on(it)][:12]
    mx = max((w for _, w in seeds), default=1) or 1
    adapted_books = await asyncio.gather(*[S.based_on_book(http, it) for it, _ in adapted])
    for (it, w), b in zip(adapted, adapted_books):
        entries.append((b, 1.2 + 0.6 * w / mx, f"The book behind {it['title']}" + (f" ({it['year']})." if it.get('year') else "."),
                        "Book of a title you loved"))

    # Gemini: 1 call (cached while your taste is unchanged)
    avoid = [b["title"] for b in shelf] + [b["title"] for b in wants()]
    gem = parse_gemini_books(await S.gemini_cached(http, "books", _gemini_prompt(seeds, shelf, avoid)) or {})
    for i, (g, b) in enumerate(zip(gem, await asyncio.gather(*[S.ol_find(http, g["title"], g["author"]) for g in gem]))):
        if b:
            entries.append((b, 1.6 - 0.04 * i, g["why"], "AI pick"))

    books = merge(entries, skip_ids, skip_titles)
    info = {"ts": time.time(), "hardcover": bool(shelf), "shelf": len(shelf), "subjects": len(subjects),
            "adapted": sum(1 for b in adapted_books if b), "gemini": len(gem), "n": len(books)}
    return books, info


async def post_refresh(ctx):
    books, info = await compute(ctx["http"], ctx["items"], ctx["seeds_w"])
    if books or not picks():                     # a failed run (APIs down) keeps yesterday's picks
        save_picks(books)
    store.meta_put("books:info", info)
    log.info("books: %d picks (%d subjects, %d gemini)", len(books), info["subjects"], info["gemini"])


# ---------------- web ----------------
@fx.router.get("/books", response_class=HTMLResponse)
async def books_page(req: Request):
    from discover import app as A                 # lazy: app imports the features while it is loading
    fb = feedback()
    books = [{**b, "wanted": fb.get(b["id"]) == "want"} for b in picks() if fb.get(b["id"]) != "dislike"]
    return A.tpl.TemplateResponse(req, "features/books.html", {
        "tab": "books", "cards": [], "q": req.query_params, "status": A._status(), "wanted_n": len(store.wants()),
        "books": books, "book_wants": wants(), "info": store.meta_get("books:info") or {}})


@fx.router.post("/books/act/{action}/{bid}")
async def books_act(req: Request, action: str, bid: str):
    if action in ("want", "dislike"):
        data = next((b for b in picks() if b["id"] == bid), None)
        if data is None:                           # e.g. a book shown in the cross-media rows: the page sends it
            try:
                data = {k: v for k, v in (await req.json()).items() if k in ("title", "author", "year", "cover")}
            except Exception:
                data = {}
        set_feedback(bid, action, {k: data.get(k) for k in ("title", "author", "year", "cover")})
    elif action in ("unwant", "undo"):
        set_feedback(bid, None)
    else:
        return Response(status_code=404)
    return JSONResponse({"ok": True, "wants": len(wants())})


fx.TABS.append(("/books", "Books", "📚"))
fx.CSS.append("/static/books.css")
fx.POST_REFRESH.append(post_refresh)
