import asyncio
import os
import tempfile

os.environ.setdefault("DISCOVER_DATA", tempfile.mkdtemp())

from discover import clients, config, store  # noqa: E402
from discover import features as fx  # noqa: E402
from discover.features import shelves  # noqa: E402

BASE = 970000          # ids far from other tests' fixtures


def it(n, lang="en", genres=("Drama",), kind="movie", **kw):
    return {"key": f"{kind}:{BASE + n}", "kind": kind, "tmdb": BASE + n, "title": f"T{n}", "year": "2015",
            "lang": lang, "genres": list(genres), "keywords": [], "rating": 7.2, "votes": 3000, "poster": None,
            "overview": "A plot. More.", "runtime": 120, "seasons": None, **kw}


def pool_ctx(extra=()):
    """60 candidates: 8 Tamil thrillers, 10 short comedies, 12 sci-fi, rest drama; first 30 ranked."""
    cs = [it(i, lang="ta", genres=["Thriller"]) for i in range(8)]
    cs += [it(i, genres=["Comedy"], runtime=90) for i in range(8, 18)]
    cs += [it(i, genres=["Science Fiction"], keywords=["time travel"]) for i in range(18, 30)]
    cs += [it(i) for i in range(30, 60)] + list(extra)
    cands = {c["key"]: c for c in cs}
    seed = {"title": "Vikram", "genres": ["Action"]}
    because = {c["key"]: ["Vikram"] for c in cs[30:40]}
    return {"http": None, "kind": "movie", "cands": cands, "ranked": [{"key": c["key"], "score": 0.5} for c in cs[:30]],
            "support": {}, "because": because, "pos": [(seed, 2.0)], "exclude": {cs[59]["key"]}, "owned": set()}


def _check(found, items):
    seen = {}
    for s in found:
        assert 6 <= len(s["keys"]) <= 12, (s["title"], len(s["keys"]))
        for k in s["keys"]:
            seen[k] = seen.get(k, 0) + 1
            assert k in items
    assert max(seen.values()) <= 2


def test_norm_id_and_parse():
    items = {"movie:5": {}, "movie:6": {}}
    assert shelves.norm_id("5", "movie", items) == "movie:5"
    assert shelves.norm_id("tv:6", "movie", items) == "movie:6"
    out = shelves.parse_gemini({"shelves": [{"title": "A", "subtitle": "b", "ids": [5, "movie:6", "x"]},
                                            {"title": "", "ids": [5]}, "junk", {"title": "B", "ids": "5"}]},
                               "movie", items)
    assert len(out) == 1 and out[0]["keys"] == ["movie:5", "movie:6"]
    assert shelves.parse_gemini("nonsense", "movie", items) == []


def test_finalize_limits_and_tamil_first():
    items = {f"m:{i}": {"lang": "ta" if i < 7 else "en"} for i in range(40)}
    prop = [{"title": f"S{j}", "keys": [f"m:{i}" for i in range(7, 40)]} for j in range(4)]
    prop.append({"title": "Tamil", "keys": [f"m:{i}" for i in range(7)]})
    prop.append({"title": "Tiny", "keys": ["m:1", "m:2"]})
    out = shelves.finalize(prop, items, want_tamil=True)
    assert out[0]["title"] == "Tamil" and "Tiny" not in [s["title"] for s in out]
    _check(out, items)
    assert len({s["id"] for s in out}) == len(out)


def test_rules_without_gemini_and_storage():
    key, old = config.GEMINI_KEY, shelves.load("movie")
    config.GEMINI_KEY = ""
    try:
        ctx = pool_ctx()
        asyncio.run(shelves.shelves_hook(ctx))
        got = shelves.load("movie")
        titles = [s["title"] for s in got]
        assert 4 <= len(got) <= 6, titles
        assert any("Tamil" in t for t in titles) and "Because you loved Vikram" in titles
        assert any(t.startswith("Short") for t in titles)
        keys = {i["key"] for s in got for i in s["items"]}
        assert ctx["exclude"].isdisjoint(keys)
        _check([{"title": s["title"], "keys": [i["key"] for i in s["items"]]} for s in got], ctx["cands"])
    finally:
        config.GEMINI_KEY = key
        shelves.save("movie", old)


def test_gemini_one_call_cached_and_tamil_enforced():
    orig, key, old = clients.gemini_json, config.GEMINI_KEY, shelves.load("movie")
    old_cache, calls = store.meta_get("shelves:gem:movie"), []

    async def fake(http, prompt):
        calls.append(len(prompt))
        ids = [str(BASE + i) for i in range(30, 42)]           # bare ids, no Tamil shelf → rules must add one
        return {"shelves": [{"title": "Quiet dramas", "subtitle": "Slow and human", "ids": ids},
                            {"title": "Time puzzles", "subtitle": "Loops", "ids": [f"movie:{BASE + i}" for i in range(18, 28)]}]}
    clients.gemini_json, config.GEMINI_KEY = fake, "x"
    store.meta_put("shelves:gem:movie", None)
    try:
        asyncio.run(shelves.shelves_hook(pool_ctx()))
        asyncio.run(shelves.shelves_hook(pool_ctx()))           # same pool → cached, no second call
        assert len(calls) == 1 and calls[0] < 40000
        got = shelves.load("movie")
        titles = [s["title"] for s in got]
        assert titles[0].startswith("Tamil") and "Quiet dramas" in titles and "Time puzzles" in titles
        assert any(s["src"] == "gemini" for s in got) and len(got) >= 4
    finally:
        clients.gemini_json, config.GEMINI_KEY = orig, key
        store.meta_put("shelves:gem:movie", old_cache)
        shelves.save("movie", old)


def test_page_top_and_shelf_page_render():
    from fastapi.testclient import TestClient

    from discover import app
    old, old_picks = shelves.load("movie"), store.picks("movie")
    ctx = pool_ctx()
    for c in ctx["cands"].values():
        store.item_put(c["key"], c)
    key = config.GEMINI_KEY
    config.GEMINI_KEY = ""
    saved = fx.PAGE_TOP[:]
    fx.PAGE_TOP[:] = [t for t in saved if "shelves" in t]      # other features' templates may be mid-edit
    try:
        asyncio.run(shelves.shelves_hook(ctx))
        c = TestClient(app.app)
        c.cookies.set("discover", config.SESSION)
        s = shelves.load("movie")[0]
        t = c.get("/movies").text
        assert 'class="sh-tile"' in t and f'/shelf/movie/{s["id"]}#c-{s["items"][0]["key"]}' in t
        assert 'class="sh-tile"' not in c.get("/movies?genre=Drama").text      # hidden while filtering
        page = c.get(f"/shelf/movie/{s['id']}").text
        assert page.count('class="card') == len(s["items"]) and f'data-key="{s["items"][0]["key"]}"' in page
        assert c.get("/shelf/movie/nope").status_code == 404
    finally:
        fx.PAGE_TOP[:] = saved
        config.GEMINI_KEY = key
        shelves.save("movie", old)
        store.save_picks("movie", old_picks)
