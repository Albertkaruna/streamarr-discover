"""Taste: rate your library (Loved / OK / Didn't like — you watch copies on the phone, so Jellyfin rarely knows),
a picks report that self-tunes the ranking weights from what you actually want, and "?" explain sheets on cards."""
import json
import logging
import time

from fastapi import Request, Response
from fastapi.responses import HTMLResponse

from discover import engine, store
from discover import features as fx

log = logging.getLogger("discover")
RATINGS = {"loved": "Loved", "ok": "OK", "meh": "Didn't like"}
LABEL = {"wanted": 1, "liked": 1, "disliked": -1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS taste_ratings (key TEXT PRIMARY KEY, rating TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS taste_events (key TEXT, event TEXT, kind TEXT, parts TEXT, ts REAL,
                                         PRIMARY KEY (key, event));   -- event: shown | wanted | liked | disliked
"""
_ready = set()


def _db():
    con = store.db()
    if id(con) not in _ready:                          # one connection per thread; create tables once each
        con.executescript(SCHEMA)
        _ready.add(id(con))
    return con


def ratings():
    return dict(_db().execute("SELECT key, rating FROM taste_ratings"))


def set_rating(key, r):
    with _db() as c:
        if r in RATINGS:
            c.execute("REPLACE INTO taste_ratings VALUES (?,?,?)", (key, r, time.time()))
        else:
            c.execute("DELETE FROM taste_ratings WHERE key=?", (key,))


# ---------------- pure helpers (tested) ----------------
def rated_weight(w, r):
    """Your rating beats whatever Jellyfin guessed: loved > a finish (1.0), OK ≈ "kept it", didn't like = dislike."""
    return {"loved": max(w, 0) + 1.0, "ok": 0.5, "meh": -1.0}.get(r, w)


def labelled_events(rows):
    """rows: [(key, event, parts_json, ts)] → [(label, parts)] one per shown pick. The latest of
    wanted/liked/disliked decides the label; shown with none = 0 (ignored)."""
    by = {}
    for key, ev, parts, ts in rows:
        d = by.setdefault(key, {"parts": None, "label": 0, "ts": -1})
        if parts and parts != "null" and d["parts"] is None:
            d["parts"] = json.loads(parts)
        if ev in LABEL and ts >= d["ts"]:
            d["label"], d["ts"] = LABEL[ev], ts
    return [(d["label"], d["parts"]) for d in by.values() if d["parts"]]


def overlap(item, seeds, n=6):
    """What a pick shares with your top seeds: genres/keywords/people ranked by summed seed weight, plus the
    closest seeds. seeds: [(item, w)] with w > 0."""
    g, k, p, close = {}, {}, {}, []
    roles = {name: role for role, name in item.get("people", [])}
    ig, ik = set(item.get("genres", [])), set(item.get("keywords", []))
    for s, w in seeds:
        if s.get("key") == item.get("key"):
            continue
        sg, sk = ig & set(s.get("genres", [])), ik & set(s.get("keywords", []))
        sp = set(roles) & {name for _, name in s.get("people", [])}
        for name in sg:
            g[name] = g.get(name, 0) + w
        for name in sk:
            k[name] = k.get(name, 0) + w
        for name in sp:
            p[name] = p.get(name, 0) + w
        hit = 0.3 * len(sg) + len(sk) + 1.5 * len(sp)
        if hit:
            close.append((hit * w, s.get("title", "")))
    top = lambda d: [x for x, _ in sorted(d.items(), key=lambda x: (-x[1], x[0]))[:n]]  # noqa: E731
    return {"genres": top(g), "keywords": top(k), "people": [(x, roles[x]) for x in top(p)],
            "closest": [t for _, t in sorted(close, key=lambda x: -x[0])[:3]]}


# ---------------- refresh hooks ----------------
def _seed_hook(seeds_w, info):
    for key, r in ratings().items():                   # 1. your ratings
        seeds_w[key] = rated_weight(seeds_w.get(key, 0), r)
    top = sorted(((k, w) for k, w in seeds_w.items() if w > 0), key=lambda x: -x[1])[:150]
    store.meta_put("seeds_w", {k: round(w, 3) for k, w in top})   # for explain sheets
    rows = _db().execute("SELECT key, event, parts, ts FROM taste_events").fetchall()
    weights, n = engine.tune_weights(labelled_events(rows))      # 2. self-tune (defaults until 15 labels)
    for mode, w in weights.items():
        engine.WEIGHTS[mode] = w
    store.meta_put("taste_weights", {"weights": weights, "labelled": n, "ts": time.time()})


async def _record(ctx):
    """Before this kind's picks are overwritten: log what was shown last time and what you did with it."""
    kind, now = ctx["kind"], time.time()
    wants, fb = store.wants(), store.feedback()
    rows = []
    for p in store.picks(kind):
        parts = json.dumps((p.get("extra") or {}).get("parts"))
        rows.append((p["key"], "shown", kind, parts, now))
        if p["key"] in wants:
            rows.append((p["key"], "wanted", kind, parts, now))
        if fb.get(p["key"]) in ("like", "dislike"):
            rows.append((p["key"], fb[p["key"]] + "d", kind, parts, now))
    with _db() as c:
        c.executemany("INSERT OR IGNORE INTO taste_events VALUES (?,?,?,?,?)", rows)


fx.SEED_HOOKS.append(_seed_hook)
fx.REFRESH_HOOKS.append(_record)
fx.NAV.extend([("/rate", "Rate"), ("/report", "Report")])
fx.CARD_ACTIONS.append("features/explain_act.html")
fx.CSS.append("/static/taste.css?v=1")
fx.JS.append("/static/taste.js?v=1")


# ---------------- pages ----------------
def _page(req, tab, name, **extra):
    """Same context as app.render_page, but our own template (which extends page.html)."""
    from discover import app
    return app.tpl.TemplateResponse(req, name, {"tab": tab, "cards": [], "status": app._status(), "q": req.query_params,
                                                "wanted_n": len(store.wants()), **extra})


@fx.router.get("/rate", response_class=HTMLResponse)
async def rate_page(req: Request):
    lib = store.meta_get("library", [])
    items, rs = store.items_get(lib), ratings()
    kind = req.query_params.get("kind", "")
    tiles = [{**items[k], "r": rs.get(k)} for k in lib if k in items and (not kind or k.startswith(kind + ":"))]
    if req.query_params.get("show") == "unrated":
        tiles = [t for t in tiles if not t["r"]]
    tiles.sort(key=lambda t: (bool(t["r"]), t["title"].lower()))   # unrated first
    rated = sum(1 for k in lib if k in rs)
    return _page(req, "rate", "features/rate.html", tiles=tiles, rated=rated, total=len(lib), kind=kind,
                 show=req.query_params.get("show", ""), ratings=RATINGS)


@fx.router.post("/rate/{key}")
async def rate(key: str, r: str = ""):
    set_rating(key, r)
    lib, rs = store.meta_get("library", []), ratings()
    return {"ok": True, "r": rs.get(key), "rated": sum(1 for k in lib if k in rs), "total": len(lib)}


@fx.router.get("/report", response_class=HTMLResponse)
async def report_page(req: Request):
    rows = _db().execute("SELECT key, event, parts, ts FROM taste_events").fetchall()
    counts = {e: 0 for e in ("shown", *LABEL)}
    for _, ev, _, _ in rows:
        counts[ev] = counts.get(ev, 0) + 1
    ev = labelled_events(rows)
    comps = {}                                        # mean component value: positive vs the rest
    for c in ("support", "content", "meaning", "quality"):
        pv = [p[c] for lab, p in ev if lab > 0 and p.get(c) is not None]
        nv = [p[c] for lab, p in ev if lab <= 0 and p.get(c) is not None]
        comps[c] = (sum(pv) / len(pv) if pv else None, sum(nv) / len(nv) if nv else None)
    tw = store.meta_get("taste_weights", {})
    labelled = sum(1 for lab, _ in ev if lab)
    rs = ratings()
    return _page(req, "report", "features/report.html", counts=counts, labelled=labelled, comps=comps,
                 weights=tw.get("weights") or engine.DEFAULT_WEIGHTS, defaults=engine.DEFAULT_WEIGHTS,
                 tuned=time.strftime("%d %b, %H:%M", time.localtime(tw["ts"])) if tw.get("ts") else None, rating_counts={r: sum(v == r for v in rs.values()) for r in RATINGS},
                 ratings=RATINGS, min_labels=15)


@fx.router.get("/explain/{key}", response_class=HTMLResponse)
async def explain(req: Request, key: str):
    from discover.app import tpl
    it = store.items_get([key]).get(key)
    if not it:
        return Response(status_code=404)
    pick = next((p for p in store.picks(it["kind"]) if p["key"] == key), {})
    parts = (pick.get("extra") or {}).get("parts") or {}
    mode = "emb" if parts.get("meaning") is not None else "plain"
    w = (store.meta_get("taste_weights", {}).get("weights") or engine.DEFAULT_WEIGHTS)[mode]
    sw = store.meta_get("seeds_w", {})
    top = sorted(sw.items(), key=lambda x: -x[1])[:60]
    seeds = store.items_get(k for k, _ in top)
    ov = overlap(it, [(seeds[k], v) for k, v in top if k in seeds])
    bars = [(c, parts[c], w[c]) for c in w if parts.get(c) is not None]
    return tpl.TemplateResponse(req, "features/explain.html", {
        "it": it, "pick": pick, "bars": bars, "ov": ov, "rating": ratings().get(key), "ratings": RATINGS})
