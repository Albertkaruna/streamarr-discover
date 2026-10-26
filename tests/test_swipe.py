import asyncio
import os
import tempfile
import time

os.environ.setdefault("DISCOVER_DATA", tempfile.mkdtemp())

from discover import config, store  # noqa: E402
from discover.features import swipe, taste  # noqa: E402


def it(key, title="T", lang="en", genres=("Drama",)):
    return {"key": key, "kind": key.split(":")[0], "tmdb": int(key.split(":")[1]), "title": title, "year": "2020",
            "genres": list(genres), "keywords": [], "people": [], "poster": "/p.jpg", "lang": lang, "rating": 7.1}


def row(key, moods=()):
    return {"key": key, "score": 0.5, "why": "", "because": [], "extra": {"moods": list(moods)}}


def test_reason_weights_decay_and_group():
    now = time.time()
    rows = [("a", "lang", "ko", "Drama", now), ("b", "lang", "ko", "Crime", now - swipe.HALF_LIFE),
            ("c", "genre", "en", "Horror", now), ("d", "slow", "en", "Drama", now), ("e", "no", "en", "Drama", now)]
    w = swipe.reason_weights(rows, now)
    assert abs(w["lang"]["ko"] - 1.5) < 1e-6                 # the 6-month-old one counts half
    assert w["genre"] == {"Horror": 1.0} and w["slow"] == 1.0


def test_reason_rerank_pushes_down_but_bounded():
    items = {f"m:{i}": it(f"movie:{i}") for i in range(20)}
    items["m:0"] = it("movie:0", lang="ko")
    items["m:1"] = it("movie:1", genres=("Horror", "Drama"))
    items["m:2"] = it("movie:2", genres=("Drama", "Horror"))
    rows = [row(f"m:{i}", ("slowburn",) if i == 3 else ()) for i in range(20)]
    wts = {"lang": {"ko": 10}, "genre": {"Horror": 4}, "slow": 3}
    out = [r["key"] for r in swipe.reason_rerank(rows, lambda r: items[r["key"]], wts)]
    assert sorted(out) == sorted(r["key"] for r in rows)     # re-order only
    assert out.index("m:0") > 0 and out.index("m:0") <= int(swipe.MAX_PUSH * 20) + 1    # capped
    assert out.index("m:1") > out.index("m:2")               # top genre pushed harder than a secondary one
    assert out.index("m:3") > 3
    assert swipe.reason_rerank(rows, lambda r: items[r["key"]], {"lang": {}, "genre": {}, "slow": 0}) == rows


def test_refresh_hook_drops_rated_and_reorders():
    taste.set_rating("movie:991003", "ok")
    said = ["movie:991001", "movie:991009", "movie:991010"]       # 3x "not my language" about Korean titles
    for k in said:
        store.item_put(k, it(k, lang="ko"))
        swipe.set_reason(k, "lang")
    try:
        keys = ["movie:991002", "movie:991003"] + [f"movie:99100{i}" for i in range(4, 9)]
        cands = {k: it(k, lang="ko" if k.endswith("2") else "en") for k in keys}
        ctx = {"kind": "movie", "cands": cands, "ranked": [row(k) for k in keys]}
        asyncio.run(swipe.refresh_hook(ctx))
        out = [r["key"] for r in ctx["ranked"]]
        assert "movie:991003" not in out and len(out) == 6           # seen → gone; nothing else dropped
        assert out[0] == "movie:991004" and out[1] == "movie:991002"  # Korean pick nudged down one place
    finally:
        taste.set_rating("movie:991003", "")
        for k in said:
            swipe.set_reason(k, "")


def test_routes_deck_actions_undo():
    from fastapi.testclient import TestClient

    from discover import app, features as fx
    assert ("/swipe", "🔥 Swipe") in fx.NAV
    paths = [p for p, _ in fx.NAV]
    if "/search" in paths:
        assert paths.index("/swipe") == paths.index("/search") + 1
    old_m, old_t, old_lib = store.picks("movie"), store.picks("tv"), store.meta_get("library", [])
    picks = [f"movie:99210{i}" for i in range(6)]
    lib = ["tv:992200", "tv:992201"]
    for k in picks + lib + ["tv:992110"]:
        store.item_put(k, it(k, "Pick" + k[-1]))
    store.save_picks("movie", [{**row(k), "why": "Fits you"} for k in picks])
    store.save_picks("tv", [row("tv:992110")])
    store.meta_put("library", lib)
    c = TestClient(app.app)
    c.cookies.set("discover", config.SESSION)
    try:
        d = swipe.deck()
        keys = [x["key"] for x in d]
        assert keys[:2] == ["movie:992100", "tv:992110"]           # kinds interleaved, best first
        assert d[3]["lib"] and d[3]["key"] in lib and not d[0]["lib"]   # every 4th is your own title
        assert set(keys) == set(picks + lib + ["tv:992110"])
        assert all(not x["lib"] for x in swipe.deck("movie")[:3]) and all(x["kind"] == "movie" for x in swipe.deck("movie"))

        r = c.get("/swipe")
        assert r.status_code == 200 and 'id="sw-data"' in r.text and "Your ✕ reasons" in r.text
        assert "Quick-rate picks" in c.get("/movies").text
        t0 = c.post("/swipe/act/like/movie:992100").json()["today"]
        assert store.feedback().get("movie:992100") == "like"
        c.post("/act/want/movie:992101")                           # client does the real want, then logs it
        assert c.post("/swipe/act/want/movie:992101").json()["today"] == t0 + 1
        c.post("/swipe/act/loved/tv:992200")
        c.post("/swipe/act/skip/movie:992102")
        assert taste.ratings().get("tv:992200") == "loved"
        assert c.post("/swipe/act/skip/movie:992103").json()["today"] == t0 + 2   # skips don't count
        left = {x["key"] for x in c.get("/swipe/deck?skip=movie:992104").json()["cards"]}
        assert left == {"movie:992105", "tv:992110", "tv:992201"}
        assert c.post("/swipe/act/bogus/movie:1").status_code == 404

        assert c.post("/swipe/undo/movie:992101").json()["undone"] == "want" and "movie:992101" not in store.wants()
        c.post("/swipe/undo/movie:992100")
        c.post("/swipe/undo/tv:992200")
        assert "movie:992100" not in store.feedback() and "tv:992200" not in taste.ratings()

        # seen-it from a card: rating hides it from Movies right away, without being a dislike
        c.post("/swipe/act/meh/movie:992105")
        assert "movie:992105" not in c.get("/movies").text and "movie:992105" not in store.feedback()
        c.post("/swipe/undo/movie:992105")
        c.post("/swipe/reason/movie:992104?r=genre")
        assert "Drama ×1" in c.get("/swipe").text
        c.post("/swipe/reason/movie:992104?r=")
        assert not [x for x in swipe.reason_rows() if x[0] == "movie:992104"]
    finally:
        for k in picks + lib + ["tv:992110"]:
            swipe.undo(k)
            store.set_feedback(k, None)
            store.remove_want(k)
            taste.set_rating(k, "")
        store.save_picks("movie", old_m)
        store.save_picks("tv", old_t)
        store.meta_put("library", old_lib)
