"""Music tab: artists and songs you might like, from what's in Tunarr (your playlists) and Jellyfin's Music library.
Taste → your artists (resolved via MusicBrainz/Deezer) → similar artists (ListenBrainz Labs + Deezer, + Last.fm if
LASTFM_KEY) → their best-known songs (iTunes) minus what you already have → ranked by support from several of your
artists, popularity sanity and a Tamil/English balance close to your own. One small Gemini call (cached a week)
adds Tamil film picks open data is thin on. Runs after each refresh; "+ Tunarr" opens Tunarr's search."""
import asyncio
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from discover import clients, config, store
from discover import features as fx

from . import music_core as mc
from . import music_sources as src

log = logging.getLogger("discover")
TUNARR_STATE = Path(os.environ.get("TUNARR_STATE", "/tunarr/state.json"))
TUNARR_PUBLIC = os.environ.get("TUNARR_PUBLIC_URL", "").rstrip("/")   # else: this host, port 5040
MAX_RESOLVE, MAX_SEEDS, MAX_CANDS, SONG_ARTISTS, MAX_SONGS, PER_ARTIST = 40, 25, 60, 40, 150, 3
TUNARR_SEATS = 8         # of MAX_RESOLVE (half of that among MAX_SEEDS)
BUDGET = 8 * 60          # seconds of uncached lookups per refresh; the rest is picked up next time (all cached)
SCHEMA = """
CREATE TABLE IF NOT EXISTS music_picks (kind TEXT, pos INTEGER, data TEXT, PRIMARY KEY (kind, pos));
CREATE TABLE IF NOT EXISTS music_nope (key TEXT PRIMARY KEY, label TEXT, ts REAL);
"""
_ready = set()

fx.TABS.append(("/music", "Music", "🎵"))
fx.CSS.append("/static/music.css?v=1")
fx.JS.append("/static/music.js?v=1")


def _db():
    con = store.db()
    if id(con) not in _ready:
        con.executescript(SCHEMA)
        _ready.add(id(con))
    return con


def nopes():
    return dict(_db().execute("SELECT key, label FROM music_nope"))


def set_nope(key, label=None):
    with _db() as c:
        if label is None:
            c.execute("DELETE FROM music_nope WHERE key=?", (key,))
        else:
            c.execute("REPLACE INTO music_nope VALUES (?,?,?)", (key, label, time.time()))


def save(kind, rows):
    with _db() as c:
        c.execute("DELETE FROM music_picks WHERE kind=?", (kind,))
        c.executemany("INSERT INTO music_picks VALUES (?,?,?)", [(kind, i, json.dumps(r)) for i, r in enumerate(rows)])


def load(kind):
    return [json.loads(d) for (d,) in _db().execute("SELECT data FROM music_picks WHERE kind=? ORDER BY pos", (kind,))]


def hidden(row, nope):
    return mc.nope_key("artist", row["artist"] if "title" in row else row["name"]) in nope or (
        "title" in row and mc.nope_key("song", row["artist"], row["title"]) in nope)


# ---------------- inputs ----------------
def _tunarr_state():
    try:
        return json.loads(TUNARR_STATE.read_text())
    except Exception as ex:          # not mounted / mid-write: Jellyfin alone still works
        log.info("music: no Tunarr state (%s)", type(ex).__name__)
        return {}


async def _jellyfin_music(http):
    if not config.JELLYFIN_KEY:
        return [], []
    h, base = {"Authorization": f'MediaBrowser Token="{config.JELLYFIN_KEY}"'}, config.JELLYFIN_URL
    try:
        users = await http.req("GET", f"{base}/Users", headers=h)
        uid = next(u["Id"] for u in users if u["Name"].lower() == config.JELLYFIN_USER.lower())
        audio = await http.req("GET", f"{base}/Users/{uid}/Items", headers=h, params={
            "IncludeItemTypes": "Audio", "Recursive": "true", "Fields": "Path", "Limit": "20000"})
        favs = await http.req("GET", f"{base}/Users/{uid}/Items", headers=h, params={
            "IncludeItemTypes": "MusicArtist", "Recursive": "true", "IsFavorite": "true"})
        return audio.get("Items", []), [a["Name"] for a in favs.get("Items", [])]
    except Exception as ex:
        log.warning("music: Jellyfin music unavailable: %s", ex)
        return [], []


async def _gemini(http, seeds, ta_share):
    """≤1 call per refresh, reused for a week while your top artists stay the same."""
    if not config.GEMINI_KEY or not seeds:
        return {}
    sig = "|".join(sorted(s["name"] for s in seeds[:12]))
    old = store.meta_get("music:gemini") or {}
    if old.get("sig") == sig and time.time() - old.get("ts", 0) < 7 * 86400:
        return old.get("out") or {}
    lines = "\n".join(f"- {s['name']} ({mc.INDIAN.get(s['lang'], 'Western/other')})" for s in seeds[:20])
    pct = round(100 * max(0.4, ta_share))
    prompt = f"""You recommend music to one listener in India. Artists in their library (strongest first):
{lines}

Suggest 12 artists (composers, playback singers or bands) they DON'T have yet, and 20 specific well-known songs
(exact titles as on Spotify/Apple Music) they would likely love. About {pct}% should be Tamil film music
(composers like the ones above, era-matched), the rest matching their Western taste. Real, findable songs only.
For Tamil songs put the film name in "film". "why": max 12 words, concrete.
Return JSON: {{"artists": [{{"name": "", "why": ""}}], "songs": [{{"title": "", "artist": "", "film": "", "why": ""}}]}}"""
    try:
        out = await clients.gemini_json(http, prompt)
        out = out if isinstance(out, dict) else {}
    except Exception as ex:
        log.warning("music: gemini failed: %s", ex)
        return old.get("out") or {}
    store.meta_put("music:gemini", {"sig": sig, "ts": time.time(), "out": out})
    return out


# ---------------- the refresh ----------------
async def _resolve(http, a):
    lang = mc.main_lang(a["lang"], None)
    mb = await src.mb_artist(http, a["name"], lang)
    dz = await src.dz_artist(http, mb and mb["name"], a["name"])
    return mb, dz


async def refresh_music(ctx):
    http, t0 = ctx["http"], time.time()
    src.deadline[0] = t0 + BUDGET
    audio, favs = await _jellyfin_music(http)
    artists, owned = mc.taste(_tunarr_state(), audio, favs)
    store.meta_put("music:owned", sorted(owned))       # search marks songs you already have
    if not artists:
        store.meta_put("music:stats", {"seeds": 0, "ts": time.time()})
        return
    nope = nopes()

    # your artists → ids; the same person under several spellings merges by MusicBrainz id
    # Tunarr playlists are small but recent (what you're adding now), so a few of their artists always get a seat
    by_w = sorted(artists.values(), key=lambda a: -a["w"])
    top = by_w[:MAX_RESOLVE - TUNARR_SEATS]
    top += [a for a in by_w if a["tunarr"] and a not in top][:TUNARR_SEATS]
    top += [a for a in by_w if a not in top][:MAX_RESOLVE - len(top)]
    seeds, own = {}, set(artists)
    for a, (mb, dz) in zip(top, await asyncio.gather(*[_resolve(http, a) for a in top])):
        sid = mb["mbid"] if mb else f"dz:{dz['id']}" if dz else f"n:{mc.norm(a['name'])}"
        s = seeds.setdefault(sid, {"name": (mb or dz or a)["name"], "w": 0.0, "lang": {}, "mbid": mb and mb["mbid"],
                                   "dz": dz and dz["id"], "in": (mb or {}).get("country") == "IN", "tunarr": False})
        s["w"] += a["w"]
        s["tunarr"] |= a["tunarr"]
        s["dz"] = s["dz"] or (dz and dz["id"])
        for k, v in a["lang"].items():
            s["lang"][k] = s["lang"].get(k, 0) + v
        own.add(mc.norm(s["name"]))
    for s in seeds.values():
        s["lang"] = mc.main_lang(s["lang"], "ta" if s["in"] else "en")
    total = sum(s["w"] for s in seeds.values()) or 1
    shares = {lg: sum(s["w"] for s in seeds.values() if s["lang"] == lg) / total for lg in ("ta", "in", "en")}
    ta_share = shares["ta"] + shares["in"]
    target = min(0.75, max(0.4, ta_share))           # keep Tamil prominent, never all of it
    ordered = sorted(seeds.items(), key=lambda kv: -kv[1]["w"])
    keep = ordered[:MAX_SEEDS - TUNARR_SEATS // 2]
    keep += [kv for kv in ordered if kv[1]["tunarr"] and kv not in keep][:TUNARR_SEATS // 2]
    keep += [kv for kv in ordered if kv not in keep][:MAX_SEEDS - len(keep)]
    seeds = dict(keep)
    wmax = max(s["w"] for s in seeds.values())
    for s in seeds.values():                         # how much this seed's neighbours count (0..1)
        s["wn"] = max(s["w"] / wmax, 0.35 if s["tunarr"] else 0)
    own_ids = {s["mbid"] for s in seeds.values() if s["mbid"]}

    # similar artists, weighted by how much you like the seed
    cands = {}

    def add(name, mbid, contrib, sid, fans=None, pic=None):
        k = mc.norm(name)
        if not k or k in own or mbid in own_ids or mc.nope_key("artist", name) in nope:
            return
        c = cands.setdefault(k, {"name": name, "mbid": None, "support": {}, "fans": None, "pic": None, "lang_votes": {}})
        c["mbid"] = c["mbid"] or mbid
        c["fans"] = fans if fans is not None else c["fans"]
        c["pic"] = c["pic"] or pic
        c["support"][sid] = max(c["support"].get(sid, 0), contrib)
        lv = seeds[sid]["lang"] if sid in seeds else None
        if lv:
            c["lang_votes"][lv] = c["lang_votes"].get(lv, 0) + contrib

    sids = list(seeds)
    lb, dz, lf = await asyncio.gather(
        asyncio.gather(*[src.lb_similar(http, seeds[s]["mbid"]) if seeds[s]["mbid"] else _empty() for s in sids]),
        asyncio.gather(*[src.dz_related(http, seeds[s]["dz"]) if seeds[s]["dz"] else _empty() for s in sids]),
        asyncio.gather(*[src.lastfm_similar(http, seeds[s]["name"]) for s in sids]))
    for sid, l1, l2, l3 in zip(sids, lb, dz, lf):
        wn = seeds[sid]["wn"]
        for name, mbid, sim in l1:
            add(name, mbid, wn * sim, sid)
        for name, sim, fans, pic in l2:
            add(name, None, 0.9 * wn * sim, sid, fans, pic)
        for name, mbid, sim in l3:
            add(name, mbid, 0.8 * wn * sim, sid)

    gem = await _gemini(http, sorted(seeds.values(), key=lambda s: -s["w"]), ta_share)
    gem_why = {}
    for g in (gem.get("artists") or [])[:15]:
        if isinstance(g, dict) and g.get("name"):
            add(g["name"], None, 0.3, "_gemini")
            gem_why[mc.norm(g["name"])] = str(g.get("why", ""))[:120]

    def rescore(rows):                               # support/popularity score × how much you listen to that language
        for c in rows:
            c["score"] = round(c["score"] * mc.lang_factor(c["lang"], shares), 4)
        return sorted(rows, key=lambda c: -c["score"])

    for c in cands.values():
        c["lang"] = mc.main_lang(c["lang_votes"], "en")
    ranked = mc.balance(rescore(mc.score_artists(cands, seeds)), target, MAX_CANDS)

    # fan counts + photos (Deezer) for the shortlist, then their best-known songs
    for c, d in zip(ranked, await asyncio.gather(*[src.dz_artist(http, c["name"]) if c["fans"] is None else _none()
                                                  for c in ranked])):
        if d:
            c["fans"], c["pic"] = d["fans"], c["pic"] or d["pic"]
    ranked = mc.balance(rescore(mc.score_artists({c["key"]: c for c in ranked}, seeds)), target, MAX_CANDS)
    songs_of = await asyncio.gather(*[src.top_songs(http, c["name"], c.get("mbid")) for c in ranked[:SONG_ARTISTS]])
    for c in ranked:
        c["examples"], c["songs"] = [], []
        c["why"] = mc.why_text(c["because"], "" if c["because"] else gem_why.get(c["key"], ""))
    for c, ss in zip(ranked, songs_of):
        if ss:                                       # the songs' store genre ("Tamil") beats the seed vote
            lang = mc.artist_lang([x["lang"] for x in ss], c["lang"])
            c["score"] = round(c["score"] * mc.lang_factor(lang, shares) / mc.lang_factor(c["lang"], shares), 4)
            c["lang"] = lang
        keep = [x for x in ss if not mc.is_owned(x["title"], owned)
                and mc.nope_key("song", c["name"], x["title"]) not in nope]
        keep.sort(key=lambda x: -mc.lang_factor(x["lang"] or c["lang"], shares))   # stable: your languages first
        c["examples"] = [{"title": x["title"], "art": x["art"]} for x in keep[:3]]
        c["pic"] = c["pic"] or (ss[0]["art"] if ss else None)
        c["songs"] = keep[:PER_ARTIST]
    songs = []
    for c in ranked:
        for i, x in enumerate(c.pop("songs")):
            lang = x["lang"] if x["lang"] != "en" or c["lang"] == "en" else c["lang"]
            songs.append({**x, "artist": c["name"], "credit": x["artist"], "lang": lang or c["lang"], "why": c["why"],
                          "score": round(c["score"] * mc.lang_factor(lang, shares) / (1 + 0.3 * i), 4)})

    # Gemini's specific songs, kept only if iTunes knows them (no invented titles)
    best = max([x["score"] for x in songs] + [0.5])
    gsongs = [g for g in (gem.get("songs") or [])[:20] if isinstance(g, dict) and g.get("title") and g.get("artist")]
    for g, hit in zip(gsongs, await asyncio.gather(*[src.find_song(http, g["title"], g["artist"]) for g in gsongs])):
        if hit and not mc.is_owned(hit["title"], owned) and mc.nope_key("song", g["artist"], hit["title"]) not in nope \
                and mc.nope_key("artist", g["artist"]) not in nope:
            songs.append({**hit, "artist": g["artist"], "credit": hit["artist"], "score": round(0.6 * best, 4),
                          "why": str(g.get("why") or "")[:140] or "Gemini pick", "film": g.get("film") or ""})

    seen, uniq = set(), []
    for x in sorted(songs, key=lambda x: -x["score"]):
        k = mc.title_core(x["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    songs = mc.balance(uniq, target, MAX_SONGS)
    shown = [c for c in ranked if c["examples"]] or [c for c in ranked if c["why"]]
    artists_out = mc.balance(sorted(shown, key=lambda c: -c["score"]), target, 30)
    keep_a = ("name", "key", "score", "because", "why", "pic", "fans", "lang", "examples", "mbid")
    save("artist", [{k: c.get(k) for k in keep_a} for c in artists_out])
    save("song", songs)
    store.meta_put("music:stats", {"artists_in_library": len(artists), "seeds": len(seeds), "candidates": len(cands),
                                   "artists": len(artists_out), "songs": len(songs), "tamil_share": round(ta_share, 2),
                                   "gemini": bool(gem), "seconds": round(time.time() - t0), "ts": time.time()})
    log.info("music: %d seeds → %d candidates → %d artists, %d songs in %ds", len(seeds), len(cands),
             len(artists_out), len(songs), time.time() - t0)


async def _empty():
    return []


async def _none():
    return None


fx.POST_REFRESH.append(refresh_music)


# ---------------- page + actions ----------------
def _tunarr_base(req):
    return TUNARR_PUBLIC or f"{req.url.scheme}://{req.url.hostname}:5040"


def _links(row, base):
    q = f"{row['artist']} {row['title']}" if "title" in row else row["name"]
    enc = urllib.parse.quote(q)
    return {"q": q, "tunarr": f"{base}/?q={enc}#search", "ytm": f"https://music.youtube.com/search?q={enc}"}


@fx.router.get("/music", response_class=HTMLResponse)
async def music_page(req: Request):
    from discover import app                         # late import: app imports the features
    nope, base = nopes(), _tunarr_base(req)
    artists = [{**a, **_links(a, base)} for a in load("artist") if not hidden(a, nope)]
    for a in artists:
        a["examples"] = [{**e, **_links({"artist": a["name"], "title": e["title"]}, base)} for e in a.get("examples") or []]
    songs = [{**s, **_links(s, base)} for s in load("song") if not hidden(s, nope)]
    return app.tpl.TemplateResponse(req, "features/music.html", {
        "tab": "music", "cards": [], "status": app._status(), "q": req.query_params, "wanted_n": len(store.wants()),
        "artists": artists, "songs": songs, "mstats": store.meta_get("music:stats") or {}, "hidden_n": len(nope)})


@fx.router.post("/music/nope")
async def music_nope(req: Request):
    p = req.query_params
    kind, artist, title = p.get("kind", "song"), p.get("artist", ""), p.get("title", "")
    if not artist:
        return {"ok": False}
    key = mc.nope_key(kind, artist, title)
    set_nope(key, f"{artist} — {title}" if kind == "song" else artist)
    return {"ok": True, "key": key}


@fx.router.post("/music/undo")
async def music_undo(req: Request):
    set_nope(req.query_params.get("key", ""), None)
    return {"ok": True}
