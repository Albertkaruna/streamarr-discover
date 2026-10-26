"""Person pages: credit splitting/sorting (pure) and the page/search/explain wiring with TMDB faked."""
CREDITS = {
    "crew": [
        {"id": 980001, "media_type": "movie", "job": "Original Music Composer", "department": "Sound", "title": "Score A",
         "release_date": "2010-05-01", "vote_average": 8.0, "vote_count": 400, "popularity": 5},
        {"id": 980002, "media_type": "movie", "job": "Music", "department": "Sound", "title": "Score B",
         "release_date": "2020-01-01", "vote_average": 9.5, "vote_count": 3, "popularity": 9},
        {"id": 980003, "media_type": "movie", "job": "Music Director", "department": "Sound", "title": "Score C",
         "release_date": "2001-01-01", "vote_average": 7.0, "vote_count": 2000, "popularity": 2},
        {"id": 980001, "media_type": "movie", "job": "Songs", "department": "Sound", "title": "Score A"},   # merged
        {"id": 980004, "media_type": "movie", "job": "Music Editor", "department": "Sound", "title": "Not Theirs"},
        {"id": 980005, "media_type": "movie", "job": "Producer", "department": "Production", "title": "Produced"},
        {"id": 980006, "media_type": "tv", "job": "Director", "department": "Directing", "name": "A Show",
         "first_air_date": "2015-02-02", "vote_average": 7.5, "vote_count": 50},
    ],
    "cast": [
        {"id": 980007, "media_type": "movie", "character": "Ravi", "title": "Cameo", "release_date": "2012-01-01"},
        {"id": 980008, "media_type": "tv", "character": "Himself", "name": "Awards Night"},
        {"id": 980009, "media_type": "tv", "character": "Judge", "name": "Talk Show", "genre_ids": [10767]},
    ],
}


def _item(key, title):
    kind, tid = key.split(":")
    return {"key": key, "kind": kind, "tmdb": int(tid), "title": title, "year": "2010", "overview": "", "poster": None,
            "genres": ["Drama"], "keywords": [], "people": [("director", "Some Director")], "lang": "ta",
            "rating": 8.0, "votes": 400, "popularity": 1, "adult": False, "imdb": None, "tvdb": None,
            "released": True, "seasons": None, "recs": [], "similar": []}


def test_split_credits_sections_roles_and_noise():
    from discover.features import people
    s = people.split_credits(CREDITS, "Sound")
    assert set(s) == {"direct", "music", "cast"}                 # no "prod": not a producer by trade
    music = {r["key"]: r for r in s["music"]}
    assert set(music) == {"movie:980001", "movie:980002", "movie:980003"}   # Music Editor isn't their work
    assert music["movie:980001"]["roles"] == ["Music"]
    assert [r["title"] for r in s["cast"]] == ["Cameo"] and s["cast"][0]["roles"] == ["as Ravi"]
    assert "prod" in people.split_credits(CREDITS, "Production")
    assert people.section_order(s, "Sound")[0] == "music"
    assert people.section_order(s, "Acting")[0] == "cast"


def test_order_bayesian_needs_votes_and_dates():
    from discover.features import people
    rows = people.split_credits(CREDITS, "Sound")["music"]
    best = [r["title"] for r in people.order(rows, "best")]
    assert best == ["Score A", "Score C", "Score B"]              # 9.5 from 3 votes goes last
    assert [r["title"] for r in people.order(rows, "new")][0] == "Score B"
    assert [r["title"] for r in people.order(rows, "old")][0] == "Score C"
    for r in rows:
        r["state"] = "lib" if r["key"] == "movie:980003" else ""
    assert [r["key"] for r in people.keep(rows, "lib")] == ["movie:980003"]
    assert len(people.keep(rows, "missing")) == 2


def test_person_page_search_people_and_by_name():
    from fastapi.testclient import TestClient

    from discover import app, clients, config, store
    calls, hydrated = [], []

    async def tmdb(http, path, cache=0, **params):
        calls.append(path)
        if path == "/person/77":
            return {"id": 77, "name": "Test Composer", "known_for_department": "Sound", "biography": "Line one.",
                    "birthday": "1975-01-08", "place_of_birth": "Chennai", "profile_path": None}
        if path == "/person/77/combined_credits":
            return CREDITS
        if path == "/search/person":
            return {"results": [{"id": 5, "name": "Other"}, {"id": 77, "name": "Test Composer"}]}
        if path == "/search/multi":
            return {"results": [{"media_type": "person", "id": 77, "name": "Test Composer", "known_for_department": "Sound",
                                 "known_for": [{"media_type": "movie", "id": 980003}]}]}
        raise AssertionError(path)

    async def tmdb_item(http, kind, tid):
        hydrated.append(tid)
        it = _item(f"{kind}:{tid}", f"Hydrated {tid}")
        store.item_put(it["key"], it)
        return it

    keys = [f"movie:{i}" for i in range(980001, 980010)] + ["tv:980006", "tv:980008", "tv:980009"]
    owned = "movie:980003"
    store.item_put(owned, _item(owned, "Score C"))
    old_lib = store.meta_get("library", [])
    store.meta_put("library", [owned])
    orig = (clients.tmdb, clients.tmdb_item)
    clients.tmdb, clients.tmdb_item = tmdb, tmdb_item
    try:
        c = TestClient(app.app)
        c.cookies.set("discover", config.SESSION)
        r = c.get("/person/77")
        assert r.status_code == 200 and "Test Composer" in r.text
        t = r.text
        assert t.index('id="music"') < t.index('id="direct"') < t.index('id="cast"')   # composer → Music first
        assert "You have 1 of 3" in t                              # music: 1 owned of 3
        assert "Awards Night" not in t and "Talk Show" not in t
        assert owned.split(":")[1] not in map(str, hydrated)       # already stored → no TMDB call
        assert len(hydrated) <= 40
        m = c.get("/person/77?music=lib")
        assert m.status_code == 200 and "Hydrated 980001" not in m.text.split('id="direct"')[0]
        lc = c.get("/person/card/movie:980005?why=Producer")
        assert lc.status_code == 200 and 'data-key="movie:980005"' in lc.text and "Producer" in lc.text
        b = c.get("/person/by-name?name=test composer", follow_redirects=False)
        assert b.status_code == 303 and b.headers["location"] == "/person/77"
        s = c.get("/search?q=composer&type=movie&partial=1")
        assert s.status_code == 200 and 'href="/person/77"' in s.text and "People" in s.text
        bad = c.get("/person/abc")
        assert bad.status_code in (404, 422)
    finally:
        clients.tmdb, clients.tmdb_item = orig
        store.meta_put("library", old_lib)
        with store.db() as con:
            con.executemany("DELETE FROM items WHERE key=?", [(k,) for k in keys])
