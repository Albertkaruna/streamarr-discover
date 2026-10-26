import datetime

from discover import engine

NOW = datetime.datetime(2026, 10, 25, tzinfo=datetime.timezone.utc)


def row(**kw):
    base = {"kind": "movie", "played": False, "playcount": 0, "favorite": False, "last": "2026-10-01T00:00:00Z",
            "percent": 0}
    return {**base, **kw}


def item(key, genres=(), keywords=(), people=(), rating=7.5, votes=2000, lang="en", year="2015"):
    return {"key": key, "genres": list(genres), "keywords": list(keywords), "people": list(people),
            "rating": rating, "votes": votes, "lang": lang, "year": year}


def test_seed_weights_order():
    finished = engine.seed_weight(row(played=True), NOW)
    fav_rewatch = engine.seed_weight(row(played=True, favorite=True, playcount=3), NOW)
    bailed = engine.seed_weight(row(percent=10, last="2026-08-01T00:00:00Z"), NOW)
    assert fav_rewatch > finished > 0 > bailed
    assert engine.seed_weight(row(played=True), NOW, "dislike") == -1.0


def test_library_counts_when_never_played():
    kept = engine.seed_weight(row(last=None), NOW)
    watched = engine.seed_weight(row(played=True, last=None), NOW)
    assert 0 < kept < watched


def test_series_completion_beats_sampling():
    done = engine.seed_weight(row(kind="tv", episodes=26, watched=26), NOW)
    sampled = engine.seed_weight(row(kind="tv", episodes=26, watched=2, last="2026-01-01T00:00:00Z"), NOW)
    assert done > 1.0 and sampled < 0


def test_old_watches_count_less():
    recent = engine.seed_weight(row(played=True, last="2026-10-20T00:00:00Z"), NOW)
    old = engine.seed_weight(row(played=True, last="2022-10-20T00:00:00Z"), NOW)
    assert recent > old > 0


def test_quality_is_bayesian():
    assert engine.quality(item("a", rating=9.5, votes=10)) < engine.quality(item("b", rating=8.2, votes=20000))


def test_rank_prefers_matching_and_supported():
    seed = item("movie:1", ["Science Fiction", "Mystery"], ["time travel"], [("director", "Nolan")])
    match = item("movie:2", ["Science Fiction", "Mystery"], ["time travel"], [("director", "Nolan")])
    off = item("movie:3", ["Romance", "Comedy"], ["wedding"], [("director", "Someone")])
    out = engine.rank([off, match], {"movie:2": 2.0, "movie:3": 0.5}, {"movie:2": ["Seed"]},
                      [(seed, 1.0)], [], {}, limit=2)
    assert [o["key"] for o in out] == ["movie:2", "movie:3"]
    assert out[0]["because"] == ["Seed"]


def test_dislike_pushes_down():
    liked = item("movie:1", ["Drama"], ["prison"])
    hated = item("movie:9", ["Horror"], ["gore", "slasher"])
    a = item("movie:2", ["Horror"], ["gore", "slasher"])
    b = item("movie:3", ["Drama"], ["prison"])
    out = engine.rank([a, b], {"movie:2": 1, "movie:3": 1}, {}, [(liked, 1.0)], [(hated, -1.0)], {}, limit=2)
    assert out[0]["key"] == "movie:3"


def test_mmr_diversifies():
    items = {f"m:{i}": item(f"m:{i}", ["Action"], ["heist"]) for i in range(3)}
    items["m:9"] = item("m:9", ["Drama"], ["family"])
    scored = [{"key": "m:0", "score": 1.0}, {"key": "m:1", "score": 0.99}, {"key": "m:2", "score": 0.98},
              {"key": "m:9", "score": 0.9}]
    out = engine.mmr(scored, items, {}, limit=2)
    assert [o["key"] for o in out] == ["m:0", "m:9"]


def test_rank_uses_module_weights():
    seed = item("movie:1", ["Drama"], ["prison"])
    match = item("movie:2", ["Drama"], ["prison"], rating=6.0, votes=500)       # fits taste, weaker quality
    good = item("movie:3", ["Comedy"], ["wedding"], rating=8.8, votes=50000)    # off-taste, great quality
    sup = {"movie:2": 1, "movie:3": 1}
    assert engine.rank([match, good], sup, {}, [(seed, 1.0)], [], {}, limit=2)[0]["key"] == "movie:2"
    saved = dict(engine.WEIGHTS["plain"])
    try:
        engine.WEIGHTS["plain"] = {"support": 0.1, "content": 0.1, "quality": 0.8}
        assert engine.rank([match, good], sup, {}, [(seed, 1.0)], [], {}, limit=2)[0]["key"] == "movie:3"
    finally:
        engine.WEIGHTS["plain"] = saved


def test_tune_needs_enough_labels():
    ev = [(1, {"support": 1, "content": 1, "meaning": 1, "quality": 1})] * 5
    w, n = engine.tune_weights(ev)
    assert n == 5 and w == engine.DEFAULT_WEIGHTS


def test_tune_leans_toward_separating_component_bounded():
    pos = [(1, {"support": 0.5, "content": 0.9, "meaning": 0.5, "quality": 0.5})] * 10
    neg = [(0, {"support": 0.5, "content": 0.1, "meaning": 0.5, "quality": 0.5})] * 30
    neg += [(-1, {"support": 0.5, "content": 0.2, "meaning": 0.5, "quality": 0.5})] * 6
    w, n = engine.tune_weights(pos + neg)
    assert n == 16
    for mode, d in engine.DEFAULT_WEIGHTS.items():
        assert abs(sum(w[mode].values()) - 1) < 1e-3
        assert w[mode]["content"] > d["content"]
        assert all(d[c] * 0.5 - 1e-3 <= w[mode][c] <= d[c] * 1.5 + 1e-3 for c in d)


def test_rerank_accepts_ids_without_prefix():
    import asyncio
    from discover import ai, clients, config
    items = {"movie:1": item("movie:1", ["Drama"]), "movie:2": item("movie:2", ["Drama"])}
    for k, it in items.items():
        it.update(title=k, kind="movie", overview="")
    orig, key = clients.gemini_json, config.GEMINI_KEY

    async def fake(http, prompt):
        return {"picks": [{"id": "2", "why": "b"}, {"id": "movie:1", "why": "a"}]}
    try:
        clients.gemini_json, config.GEMINI_KEY = fake, "x"
        out = asyncio.run(ai.rerank(None, "movie", [{"key": "movie:1"}, {"key": "movie:2"}], items, [], []))
    finally:
        clients.gemini_json, config.GEMINI_KEY = orig, key
    assert {o["key"]: o["why"] for o in out} == {"movie:1": "a", "movie:2": "b"}
