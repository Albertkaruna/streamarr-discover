import asyncio


def _fake_item(key, title):
    kind, tid = key.split(":")
    return {"key": key, "kind": kind, "tmdb": int(tid), "title": title, "year": "2016", "overview": "", "poster": None,
            "genres": ["Drama"], "keywords": [], "people": [], "lang": "en", "rating": 8.0, "votes": 5000,
            "popularity": 1, "adult": False, "imdb": None, "tvdb": None, "released": True, "seasons": None,
            "recs": [], "similar": []}


def test_search_page_marks_owned_and_renders_all_sections():
    from fastapi.testclient import TestClient

    from discover import app, config, store
    from discover.features import search

    owned = _fake_item("movie:970001", "Owned Film")
    new = _fake_item("movie:970002", "New Film")
    for it in (owned, new):
        store.item_put(it["key"], it)
    old_lib = store.meta_get("library", [])
    store.meta_put("library", [owned["key"]])
    store.meta_put("music:owned", ["known song"])
    orig = (search.search_titles, search.search_books, search.search_songs)

    async def titles(http, q, kind):
        return [owned, new]

    async def books(http, q):
        return [{"id": "OL1W", "title": "A Book", "author": "Someone", "year": 2001, "cover": None, "wanted": False,
                 "why": "", "tag": "✅ In library", "rating": 4.1}]

    async def songs(http, q, base):
        return [{"title": "Known Song", "artist": "X", "album": "", "year": "2020", "art": "", "preview": None,
                 "have": True, "q": "X Known Song", "tunarr": "#", "ytm": "#"}]
    search.search_titles, search.search_books, search.search_songs = titles, books, songs
    try:
        c = TestClient(app.app)
        c.cookies.set("discover", config.SESSION)
        r = c.get("/search?q=film")
        assert r.status_code == 200 and 'id="sr-q"' in r.text
        assert r.text.count("✅ In library") >= 2                       # book tag + song badge
        assert r.text.index("New Film") < r.text.index("Owned Film")   # new-to-you first
        p = c.get("/search?q=film&type=book&partial=1")
        assert p.status_code == 200 and "<html" not in p.text and "A Book" in p.text and "New Film" not in p.text
        assert "Type a title" in c.get("/search").text
    finally:
        search.search_titles, search.search_books, search.search_songs = orig
        store.meta_put("library", old_lib)


def test_run_survives_one_source_failing():
    from discover.features import search

    class Req:
        class url:
            scheme, hostname = "http", "x"
    orig = (search.search_titles, search.search_books, search.search_songs)

    async def boom(*a):
        raise RuntimeError("down")

    async def ok(*a):
        return [{"title": "S"}]
    search.search_titles, search.search_books, search.search_songs = boom, boom, ok
    try:
        out = asyncio.run(search.run(Req, "q", "all"))
        assert out["titles"] == [] and out["books"] == [] and out["songs"] == [{"title": "S"}]
    finally:
        search.search_titles, search.search_books, search.search_songs = orig
