import json
from pathlib import Path

from discover.features import music_core as mc


def test_split_artists_drops_site_junk():
    assert mc.split_artists("Hariharan - MassTamilan.com") == ["Hariharan"]
    assert mc.split_artists("S.P.Balasubramaniam, Sujatha 5eli.com") == ["S.P.Balasubramaniam", "Sujatha"]
    assert mc.split_artists("S. P. Balasubrahmanyam/S. Janaki") == ["S. P. Balasubrahmanyam", "S. Janaki"]
    assert mc.split_artists("EelamMp3.Com & TamilGood.Com Free  High Quality Tamil Mp3 Songs Free [ Download 320Kbps ]") == []
    assert mc.split_artists("www.eelammp3.com") == []
    assert mc.split_artists("AC/DC") == ["AC/DC"]
    assert mc.split_artists("Nicky Jam x J. Balvin") == ["Nicky Jam", "J. Balvin"]
    assert mc.split_artists("Shakira ft. Dizzee Rascal") == ["Shakira", "Dizzee Rascal"]


def test_norm_and_title_core():
    assert mc.norm("S. P. Balasubrahmanyam") == mc.norm("s p  balasubrahmanyam")
    assert mc.norm("Beyoncé") == "beyonce"
    assert mc.title_core('Mundhinam (From "Vaaranam Aayiram")') == "mundhinam"
    assert mc.title_core("Livin' La Vida Loca - Remastered") == "livinlavidaloca"
    assert mc.is_owned("Mundhinam [Remastered]", {"mundhinam"}) and not mc.is_owned("Hey", {"hey"})


def test_taste_from_tunarr_and_jellyfin():
    state = {"playlists": {"u": {"name": "Mix", "tracks": {
        "a": {"artist": "Ricky Martin", "title": "Maria (Official Video)", "status": "done"},
        "b": {"artist": "Ricky Martin", "title": "Livin' La Vida Loca", "status": "pending"},
        "c": {"artist": "Saregama Tamil", "title": "Endi Ippadi | Enakkul Oruvan", "artist_wrong": True, "status": "done"}}}}}
    jf = [{"Name": f"Song {i}", "Path": f"/data/Music/Tamil/Harris Jayaraj/Album/{i}.mp3",
           "ArtistItems": [{"Name": "Karthik - MassTamilan.com"}], "UserData": {"PlayCount": 0}} for i in range(6)]
    jf.append({"Name": "Numb", "Path": "/data/Music/Rock Songs/Linkin Park/Numb.mp3",
               "ArtistItems": [{"Name": "Linkin Park"}], "UserData": {"PlayCount": 9, "IsFavorite": True}})
    jf.append({"Name": "X", "Path": "/data/Music/Tamil/Collection/Super Hits/x.mp3", "ArtistItems": [], "UserData": {}})
    artists, owned = mc.taste(state, jf, ["Queen"])
    assert "saregama tamil" not in artists                       # label channel, not an artist
    assert {"ricky martin", "harris jayaraj", "karthik", "linkin park", "queen"} <= set(artists)
    assert "super hits" not in artists and "collection" not in artists
    assert set(artists["harris jayaraj"]["lang"]) == {"ta"} and abs(artists["harris jayaraj"]["lang"]["ta"] - 4.2) < 1e-9
    assert mc.main_lang(artists["linkin park"]["lang"]) == "en"
    assert {"numb", "maria", "song0"} <= owned


def test_real_tunarr_state_parses_if_mounted():
    p = Path("/tunarr/state.json")
    if p.exists():                                              # mounted in the docker test run; counts only
        artists, owned = mc.taste(json.loads(p.read_text()), [])
        assert isinstance(artists, dict) and isinstance(owned, set)


def test_pick_mb_prefers_alias_match():
    res = [{"id": "x", "name": "Anthakudi Ilayaraja", "score": 100, "country": "IN", "aliases": []},
           {"id": "y", "name": "Ilaiyaraaja", "score": 79, "country": "IN", "aliases": [{"name": "Ilayaraja"}]}]
    assert mc.pick_mb(res, "Ilayaraja", "ta")["mbid"] == "y"
    assert mc.pick_mb([{"id": "z", "name": "Somebody", "score": 40}], "Nobody") is None


def test_parse_sources():
    lb = [{"artist_mbid": "m1", "name": "Anirudh Ravichander", "score": 35},
          {"artist_mbid": "m2", "name": "Yuvan Shankar Raja", "score": 24}]
    out = mc.parse_lb_similar(lb)
    assert out[0] == ("Anirudh Ravichander", "m1", 1.0) and abs(out[1][2] - 24 / 35) < 1e-9
    assert mc.parse_lb_similar([{"data": lb}])[0][0] == "Anirudh Ravichander"
    dz = mc.parse_dz_related({"data": [{"name": "Vidyasagar", "nb_fan": 5000, "picture_medium": "p"}]})
    assert dz[0][0] == "Vidyasagar" and dz[0][2] == 5000
    assert mc.pick_dz([{"id": 1, "name": "Harris Jayaraj & Karthik"}, {"id": 2, "name": "Harris Jayaraj", "nb_fan": 9}],
                      "Harris Jayaraj")["id"] == 2
    it = {"results": [
        {"wrapperType": "track", "trackName": "Mundhinam", "artistName": "Harris Jayaraj, Naresh Iyer",
         "collectionName": "Vaaranam Aayiram (Original Motion Picture Soundtrack)", "primaryGenreName": "Tamil",
         "artworkUrl100": "https://a/100x100bb.jpg", "previewUrl": "https://p", "releaseDate": "2008-01-01T00:00:00Z"},
        {"wrapperType": "track", "trackName": "Mundhinam (Karaoke)", "artistName": "Harris Jayaraj"},
        {"wrapperType": "track", "trackName": "Other", "artistName": "Someone Else"}]}
    songs = mc.parse_itunes(it, "Harris Jayaraj")
    assert [s["title"] for s in songs] == ["Mundhinam"]
    assert songs[0]["lang"] == "ta" and songs[0]["album"] == "Vaaranam Aayiram" and "160x160" in songs[0]["art"]
    assert mc.lang_of_genre("Regional Indian") == "in" and mc.lang_of_genre("Alternative") == "en"


def test_score_rewards_support_from_several_artists():
    seeds = {"a": {"name": "A"}, "b": {"name": "B"}, "c": {"name": "C"}}
    cands = {"multi": {"name": "Multi", "support": {"a": 0.5, "b": 0.5, "c": 0.4}, "fans": 50000},
             "single": {"name": "Single", "support": {"a": 1.0}, "fans": 50000},
             "tiny": {"name": "Tiny", "support": {"a": 0.9, "b": 0.6}, "fans": 12}}
    out = mc.score_artists(cands, seeds)
    assert out[0]["key"] == "multi" and out[0]["because"][:2] == ["A", "B"]
    assert mc.why_text(["A", "B"]) == "Similar to A and B"
    assert mc.why_text(["A", "B", "C"]) == "Similar to A, B and C"


def test_balance_keeps_tamil_prominent():
    items = [{"k": i, "lang": "en"} for i in range(10)] + [{"k": 10 + i, "lang": "ta"} for i in range(10)]
    out = mc.balance(items, 0.5, 10)
    assert sum(x["lang"] == "ta" for x in out) == 5 and out[0]["lang"] == "ta"
    only_en = mc.balance([{"lang": "en"}] * 3, 0.6, 10)      # not enough Tamil → still fills
    assert len(only_en) == 3


def test_nope_keys_are_fuzzy():
    assert mc.nope_key("artist", "S. P. Balasubrahmanyam") == mc.nope_key("artist", "s p balasubrahmanyam")
    assert mc.nope_key("song", "A", "Mundhinam (From Vaaranam Aayiram)") == mc.nope_key("song", "a", "mundhinam")


def test_music_page_and_not_interested():
    from fastapi.testclient import TestClient

    from discover import app, config
    from discover.features import music
    music.save("artist", [{"name": "Vidyasagar", "key": "vidyasagar", "why": "Similar to Harris Jayaraj", "lang": "ta",
                           "examples": [{"title": "Malare Mounama", "art": ""}], "pic": None, "because": ["Harris Jayaraj"]}])
    music.save("song", [{"title": "Mundhinam", "artist": "Harris Jayaraj", "album": "Vaaranam Aayiram", "year": "2008",
                         "lang": "ta", "art": "", "preview": "https://p", "why": "Similar to A and B", "score": 1},
                        {"title": "Numb", "artist": "Linkin Park", "lang": "en", "why": "", "score": 0.5}])
    c = TestClient(app.app)
    c.cookies.set("discover", config.SESSION)
    r = c.get("/music")
    assert r.status_code == 200 and "Mundhinam" in r.text and "Vidyasagar" in r.text and "Malare Mounama" in r.text
    assert "music.youtube.com/search?q=Harris%20Jayaraj%20Mundhinam" in r.text and ":5040/?q=" in r.text
    k = c.post("/music/nope?kind=song&artist=Harris+Jayaraj&title=Mundhinam").json()["key"]
    assert "Mundhinam" not in c.get("/music").text
    c.post(f"/music/nope?kind=artist&artist=Vidyasagar")
    assert "Vidyasagar" not in c.get("/music").text
    c.post("/music/undo", params={"key": k})
    assert "Mundhinam" in c.get("/music").text
    music.set_nope(mc.nope_key("artist", "Vidyasagar"), None)
