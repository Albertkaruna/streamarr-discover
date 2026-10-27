"""Release radar: TMDB release dates → theatrical/digital (+ estimate), grouping, taste score, arrivals, notify table,
page render — all offline."""
import datetime
import time

from discover import features as fx
from discover import store

fx.load()
from discover.features import radar  # noqa: E402
from discover.features import want_arr as wa  # noqa: E402

TODAY = datetime.date(2026, 10, 26)
META = ("radar", "radar:waiting", "radar:arrivals", "radar:dismissed")


def day(n):
    return (TODAY + datetime.timedelta(days=n)).isoformat()


def rd(*rows):
    out = {}
    for country, typ, date, note in rows:
        out.setdefault(country, []).append({"type": typ, "release_date": date + "T00:00:00.000Z", "note": note})
    return {"results": [{"iso_3166_1": c, "release_dates": v} for c, v in out.items()]}


def test_release_info_india_first_and_real_digital():
    i = radar.release_info(rd(("US", 3, day(-20), ""), ("IN", 3, day(-10), ""), ("IN", 4, day(20), "Prime Video")), "ta")
    assert i["theatrical"] == day(-10) and i["digital"] == day(20) and not i["est"] and i["note"] == "Prime Video"
    i = radar.release_info(rd(("US", 3, day(5), ""), ("GB", 4, day(50), "")), "en")   # no IN → earliest anywhere
    assert i["theatrical"] == day(5) and i["digital"] == day(50)


def test_release_info_estimates_digital():
    i = radar.release_info(rd(("IN", 3, day(0), "")), "ta")
    assert i["est"] and i["digital"] == day(42)                 # Indian film: ~6 weeks
    i = radar.release_info(rd(("IN", 3, day(0), "")), "en")
    assert i["digital"] == day(45)
    i = radar.release_info({"results": []}, "ta", day(10))      # no release_dates → list's primary date
    assert i["theatrical"] == day(10) and i["digital"] == day(52)
    assert radar.release_info({}, "ta")["digital"] == ""        # nothing known → TBA


def test_groups_and_labels():
    g = lambda th, dig="", est=False: radar.group_of({"theatrical": th, "digital": dig, "est": est}, TODAY)  # noqa: E731
    assert g(day(-10), day(32), True) == "theatres"
    assert g(day(10)) == "month" and g(day(60)) == "later" and g("") == "later"
    assert g(day(-50), day(-1)) is None                         # real digital date passed → out, not "coming"
    assert g(day(-50), day(-8), True) == "theatres"             # estimate just passed: maybe late, keep it
    assert g(day(-70), day(-28), True) is None                  # estimate long gone → assume out
    assert radar.badge({"theatrical": day(-3)}, TODAY) == "In theatres"
    assert radar.badge({"theatrical": "2026-12-12"}, TODAY) == "Out 12 Dec"
    assert radar.digital_label({"digital": "2027-01-09", "est": True}, TODAY) == "Digital expected ~Jan 2027"
    assert radar.digital_label({"digital": "2026-11-20", "est": False, "note": "Netflix"}, TODAY) == "Digital 20 Nov · Netflix"


def test_langs_and_score():
    assert radar.langs_to_query({"ta": "off", "hi": "off", "ko": "lots"})[:2] == ["ta", "te"]   # Tamil always
    assert "hi" not in radar.langs_to_query({"hi": "off"})
    assert radar.lang_ok("ta", {"ta": "off"}) and not radar.lang_ok("fr", {"other": "off"})
    items = {"movie:1": {"genres": ["Thriller", "Crime"]}, "movie:2": {"genres": ["Thriller"]}}
    gw = radar.genre_weights({"movie:1": 2.0, "movie:2": 1.0, "tv:9": 5.0}, items)
    assert gw == {"Thriller": 1.0, "Crime": 2 / 3}
    thriller = {"genre_ids": [53, 80], "popularity": 10, "original_language": "ta"}
    romcom = {"genre_ids": [10749, 35], "popularity": 10, "original_language": "ta"}
    assert radar.score(thriller, gw, {}, {"ta": 100}) > radar.score(romcom, gw, {}, {"ta": 100})
    assert radar.score(romcom, gw, {"ta": "lots"}, {"ta": 100}) > radar.score(romcom, gw, {}, {"ta": 100})
    big_en = {"genre_ids": [53], "popularity": 900, "original_language": "en"}
    big_ta = {"genre_ids": [53], "popularity": 60, "original_language": "ta"}   # biggest Tamil film of the month
    assert radar.score(big_ta, gw, {}, {"en": 900, "ta": 60}) > radar.score(big_en, gw, {}, {"en": 900, "ta": 60})


def test_detect_arrivals():
    movies = {1: {"hasFile": True}, 2: {"hasFile": False, "isAvailable": False}, 3: {"hasFile": True}}
    wanted = ["movie:1", "movie:2", "movie:3", "movie:4"]
    dates = {"movie:7": {"digital": day(-1), "est": False}, "movie:8": {"digital": day(-1), "est": True},
             "movie:9": {"digital": day(5), "physical": day(-2)}}
    arr, waiting, done = radar.detect_arrivals({"movie:1", "movie:2"}, wanted, {"movie:2", "movie:4"}, movies,
                                               {"movie:7": 0, "movie:8": 0, "movie:9": 0, "movie:6": 0}, dates, TODAY)
    # 1 was waiting → arrived; 3 had a file but was never seen waiting → no alert; 8 only has an estimate
    assert sorted(arr) == [("movie:1", "downloaded"), ("movie:7", "digital"), ("movie:9", "digital")]
    assert waiting == {"movie:2", "movie:4"} and sorted(done) == ["movie:7", "movie:9"]
    arr, _, _ = radar.detect_arrivals(set(), wanted, set(), movies, {"movie:7": time.time()}, dates, TODAY)
    assert arr == []                                            # first run / already notified → quiet


def test_waiting_status():
    s = lambda m, dl=None, info=None, item=None: radar.waiting_status(m, dl or {}, info, {"imdb": "tt1"} if item is None else item, TODAY)  # noqa: E731
    assert s(None, item={}).startswith("Radarr can't take it")
    assert s(None).startswith("On its way")
    assert s({"hasFile": True}) == "Arrived ✅"
    assert s({"monitored": True}, {"state": "dl", "label": "Downloading 40%"}).startswith("Downloading 40%")
    assert "searching" in s({"monitored": True, "isAvailable": True})
    assert s({"monitored": True, "isAvailable": False, "digitalRelease": "2026-12-05T00:00:00Z"}) == \
        "Radarr waits for the digital release · 5 Dec"
    assert s({"monitored": True, "isAvailable": False}, info={"digital": day(40), "est": True}).endswith("expected ~Dec")


def _snapshot():
    return ({k: store.meta_get(k) for k in META}, store.wants(), radar.notify_keys(),
            dict(wa._st), store.items_get(["movie:901", "movie:902", "movie:903"]))


def _restore(snap):
    meta, wants, notify, st, items = snap
    for k, v in meta.items():
        with store.db() as c:
            c.execute("DELETE FROM meta WHERE k=?", (k,)) if v is None else store.meta_put(k, v)
    for k in ("movie:901", "movie:902", "movie:903"):
        if k not in wants:
            store.remove_want(k)
        with store.db() as c:
            if k not in items:
                c.execute("DELETE FROM items WHERE key=?", (k,))
        if (k in notify) != (k in radar.notify_keys()):
            radar.notify_toggle(k)
    wa._st.clear()
    wa._st.update(st)


def test_flow_card_data_arrivals_and_page():
    snap = _snapshot()
    try:
        today = datetime.date.today()
        d = lambda n: (today + datetime.timedelta(days=n)).isoformat()  # noqa: E731
        for k, rel in (("movie:901", False), ("movie:902", True), ("movie:903", False)):
            store.item_put(k, {"key": k, "kind": "movie", "tmdb": int(k[6:]), "title": "Film " + k[6:], "year": "2026",
                               "lang": "ta", "released": rel, "imdb": "tt" + k[6:], "genres": ["Drama"], "poster": None})
        store.meta_put("radar", {"day": d(0), "coming": [{"key": "movie:901", "score": 1}, {"key": "movie:902", "score": 0.5}],
                                 "dates": {"movie:901": {"theatrical": d(10), "digital": d(52), "est": True},
                                           "movie:902": {"theatrical": d(-5), "digital": d(37), "est": True},
                                           "movie:903": {"theatrical": d(-40), "digital": d(-1), "est": False}}})
        for k in ("radar:waiting", "radar:arrivals", "radar:dismissed"):
            store.meta_put(k, [] if k != "radar:dismissed" else 0)

        cards = [{"key": "movie:901", "kind": "movie", "released": False}, {"key": "movie:902", "kind": "movie", "released": True},
                 {"key": "movie:5", "kind": "movie", "released": True}]
        radar._card_data(cards)
        assert cards[0]["radar"]["badge"].startswith("Out ") and cards[1]["radar"]["badge"] == "In theatres"
        assert "radar" not in cards[2]                          # released & not on the radar → no slot
        assert radar.notify_toggle("movie:901") and radar.notify_toggle("movie:903")
        radar._card_data(cards)
        assert cards[0]["radar"]["notify"]

        store.add_want("movie:902")
        radar.check_arrivals({902: {"hasFile": False, "isAvailable": False, "monitored": True}})
        assert store.meta_get("radar:waiting") == ["movie:902"]
        radar.check_arrivals({902: {"hasFile": True}})
        titles = [a["title"] for a in radar.arrivals()]
        assert "Film 902" in titles and "Film 903" in titles    # 903: notify-only, real digital date passed
        assert radar.notify_keys()["movie:903"]                 # marked done → alerts once
        t = radar.top()
        assert [x["key"] for x in t["tiles"]] == ["movie:901", "movie:902"] and t["tiles"][1]["wanted"]

        from fastapi.testclient import TestClient
        from discover import app, config
        cl = TestClient(app.app)
        cl.cookies.set("discover", config.SESSION)
        wa._st.update(ts=time.time() + 3600, movies={902: {"id": 1, "tmdbId": 902, "hasFile": True}}, mq={})
        r = cl.get("/coming")
        assert r.status_code == 200 and "This month" not in r.text and "Later" not in r.text   # only in-theatres films now
        assert "You&#39;re waiting for" in r.text and "Arrived ✅" in r.text and "Notify me" in r.text
        j = cl.get("/api/radar").json()
        assert j["coming"] == 1 and j["waiting"] == 0 and "Film 902" in j["arrived"]
        assert cl.post("/radar/dismiss").json()["ok"] and cl.get("/api/radar").json()["arrived"] == []
        assert cl.post("/radar/notify/movie:999999").json() == {"ok": False}
        m = cl.get("/movies")
        assert m.status_code == 200 and "coming to streaming" in m.text
    finally:
        _restore(snap)
