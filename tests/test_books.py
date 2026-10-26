"""Books + cross-media: pure helpers and a full offline refresh against a fake HTTP layer."""
import asyncio

from discover import config
from discover.features import books, crossmedia
from discover.features import books_sources as S


def doc(key, title, author, year=2000, readers=500, editions=20, cover=1, subjects=()):
    return {"key": f"/works/{key}", "title": title, "author_name": [author], "first_publish_year": year,
            "readinglog_count": readers, "edition_count": editions, "cover_i": cover, "subject_key": list(subjects)}


SUBJECT_DOCS = {
    "time_travel": [doc("OL1W", "11/22/63", "Stephen King", 2011, 3000), doc("OL2W", "Recursion", "Blake Crouch", 2019, 900)],
    "science_fiction": [doc("OL3W", "Dark Matter", "Blake Crouch", 2016, 2000), doc("OL2W", "Recursion", "Blake Crouch", 2019, 900),
                        doc("OL4W", "Upgrade", "Blake Crouch", 2022, 400)],
    "thrillers": [doc("OL5W", "Gone Girl", "Gillian Flynn", 2012, 2500)],
}
TITLE_DOCS = {
    "the martian": [doc("OL9W", "The Martian", "George du Maurier", 1897, 50), doc("OL10W", "The Martian", "Andy Weir", 2011, 4000)],
    "project hail mary": [doc("OL11W", "Project Hail Mary", "Andy Weir", 2021, 1500)],
}


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def req(self, method, url, *, params=None, headers=None, json_body=None, cache=0, conc=6, tries=4):
        self.calls.append(url)
        p = params or {}
        assert "User-Agent" in (headers or {}) or "themoviedb" in url
        if "openlibrary.org/search.json" in url:
            if "q" in p:
                return {"docs": SUBJECT_DOCS.get(p["q"].split()[0].split(":")[1], [])}
            if "title" in p:
                return {"docs": TITLE_DOCS.get(p["title"].lower(), [])}
            return {"docs": []}
        if "musicbrainz" in url:
            return {"release-groups": [
                {"id": "fan", "score": 100, "title": "Interstellar (Retro)", "count": 1, "primary-type": "Single",
                 "artist-credit": [{"name": "Somebody"}]},
                {"id": "real", "score": 75, "title": "Interstellar: Original Motion Picture Soundtrack", "count": 19,
                 "primary-type": "Album", "artist-credit": [{"name": "Hans Zimmer"}], "first-release-date": "2014-11-17"}]}
        raise AssertionError(f"unexpected call {url}")


def item(key, title, genres=(), keywords=(), people=(), year="2014", kind="movie"):
    return {"key": key, "kind": kind, "title": title, "year": year, "genres": list(genres), "keywords": list(keywords),
            "people": list(people), "poster": "/p.jpg"}


ITEMS = {
    "movie:1": item("movie:1", "Interstellar", ["Science Fiction"], ["time travel", "space", "duringcreditsstinger"]),
    "tv:2": item("tv:2", "Dark", ["Sci-Fi & Fantasy", "Mystery"], ["time travel", "small town"], kind="tv", year="2017"),
    "movie:3": item("movie:3", "The Martian", ["Science Fiction"], ["based on novel or book", "mars"],
                    [("director", "Ridley Scott"), ("writer", "Drew Goddard"), ("writer", "Andy Weir")], year="2015"),
    "movie:4": item("movie:4", "Bad Movie", ["Thriller"]),
}
SEEDS_W = {"movie:1": 3.0, "tv:2": 2.5, "movie:3": 2.0, "movie:4": -1.0}


def test_taste_subjects_keeps_shared_keywords_only():
    subj = books.taste_subjects(S.top_seeds(ITEMS, SEEDS_W, 30))
    assert "time_travel" in subj and subj["time_travel"]["w"] == 1.0          # shared by Interstellar + Dark
    assert "mars" not in subj and "space" not in subj                         # one film only
    assert "duringcreditsstinger" not in subj
    assert "science_fiction" in subj and subj["science_fiction"]["w"] <= 0.7  # genres weigh less
    assert "thrillers" not in subj                                            # only from a disliked seed


def test_best_match_checks_author_and_year():
    docs = TITLE_DOCS["the martian"]
    assert S.best_match(docs, "The Martian")["key"] == "/works/OL10W"         # the famous one by readers
    assert S.best_match(docs, "The Martian", before=1950)["key"] == "/works/OL9W"
    assert S.best_match(docs, "The Martian", authors_any=["Ridley Scott"]) is None
    assert S.best_match(docs, "Martian Chronicles") is None
    assert S.same_author("Weir, Andy", "Andy Weir") and not S.same_author("A. Weir", "Peter Weir")


def test_merge_dedupes_caps_authors_and_skips():
    b = lambda i, t, a, r=100: S.book(doc(i, t, a, readers=r))
    entries = [(b("A", "Dark Matter", "Blake Crouch"), 1.0, "subj", ""),
               (b("A2", "Dark Matter: A Novel", "Blake Crouch"), 1.5, "gemini why", "AI pick"),   # same book
               (b("B", "Recursion", "Blake Crouch"), 0.9, "", ""), (b("C", "Upgrade", "Blake Crouch"), 0.8, "", ""),
               (b("D", "Gone Girl", "Gillian Flynn"), 0.5, "", ""), (b("E", "Read It", "X Y"), 9, "", ""),
               (b("F", "Hated", "Z Q"), 9, "", "")]
    out = books.merge(entries, skip_ids={"F"}, skip_titles={"read it"})
    titles = [x["title"] for x in out]
    assert titles[0] == "Dark Matter" and out[0]["why"] == "gemini why" and out[0]["support"] == 2.5
    assert "Upgrade" not in titles and "Gone Girl" in titles                  # max 2 per author
    assert "Read It" not in titles and "Hated" not in titles


def test_parse_hardcover_and_gemini():
    r = {"data": {"me": [{"user_books": [
        {"status_id": 3, "rating": 5, "book": {"title": "Dune", "contributions": [{"author": {"name": "Frank Herbert"}}]}},
        {"status_id": 5, "rating": None, "book": {"title": "Meh", "contributions": []}},
        {"status_id": 1, "rating": None, "book": {"title": "Later", "contributions": [{"author": {"name": "A B"}}]}},
        {"status_id": 9, "book": {"title": "Unknown status"}}]}]}}
    shelf = S.parse_hardcover(r)
    assert [(b["title"], b["status"]) for b in shelf] == [("Dune", "read"), ("Meh", "dnf"), ("Later", "want")]
    assert shelf[0]["w"] > shelf[2]["w"] > 0 > shelf[1]["w"]
    assert S.parse_hardcover({"errors": ["x"]}) == []
    g = books.parse_gemini_books({"books": [{"title": "X", "author": "Y", "why": "z"}, {"nope": 1}, "junk"]})
    assert g == [{"title": "X", "author": "Y", "why": "z"}]
    assert books.parse_gemini_books(None) == [] and books.parse_gemini_books([{"title": "T"}])[0]["author"] == ""


def test_pick_release_group_prefers_the_real_album():
    groups = asyncio.run(FakeHttp().req("GET", "https://musicbrainz.org/x", headers=S.UA))["release-groups"]
    assert S.pick_release_group(groups, "Interstellar")["artist"] == "Hans Zimmer"
    assert S.pick_release_group(groups[:1], "Something Else") is None


def test_books_refresh_offline_without_gemini():
    config.GEMINI_KEY = ""
    books.save_picks([])
    http = FakeHttp()
    asyncio.run(books.post_refresh({"http": http, "items": ITEMS, "seeds_w": SEEDS_W}))
    got = books.picks()
    ids = [b["id"] for b in got]
    assert "OL10W" in ids and "OL9W" not in ids                               # Weir's Martian, not du Maurier's
    martian = next(b for b in got if b["id"] == "OL10W")
    assert martian["tag"] == "Book of a title you loved" and "The Martian" in martian["why"]
    assert "OL5W" not in ids                                                  # thrillers came only from a dislike
    rec = next(b for b in got if b["id"] == "OL2W")                           # in two of your subjects
    assert "Time travel" in rec["why"] and "Interstellar" in rec["why"]
    assert all("openlibrary" in u for u in http.calls)
    # ✕ and Want
    books.set_feedback("OL2W", "dislike")
    asyncio.run(books.post_refresh({"http": FakeHttp(), "items": ITEMS, "seeds_w": SEEDS_W}))
    assert "OL2W" not in [b["id"] for b in books.picks()]
    books.set_feedback("OL2W", None)
    books.set_feedback("OL10W", "want", {"title": "The Martian", "author": "Andy Weir"})
    assert books.wants()[0]["id"] == "OL10W" and books.wants()[0]["title"] == "The Martian"
    books.set_feedback("OL10W", None)


def test_gemini_books_resolved_and_unknown_dropped():
    async def fake_gem(http, tag, prompt, max_age=0):
        assert "Interstellar" in prompt and "Bad Movie" not in prompt
        return {"books": [{"title": "Project Hail Mary", "author": "Andy Weir", "why": "Like The Martian."},
                          {"title": "A Book That Does Not Exist", "author": "Nobody", "why": "x"}]}
    orig, S.gemini_cached = S.gemini_cached, fake_gem
    try:
        got, info = asyncio.run(books.compute(FakeHttp(), ITEMS, SEEDS_W))
    finally:
        S.gemini_cached = orig
    phm = next(b for b in got if b["id"] == "OL11W")
    assert phm["tag"] == "AI pick" and phm["why"] == "Like The Martian." and info["gemini"] == 2
    assert not any("Does Not Exist" in b["title"] for b in got)


def test_crossmedia_fallbacks_and_gemini():
    config.GEMINI_KEY = ""
    rows = asyncio.run(crossmedia.compute(FakeHttp(), ITEMS, SEEDS_W))
    by = {r["loved"]: r for r in rows}
    assert "Bad Movie" not in by
    assert [p["type"] for p in by["The Martian"]["picks"]][:1] == ["book"]   # adapted-from fallback
    assert by["Interstellar"]["picks"][0]["type"] == "album" and by["Interstellar"]["picks"][0]["by"] == "Hans Zimmer"

    async def fake_gem(http, tag, prompt, max_age=0):
        return {"items": [{"loved": "interstellar", "book": {"title": "Project Hail Mary", "author": "Andy Weir", "why": "w"},
                           "album": {"title": "Interstellar", "artist": "Hans Zimmer", "why": "a"},
                           "watch": {"type": "documentary", "title": "Cosmos", "why": "d"}}]}
    orig, S.gemini_cached = S.gemini_cached, fake_gem
    orig_doc, S.tmdb_doc = S.tmdb_doc, lambda http, t: asyncio.sleep(0, {"title": t, "year": "1980", "url": "u", "cover": ""})
    try:
        rows = asyncio.run(crossmedia.compute(FakeHttp(), ITEMS, SEEDS_W))
    finally:
        S.gemini_cached, S.tmdb_doc = orig, orig_doc
    inter = next(r for r in rows if r["loved"] == "Interstellar")
    assert [p["type"] for p in inter["picks"]] == ["book", "album", "documentary"]
    assert inter["picks"][0]["title"] == "Project Hail Mary" and inter["picks"][0]["why"] == "w"
    crossmedia.save(rows)
    assert crossmedia.rows()[0]["loved"] == rows[0]["loved"]


def test_pages_render():
    from fastapi.testclient import TestClient

    from discover import app as A
    books.save_picks([{**S.book(doc("OL10W", "The Martian", "Andy Weir")), "why": "because", "tag": "AI pick"}])
    crossmedia.save([{"loved": "Interstellar", "key": "movie:1", "year": "2014", "kind": "movie", "poster": None,
                      "picks": [{"type": "album", "icon": "x", "title": "OST", "by": "Hans Zimmer", "why": "", "url": "u", "cover": ""}]}])
    c = TestClient(A.app)
    c.cookies.set("discover", config.SESSION)
    r = c.get("/books")
    assert r.status_code == 200 and "The Martian" in r.text and "Goodreads" in r.text and "Beyond the screen" in r.text
    assert "OST" in r.text and "/static/books.css" in r.text
    r = c.post("/books/act/want/OL10W", headers={"x-requested-with": "fetch"})
    assert r.json() == {"ok": True, "wants": 1}
    assert "Chaptarr isn't set up yet" in c.get("/books").text
    c.post("/books/act/unwant/OL10W")
    assert c.post("/books/act/bogus/OL10W").status_code == 404
    m = c.get("/movies")
    assert m.status_code == 200 and "xm-top" in m.text and "<details" in m.text
