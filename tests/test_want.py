"""Want options, variant lists, Sonarr/Radarr plans, card status, Tamil profile, trailer pick — all offline."""
import time

from discover import store
from discover.features import want_arr as wa
from discover.features import want_like as wl

MOVIE = {"key": "movie:1", "kind": "movie", "tmdb": 1, "lang": "en", "title": "M", "imdb": "tt1"}
TAMIL_MOVIE = {"key": "movie:2", "kind": "movie", "tmdb": 2, "lang": "ta", "title": "T", "imdb": "tt2"}
SHOW = {"key": "tv:3", "kind": "tv", "tmdb": 3, "tvdb": 33, "lang": "en", "title": "S"}


def series(stats=None):
    return {"id": 9, "tvdbId": 33, "monitored": False, "qualityProfileId": 6, "monitorNewItems": "all",
            "seasons": [{"seasonNumber": n, "monitored": False,
                         "statistics": (stats or {}).get(n, {"episodeFileCount": 0, "totalEpisodeCount": 10})}
                        for n in (0, 1, 2, 3)]}


def test_clean_opts_and_variant():
    o = wa.clean_opts({"quality": "720", "tamil": True, "pick": "custom", "seasons": [2, "1", 0, "x"]}, SHOW)
    assert o == {"quality": "720", "tamil": True, "pick": "custom", "seasons": [1, 2]}
    assert wa.variant(o, SHOW) == "tamil720"
    assert wa.clean_opts({"pick": "custom", "seasons": []}, SHOW)["pick"] == "all"
    assert wa.clean_opts({"tamil": True}, TAMIL_MOVIE)["tamil"] is False          # already Tamil
    assert wa.clean_opts({"pick": "first"}, MOVIE)["pick"] == "all"               # no seasons for movies
    assert wa.variant(None, MOVIE) == "" and wa.variant({"quality": "720"}, MOVIE) == "720"
    assert wa.variant({"tamil": True}, TAMIL_MOVIE) == ""


def test_feed_each_want_on_exactly_one_list():
    items = {"movie:1": MOVIE, "movie:2": TAMIL_MOVIE, "movie:5": {**MOVIE, "key": "movie:5"}}
    opts = {"movie:1": {"tamil": True}, "movie:5": {"tamil": True, "quality": "720"}}
    live = set(wa.VARIANTS)
    lists = {v: [i["key"] for i in wa.feed_items(items, opts, v, live)] for v in wa.VARIANTS}
    assert lists == {"": ["movie:2"], "tamil": ["movie:1"], "720": [], "tamil720": ["movie:5"]}
    # variant lists not set up in the arr yet → everything rides on the plain list
    assert len(wa.feed_items(items, opts, "", {""})) == 3
    assert wa.feed_items(items, opts, "tamil", {""}) == []


def test_plan_series_picks():
    s, search = wa.plan_series(series(), {"pick": "latest"}, 7)
    assert s["monitored"] and s["qualityProfileId"] == 7 and s["monitorNewItems"] == "all"
    assert [x["monitored"] for x in s["seasons"]] == [False, False, False, True] and search == [3]
    s, search = wa.plan_series(series(), {"pick": "first"}, 6)
    assert [x["monitored"] for x in s["seasons"]] == [False, True, False, False] and s["monitorNewItems"] == "none"
    s, search = wa.plan_series(series({2: {"episodeFileCount": 10, "totalEpisodeCount": 10}}), None, 6)
    assert [x["monitored"] for x in s["seasons"]] == [False, True, True, True] and search == [1, 3]   # S2 complete
    s, _ = wa.plan_series(series(), {"pick": "custom", "seasons": [7, 8]}, 6)     # numbering mismatch → all
    assert [x["monitored"] for x in s["seasons"]] == [False, True, True, True]


def test_plan_movie():
    m, search = wa.plan_movie({"id": 1, "monitored": True, "qualityProfileId": 6}, 6)
    assert not search
    m, search = wa.plan_movie({"id": 1, "monitored": False, "qualityProfileId": 6}, 6)
    assert search and m["monitored"]
    assert wa.plan_movie({"monitored": True, "qualityProfileId": 6}, 11)[1]


def test_queue_counts_season_pack_once():
    recs = [{"seriesId": 2, "downloadId": "A", "size": 100, "sizeleft": 40, "status": "downloading"}] * 3 + \
           [{"seriesId": 2, "downloadId": "B", "size": 100, "sizeleft": 100, "status": "paused"}]
    q = wa.queue_by(recs, "seriesId")
    assert q == {2: {"size": 200, "left": 140, "active": True}}


def test_card_status_states_and_sizes():
    st = {"movies": {1: {"id": 10, "hasFile": False, "monitored": True}, 2: {"id": 20, "hasFile": True, "sizeOnDisk": 2.34e9}},
          "mq": {10: {"size": 100, "left": 58, "active": True}}, "sq": {},
          "series": {("tvdb", 33): {"id": 9, "monitored": True, "statistics": {"episodeFileCount": 0, "episodeCount": 20,
                                                                           "totalEpisodeCount": 62}}}}
    assert wa.card_status(MOVIE, None, (), st) == {"state": "dl", "label": "Downloading 42%", "size": "~2.5 GB"}
    assert wa.card_status(TAMIL_MOVIE, {"quality": "720"}, (), st)["label"] == "In library"
    assert wa.card_status(TAMIL_MOVIE, None, (), st)["size"] == "2.3 GB"           # real size once on disk
    st["mq"][10]["active"] = False
    assert wa.card_status(MOVIE, None, (), st)["label"] == "Queued"
    sh = wa.card_status(SHOW, {"quality": "1080"}, (), st)
    assert sh == {"state": "want", "label": "Wanted", "size": "~1 GB/ep × 62 eps"}
    wa._eps["tv:3"] = {1: 10, 2: 12, 3: 8}
    assert wa.card_status(SHOW, {"pick": "latest", "quality": "720"}, (), st)["size"] == "~0.5 GB/ep × 8 eps"
    assert wa.card_status({**MOVIE, "key": "movie:99", "tmdb": 99}, None, {"movie:99"}, st)["label"] == "In library"
    assert wa.card_status({**MOVIE, "key": "movie:98", "tmdb": 98}, None, (), st)["state"] == ""


def _base(app):
    q = lambda i, n, r: {"quality": {"id": i, "name": n, "resolution": r}, "items": [], "allowed": True}
    off = {"quality": {"id": 1, "name": "SDTV", "resolution": 480}, "items": [], "allowed": False}
    return {"id": 6, "name": "HD - 720p/1080p", "upgradeAllowed": False, "cutoff": 4, "minFormatScore": 0,
            "cutoffFormatScore": 0, "minUpgradeFormatScore": 1, "language": {"id": -2, "name": "Original"},
            "items": [off, q(4, "HDTV-720p", 720),
                      {"id": 1001, "name": "WEB 720p", "allowed": True, "items": [q(5, "WEBDL-720p", 720)]},
                      q(9, "HDTV-1080p", 1080),
                      {"id": 1002, "name": "WEB 1080p", "allowed": True, "items": [q(3, "WEBDL-1080p", 1080)]}],
            "formatItems": [{"format": 1, "name": "Block CAM/TS", "score": -10000}]}


def test_tamil_profile():
    cfs = [{"id": 1, "name": "Block CAM/TS"}, {"id": 2, "name": wa.CF_TAMIL}, {"id": 3, "name": wa.CF_1080}]
    p = wa.tamil_profile(_base("radarr"), "HD - Tamil preferred", cfs, False, "radarr")
    grp = next(i for i in p["items"] if i.get("id") == p["cutoff"])
    assert grp["allowed"] and [x["quality"]["id"] for x in grp["items"]] == [4, 5, 9, 3]
    assert sum(1 for i in p["items"] if i.get("allowed")) == 1                  # one group → CF score decides
    assert {f["name"]: f["score"] for f in p["formatItems"]} == {"Block CAM/TS": -10000, wa.CF_TAMIL: 1000, wa.CF_1080: 100}
    assert p["upgradeAllowed"] and p["cutoffFormatScore"] == 1000 and p["minFormatScore"] == 0
    assert p["language"]["id"] == -1 and "id" not in p
    p7 = wa.tamil_profile(_base("sonarr"), "x", cfs, True, "sonarr")
    grp = next(i for i in p7["items"] if i.get("id") == p7["cutoff"])
    assert [x["quality"]["id"] for x in grp["items"]] == [4, 5]
    assert not any(i.get("allowed") for i in p7["items"] if i is not grp)
    assert p7["language"]["id"] == -2                                           # sonarr: untouched field
    assert {f["name"]: f["score"] for f in p7["formatItems"]}[wa.CF_1080] == 0


def test_tamil_regexes():
    import re
    hit = lambda s: bool(re.search(wa.TAMIL_RE, s, re.I) or re.search(wa.MULTI_RE, s, re.I))
    for s in ("Movie.2023.1080p.WEB-DL.Tamil.Dubbed", "Movie 2023 [Tam + Tel + Hin] 720p", "Movie.2023.TA.EN.1080p",
              "Movie (2023) Hin-Tam 720p HDRip", "Show S01 [Te + Ta + Hi]"):
        assert hit(s), s
    for s in ("Movie.2023.1080p.WEB-DL.DDP5.1", "Tamara.2016.720p", "Stamped.2020.1080p", "Movie.MULTi.1080p"):
        assert not hit(s), s


def test_list_body_and_redact():
    base = {"id": 2, "name": wa.LIST, "qualityProfileId": 6, "shouldMonitor": "all", "searchForMissingEpisodes": True,
            "enableAutomaticAdd": False, "fields": [{"name": "baseUrl", "value": "http://discover:5050/lists/sonarr?key=SECRET"}]}
    b = wa.list_body(base, "sonarr", "tamil720", 12)
    assert b["name"] == "Streamarr Discover (Tamil 720p)" and b["qualityProfileId"] == 12 and "id" not in b
    assert b["fields"][0]["value"] == "http://discover:5050/lists/sonarr?key=SECRET&variant=tamil720"
    assert b["shouldMonitor"] == "none" and b["searchForMissingEpisodes"] is False and b["enableAutomaticAdd"] is True
    assert "SECRET" not in wa._redact(b["fields"][0]["value"])
    r = wa.list_body({**base, "fields": [{"name": "link", "value": "http://d/lists/radarr?key=K"}]}, "radarr", "720", 3)
    assert r["fields"][0]["value"].endswith("variant=720") and r["shouldMonitor"] == "all"   # radarr keeps its settings


def test_opts_store_and_pending():
    store.item_put(SHOW["key"], SHOW)
    store.meta_put("want:epoch", time.time() - 100)
    store.add_want("tv:3")
    wa.opts_put("tv:3", {"pick": "first"})
    assert wa.opts_get("tv:3") == {"pick": "first"} and "tv:3" in wa.pending("tv")
    wa.mark_applied("tv:3")
    assert "tv:3" not in wa.pending("tv") and wa.opts_get("tv:3") == {"pick": "first"}
    time.sleep(0.01)
    store.add_want("tv:3")                                 # re-wanted later (e.g. via old /act/want) → pending again
    assert "tv:3" in wa.pending("tv")
    store.meta_put("want:epoch", time.time() + 10)          # wants older than the feature are left alone
    assert "tv:3" not in wa.pending("tv")
    store.remove_want("tv:3")


def test_best_video():
    vids = [{"site": "YouTube", "key": "teaser", "type": "Teaser", "official": True, "iso_639_1": "en"},
            {"site": "Vimeo", "key": "v", "type": "Trailer", "official": True},
            {"site": "YouTube", "key": "fan", "type": "Trailer", "official": False, "iso_639_1": "en"},
            {"site": "YouTube", "key": "en", "type": "Trailer", "official": True, "iso_639_1": "en"},
            {"site": "YouTube", "key": "ta", "type": "Trailer", "official": True, "iso_639_1": "ta"}]
    assert wl.best_video(vids, "en").endswith("=en")
    assert wl.best_video(vids, "ta").endswith("=ta")
    assert wl.best_video([vids[0], vids[1]], "en").endswith("=teaser")
    assert wl.best_video([vids[1]], "en") is None


def test_templates_render():
    from discover import app as web
    c = {**MOVIE, "poster": None, "year": "2020", "genres": [], "why": "", "because": [], "fb": None, "wanted": True,
         "rating": 7, "dl": {"state": "dl", "label": "Downloading 5%", "size": "~2.5 GB"}, "trailer": False}
    html = web.tpl.env.get_template("_card.html").render(c=c, fx=web.features)
    assert 'data-wst="movie:1"' in html and "Downloading 5%" in html and "wtrailer off" in html
    assert 'href="/like/movie:1"' in html and 'data-want="movie:1"' in html and 'data-act="unwant"' in html
    html = web.tpl.env.get_template("_card.html").render(c={**c, "wanted": False, "dl": None}, fx=web.features)
    assert 'data-want="movie:1" data-act="want"' in html
    assert "/static/want.js?v=1" in web.features.JS and "features/want_slot.html" in web.features.CARD_SLOTS


def test_like_page_template():
    from discover import app as web
    c = {**MOVIE, "poster": None, "year": "2020", "genres": [], "why": "", "because": ["S"], "fb": None, "wanted": False,
         "rating": 7}
    html = web.tpl.env.get_template("features/like.html").render(
        tab="like", cards=[c], seed={"title": "Seed", "year": "1999", "poster": "/p.jpg"}, err="",
        status={"running": False, "step": "", "error": "", "last": None, "setup": []}, q={}, wanted_n=0, fx=web.features)
    assert "More like" in html and "Seed" in html and 'data-key="movie:1"' in html
