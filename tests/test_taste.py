import json

from discover import engine, store
from discover.features import taste


def it(key, title, genres=(), keywords=(), people=()):
    return {"key": key, "kind": key.split(":")[0], "tmdb": int(key.split(":")[1]), "title": title, "year": "2020",
            "genres": list(genres), "keywords": list(keywords), "people": [list(p) for p in people], "poster": None}


def test_rated_weight():
    assert taste.rated_weight(1.0, "loved") > taste.rated_weight(1.0, None) == 1.0   # loved beats a finish
    assert taste.rated_weight(-0.3, "loved") == 1.0
    assert taste.rated_weight(1.2, "ok") == 0.5
    assert taste.rated_weight(0.4, "meh") == -1.0


def test_labelled_events_latest_label_wins():
    p = json.dumps({"support": 0.5})
    rows = [("a", "shown", p, 1), ("a", "liked", p, 2), ("a", "disliked", p, 3),
            ("b", "shown", p, 1), ("b", "wanted", p, 2),
            ("c", "shown", p, 1),
            ("d", "shown", "null", 1)]                  # no score breakdown → can't learn from it
    got = sorted(lab for lab, _ in taste.labelled_events(rows))
    assert got == [-1, 0, 1]


def test_overlap_ranks_by_seed_weight():
    pick = it("movie:1", "Pick", ["Drama", "Crime"], ["heist", "prison"], [("director", "Ann"), ("cast", "Bo")])
    s1 = it("movie:2", "Strong", ["Drama"], ["heist"], [("director", "Ann")])
    s2 = it("movie:3", "Weak", ["Crime"], ["prison"], [("cast", "Bo")])
    s3 = it("movie:4", "Unrelated", ["Comedy"], ["wedding"])
    ov = taste.overlap(pick, [(s1, 2.0), (s2, 0.5), (s3, 3.0), (pick, 9.0)])
    assert ov["closest"] == ["Strong", "Weak"]
    assert ov["keywords"] == ["heist", "prison"] and ov["genres"] == ["Drama", "Crime"]
    assert ov["people"] == [("Ann", "director"), ("Bo", "cast")]


def test_seed_hook_applies_ratings_and_records_events():
    lib = ["movie:990001", "tv:990002", "movie:990003"]
    for k in lib:
        store.item_put(k, it(k, "T" + k[-1]))
    store.meta_put("library", lib)
    taste.set_rating("movie:990001", "loved")
    taste.set_rating("tv:990002", "meh")
    taste.set_rating("movie:990003", "ok")
    taste.set_rating("movie:990003", "")           # clear
    seeds = {"movie:990001": 1.0, "tv:990002": 0.4, "movie:990003": 0.4}
    taste._seed_hook(seeds, {})
    assert seeds == {"movie:990001": 2.0, "tv:990002": -1.0, "movie:990003": 0.4}
    assert store.meta_get("seeds_w")["movie:990001"] == 2.0
    assert engine.WEIGHTS == engine.DEFAULT_WEIGHTS                   # not enough labels yet

    import asyncio
    old = store.picks("movie")
    parts = {"support": 0.4, "content": 0.6, "meaning": None, "quality": 0.7}
    store.save_picks("movie", [{"key": "movie:990010", "score": 0.5, "extra": {"parts": parts}},
                               {"key": "movie:990011", "score": 0.4, "extra": {"parts": parts}}])
    store.add_want("movie:990010")
    store.set_feedback("movie:990011", "dislike")
    try:
        asyncio.run(taste._record({"kind": "movie"}))
        ev = dict(((k, e), 1) for k, e, _, _ in taste._db().execute("SELECT key, event, parts, ts FROM taste_events"))
        assert ("movie:990010", "wanted") in ev and ("movie:990011", "disliked") in ev
        assert ("movie:990011", "shown") in ev
    finally:
        store.remove_want("movie:990010")
        store.set_feedback("movie:990011", None)
        store.save_picks("movie", old)
        for k in lib:
            taste.set_rating(k, "")


def test_pages_render():
    from fastapi.testclient import TestClient

    from discover import app, config
    store.item_put("movie:990020", it("movie:990020", "Pick", ["Drama"], ["heist"]))
    store.meta_put("library", ["movie:990020"])
    c = TestClient(app.app)
    c.cookies.set("discover", config.SESSION)
    r = c.get("/rate?kind=movie")
    assert r.status_code == 200 and "0</b> of 1 rated" in r.text and 'data-key="movie:990020"' in r.text
    assert c.post("/rate/movie:990020?r=loved").json()["rated"] == 1
    assert "1</b> of 1 rated" in c.get("/rate").text
    assert c.get("/rate?show=unrated").text.count("tx-tile") == 0
    taste.set_rating("movie:990020", "")
    assert c.get("/report").status_code == 200
    r = c.get("/explain/movie:990020")
    assert r.status_code == 200 and "Why Pick?" in r.text
    assert c.get("/explain/movie:1").status_code == 404
    old = store.picks("movie")
    store.item_put("movie:990021", it("movie:990021", "Seed", ["Drama"], ["heist"]))
    store.meta_put("seeds_w", {"movie:990021": 1.5})
    store.save_picks("movie", [{"key": "movie:990020", "score": 0.6, "because": ["Seed"],
                                "extra": {"parts": {"support": 0.4, "content": 0.9, "meaning": 0.5, "quality": 0.7}}}])
    try:
        t = c.get("/explain/movie:990020").text
        assert t.count("tx-row") == 4 and "heist" in t and "Because you watched" in t
    finally:
        store.save_picks("movie", old)
