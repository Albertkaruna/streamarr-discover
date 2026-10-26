import asyncio
import datetime

from discover import features as fx
from discover import store

fx.load()
from discover.features import alerts, tonight  # noqa: E402

TODAY = datetime.date(2026, 10, 26)   # a Monday


def ago(days):
    return (TODAY - datetime.timedelta(days=days)).isoformat()


def show(*seasons, status="Returning Series", nxt=None):
    return {"status": status, "next_episode_to_air": nxt,
            "seasons": [{"season_number": 0, "air_date": ago(4000)}] + [{"season_number": n, "air_date": a}
                                                                        for n, a in seasons]}


def test_new_season_out_after_yours():
    d = show((1, ago(800)), (2, ago(400)), (3, ago(30)), nxt={"season_number": 3, "air_date": ago(-3)})
    a = alerts.season_alerts(d, {1, 2}, TODAY)
    assert [(x["season"], x["state"], x["airing"]) for x in a] == [(3, "out", True)]


def test_old_missing_seasons_are_not_news():
    d = show((1, ago(3000)), (2, ago(2600)), (3, ago(2200)), status="Ended")
    assert alerts.season_alerts(d, {1}, TODAY) == []          # behind on an old show ≠ new season
    assert alerts.season_alerts(d, {1, 2, 3}, TODAY) == []


def test_season_coming_and_tba():
    d = show((1, ago(500)), (2, ago(-40)), (3, None))
    a = alerts.season_alerts(d, {1}, TODAY)
    assert [(x["season"], x["state"], x["date"]) for x in a] == [(2, "coming", ago(-40)), (3, "coming", "")]
    assert alerts.season_alerts(show((1, ago(500)), (2, None), status="Ended"), {1}, TODAY) == []


def test_unknown_library_only_flags_recent_later_seasons():
    d = show((1, ago(20)))                                      # brand-new show you already have
    assert alerts.season_alerts(d, None, TODAY) == []
    d = show((1, ago(700)), (2, ago(200)), (3, ago(10)))
    assert [x["season"] for x in alerts.season_alerts(d, None, TODAY)] == [3]


def test_fresh_works_window_and_roles():
    cr = {"crew": [{"job": "Director", "media_type": "movie", "id": 1, "release_date": ago(30)},
                   {"job": "Director", "media_type": "movie", "id": 2, "release_date": ago(400)},     # too old
                   {"job": "Writer", "media_type": "movie", "id": 3, "release_date": ago(-90)},       # upcoming
                   {"job": "Producer", "media_type": "movie", "id": 4, "release_date": ago(10)},      # wrong job
                   {"job": "Creator", "media_type": "tv", "id": 5, "first_air_date": ago(5), "genre_ids": [10767]},
                   {"job": "Director", "media_type": "movie", "id": 6, "release_date": ""}],          # undated
          "cast": [{"media_type": "movie", "id": 7, "order": 0, "release_date": ago(3)},
                   {"media_type": "movie", "id": 8, "order": 9, "release_date": ago(3)}]}
    assert set(alerts.fresh_works(cr, TODAY)) == {("movie", 1), ("movie", 3)}
    assert set(alerts.fresh_works(cr, TODAY, with_cast=True)) == {("movie", 1), ("movie", 3), ("movie", 7)}


def test_cast_affinity_top_billed_only():
    items = {"a": {"people": [("director", "D"), ("cast", "X"), ("cast", "Y"), ("cast", "Z"), ("cast", "W")]},
             "b": {"people": [("cast", "X")]}}
    aff = alerts.cast_affinity(items, {"a": 2.0, "b": 1.5, "c": 3.0})
    assert aff == {"X": 3.5, "Y": 2.0, "Z": 2.0}


def test_badge_counts_actionable_only():
    data = {"seasons": [{"alerts": [{"state": "out"}, {"state": "coming"}, {"state": "out", "tracking": True}]}],
            "people": [{"key": "movie:1", "date": "2000-01-01"}, {"key": "movie:2", "date": "2999-01-01"},
                       {"key": "movie:3", "date": "2000-01-01"}]}
    assert alerts.badge_count(data, {"movie:3"}) == 2
    assert alerts.nice("2026-11-03", TODAY) == "3 Nov" and alerts.nice("2027-03-01", TODAY) == "1 Mar 2027"
    assert alerts.nice("", TODAY) == "TBA"


def test_tab_label_renders_badge():
    store.meta_put("alerts", {"seasons": [{"alerts": [{"state": "out"}]}], "people": []})
    label = next(lbl for path, lbl, _ in fx.TABS if path == "/new")
    assert label == "New" and label.__html__() == 'New <i class="badge">1</i>'
    store.meta_put("alerts", {})
    assert label.__html__() == "New"


def test_choose_weekday_prefers_short_movies():
    pool = ["movie:1", "movie:2", "movie:3", "tv:9"]
    rt = {"movie:1": 170, "movie:2": 100, "movie:3": 115}
    for i in range(7):
        k = tonight.choose(pool, TODAY + datetime.timedelta(days=i), runtimes=rt)
        weekend = (TODAY + datetime.timedelta(days=i)).weekday() >= 5
        assert k in (pool if weekend else ["movie:2", "movie:3"])
    assert tonight.choose(pool, TODAY, runtimes=rt) == tonight.choose(pool, TODAY, runtimes=rt)  # deterministic
    assert tonight.choose(pool, TODAY, recent=["movie:2"], runtimes=rt) == "movie:3"           # rotates
    assert tonight.choose(["tv:9"], TODAY) is None                                              # no shows on weekdays


def _seed_picks():
    for i in (980001, 980002):
        store.item_put(f"movie:{i}", {"key": f"movie:{i}", "kind": "movie", "tmdb": i, "title": f"M{i}", "year": "2020",
                                      "poster": "/p.jpg", "rating": 7.84, "genres": ["Drama"]})
    store.save_picks("movie", [{"key": "movie:980001", "score": 1, "why": "", "because": ["Dark"]},
                               {"key": "movie:980002", "score": 0.9, "why": "Tense and smart.", "because": []}])
    store.save_picks("tv", [])
    store.meta_put("tonight", {})
    store.meta_put("tonight:recent", [])
    store.meta_put("tonight:runtime", {"movie:980001": 100, "movie:980002": 100})
    store.remove_want("movie:980001")
    store.remove_want("movie:980002")
    store.set_feedback("movie:980001", None)
    store.set_feedback("movie:980002", None)


def _cleanup():   # other test modules share this DB
    store.save_picks("movie", [])
    store.meta_put("tonight", {})


def test_tonight_is_stable_for_the_day_and_api_shape():
    _seed_picks()
    first = tonight.pick(TODAY)
    store.add_want(first["key"])                                # wanting it doesn't swap tonight's pick
    again = tonight.pick(TODAY)
    assert again["key"] == first["key"] and again["wanted"]
    nxt = tonight.pick(TODAY + datetime.timedelta(days=1))      # next day: the other one (wanted ones drop out)
    assert nxt["key"] != first["key"]
    store.remove_want(first["key"])
    store.meta_put("tonight", {"date": datetime.date.today().isoformat(), "key": "movie:980001"})
    j = asyncio.run(tonight.api_tonight())
    assert set(j) == {"title", "year", "kind", "why", "rating", "poster", "url"}
    assert j["title"] == "M980001" and j["why"] == "Because you watched Dark." and j["rating"] == 7.8
    assert j["poster"].endswith("/w342/p.jpg") and j["url"].endswith("/movie/980001") and j["kind"] == "Movie"
    _cleanup()


def test_tonight_dislike_replaces_pick():
    _seed_picks()
    first = tonight.pick(TODAY)
    store.set_feedback(first["key"], "dislike")
    assert tonight.pick(TODAY)["key"] != first["key"]
    store.set_feedback(first["key"], None)
    _cleanup()


def test_people_hook_offline():
    """POST_REFRESH people part with TMDB stubbed: excludes owned, hydrates via tmdb_item, newest first."""
    async def fake_tmdb(http, path, cache=0, **params):
        if path == "/search/person":
            return {"results": [{"id": 7}]}
        return {"crew": [{"job": "Director", "media_type": "movie", "id": 11, "release_date": ago(10)},
                         {"job": "Director", "media_type": "movie", "id": 12, "release_date": ago(60)},
                         {"job": "Director", "media_type": "movie", "id": 13, "release_date": ago(5)}], "cast": []}

    async def fake_item(http, kind, tid):
        return {"key": f"{kind}:{tid}", "adult": False}
    real = alerts.clients.tmdb, alerts.clients.tmdb_item
    alerts.clients.tmdb, alerts.clients.tmdb_item = fake_tmdb, fake_item
    try:
        ctx = {"http": None, "people": {"Some Director": 4.0, "Faint": 0.3}, "items": {}, "seeds_w": {},
               "exclude": {"movie:13"}}
        got = asyncio.run(alerts._new_from_people(ctx, TODAY))
    finally:
        alerts.clients.tmdb, alerts.clients.tmdb_item = real
    assert [w["key"] for w in got] == ["movie:11", "movie:12"] and got[0]["who"] == "Some Director"


def test_new_page_and_hero_render():
    from fastapi.testclient import TestClient

    from discover import app, config
    store.item_put("movie:980011", {"key": "movie:980011", "kind": "movie", "tmdb": 980011, "title": "Fresh One",
                                    "year": "2026", "poster": None, "rating": 7, "genres": []})
    store.meta_put("alerts", {"seasons": [{"key": "tv:5", "tmdb": 5, "title": "Some Show", "poster": None, "tvdb": 9,
                                           "in_sonarr": False, "alerts": [{"season": 3, "state": "out",
                                                                           "date": ago(9), "airing": False}]}],
                              "people": [{"key": "movie:980011", "who": "A Director", "date": ago(5)}]})
    c = TestClient(app.app)
    c.cookies.set("discover", config.SESSION)
    saved = {n: getattr(fx, n)[:] for n in ("CARD_SLOTS", "CARD_ACTIONS", "PAGE_TOP")}
    for n in saved:      # other features' templates may be mid-edit; this test is about ours
        getattr(fx, n)[:] = [x for x in saved[n] if "tonight" in x]
    try:
        _render_checks(c)
    finally:
        for n, v in saved.items():
            getattr(fx, n)[:] = v
        _cleanup()
        store.meta_put("alerts", {})


def _render_checks(c):
    t = c.get("/new").text
    assert "Season 3 out" in t and 'data-season="5/3"' in t and "New from A Director" in t
    assert 'data-key="movie:980011"' in t and '<i class="badge">2</i>' in t
    _seed_picks()
    t = c.get("/movies").text
    assert "tn-hero" in t and "/static/alerts.css" in t
    assert "tn-hero" not in c.get("/shows").text
