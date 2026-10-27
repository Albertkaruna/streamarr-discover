import asyncio
import os
import tempfile

os.environ.setdefault("DISCOVER_DATA", tempfile.mkdtemp())

from discover import clients, config, store  # noqa: E402
from discover.features import browse  # noqa: E402


def card(key, lang="en", genres=("Drama",), score=0.5, votes=5000, rating=7.0, kind="movie", **kw):
    return {"key": key, "kind": kind, "lang": lang, "genres": list(genres), "score": score, "votes": votes,
            "rating": rating, "keywords": [], "extra": {}, **kw}


def test_lang_rerank_drops_off_and_boosts_lots():
    rows = [card(f"m:{i}", lang="en") for i in range(10)] + [card("m:ta", lang="ta"), card("m:fr", lang="fr")]
    prefs = {**{g: "some" for g in browse.LANGS}, "ta": "lots", "other": "off"}
    out = browse.lang_rerank(rows, lambda r: r["lang"], prefs)
    keys = [r["key"] for r in out]
    assert "m:fr" not in keys                       # "other" is Off
    assert keys.index("m:ta") < 10                  # lifted from the bottom (#11)


def test_lang_rerank_caps_one_language():
    rows = [card(f"en:{i}", lang="en") for i in range(40)] + [card(f"ko:{i}", lang="ko") for i in range(20)]
    some = {g: "some" for g in browse.LANGS}
    top = browse.lang_rerank(rows, lambda r: r["lang"], some)[:30]
    assert sum(r["lang"] == "en" for r in top) == 18          # 60% of 30
    only_en = {**some, "en": "lots"}                           # sole Lots → allowed to dominate
    top = browse.lang_rerank(rows, lambda r: r["lang"], only_en)[:30]
    assert sum(r["lang"] == "en" for r in top) == 30


def test_lang_rerank_everything_off_keeps_list():
    rows = [card("a", lang="en"), card("b", lang="en")]
    out = browse.lang_rerank(rows, lambda r: r["lang"], {g: "off" for g in browse.LANGS})
    assert len(out) == 2


def test_genre_or_and_across_groups():
    cs = [card("a", genres=["Drama"]), card("b", genres=["Comedy"]), card("c", genres=["Horror"], lang="ta")]
    sel = {"genre": ["Drama", "Comedy"]}
    assert [c["key"] for c in cs if browse.match(c, sel)] == ["a", "b"]
    sel = {"genre": ["Drama", "Horror"], "lang": ["ta"]}
    assert [c["key"] for c in cs if browse.match(c, sel)] == ["c"]


def test_moments_and_gems():
    short = card("s", runtime=95)
    long_ = card("l", runtime=150)
    binge = card("b", kind="tv", seasons=2)
    gem = card("g", votes=800, rating=7.6)
    assert browse.match(short, {"pick": ["short"]}) and not browse.match(long_, {"pick": ["short"]})
    assert browse.match(binge, {"pick": ["binge"]}) and not browse.match(short, {"pick": ["binge"]})
    assert browse.is_gem(gem) and not browse.is_gem(short)
    u = {"langs": ["ta", "en"], "genres": ["Drama", "Action"]}
    assert browse.is_new(card("k", lang="ko"), u) and not browse.is_new(card("e"), u)
    assert browse.is_new(card("w", genres=["Western"]), u)


def test_heuristic_moods():
    assert "light" in browse.heuristic_moods({"genres": ["Comedy"], "keywords": []})
    assert "mind" in browse.heuristic_moods({"genres": ["Drama"], "keywords": ["time travel"]})
    assert "dark" in browse.heuristic_moods({"genres": ["Crime"], "keywords": []})
    assert browse.moods_of({"extra": {"moods": ["romantic"]}, "genres": ["Horror"]}) == ["romantic"]


def test_href_toggles_and_keeps_others():
    q = {"genre": ["Drama"], "lang": ["ta"], "x": ["1"]}
    h = browse.href(q, "genre", "Comedy")
    assert "genre=Drama" in h and "genre=Comedy" in h and "lang=ta" in h
    assert "genre" not in browse.href(q, "genre", "Drama")
    assert browse.href(q) == "?x=1"


def test_usual():
    seeds = [({"lang": "ta", "genres": ["Drama", "Action"]}, 3.0), ({"lang": "ko", "genres": ["Romance"]}, 0.1)]
    u = browse.usual(seeds)
    assert u["langs"] == ["ta"] and "Romance" not in u["genres"]


def test_mood_hook_without_gemini():
    key = config.GEMINI_KEY
    config.GEMINI_KEY = ""
    try:
        ctx = {"kind": "movie", "http": None, "cands": {"movie:1": card("movie:1", genres=["Comedy"], title="x")},
               "ranked": [{"key": "movie:1", "score": 0.5}]}
        asyncio.run(browse.mood_hook(ctx))
        assert "light" in ctx["ranked"][0]["extra"]["moods"]
    finally:
        config.GEMINI_KEY = key


def test_settings_prefs_default_some():
    old = store.pref("languages")
    store.set_pref("languages", {"ta": "lots", "en": "bogus"})
    try:
        p = browse.lang_prefs()
        assert p["ta"] == "lots" and p["en"] == "some" and p["other"] == "some"
    finally:                                     # shared test DB: don't re-rank other tests' pages
        store.set_pref("languages", old)


def test_normalize_runtime_countries():
    base = {"id": 1, "genres": [], "production_countries": [{"iso_3166_1": "IN"}], "origin_country": ["IN"]}
    m = clients.normalize("movie", {**base, "runtime": 95})
    assert m["runtime"] == 95 and m["countries"] == ["IN"]
    t = clients.normalize("tv", {**base, "episode_run_time": [], "last_episode_to_air": {"runtime": 42}})
    assert t["runtime"] == 42
    assert clients.normalize("tv", {"id": 2})["runtime"] is None


def test_anime_and_country_filters():
    tv = [card("a", kind="tv", lang="ja", genres=["Animation"]), card("b", kind="tv", keywords=["anime"]),
          card("c", kind="tv", countries=["US"]), card("d", kind="tv", lang="ja", genres=["Drama"], countries=["JP"])]
    assert [c["key"] for c in tv if browse.match(c, {"type": ["anime"]})] == ["a", "b"]
    assert [c["key"] for c in tv if browse.match(c, {"type": ["live"]})] == ["c", "d"]
    mv = [card("x", countries=["IN", "US"]), card("y", countries=["US", "IN"]), card("z")]
    assert [c["key"] for c in mv if browse.match(c, {"country": ["IN"]})] == ["x"]          # primary country only
    assert [c["key"] for c in mv if browse.match(c, {"country": ["US", "?"]})] == ["y", "z"]
