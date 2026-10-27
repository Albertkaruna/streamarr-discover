"""Web: sign-in, Movies/Shows/Wanted pages, one-tap actions, wishlist feeds for Sonarr/Radarr, daily refresh."""
import asyncio
import base64
import contextlib
import datetime
import functools
import hashlib
import hmac
import logging
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import clients, config, features, pipeline, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discover")
HERE = Path(__file__).parent
STATIC = HERE / "static"
KINDS = {"movies": "movie", "shows": "tv"}
TITLES = {"movies": "Movies", "shows": "Shows", "wanted": "Wanted"}
IMMUTABLE = "public, max-age=31536000, immutable"
features.load()


# ---------------- assets: one hashed CSS bundle, hashed JS urls → cached forever, re-fetched only when changed ----------------
@functools.cache
def _bundle():
    """style.css + every feature stylesheet (registry order, de-duplicated) → (css, short hash)."""
    files = dict.fromkeys(u.split("?")[0].removeprefix("/static/") for u in ["/static/style.css", *features.CSS])
    css = "\n".join((STATIC / f).read_text() for f in files)
    return css, hashlib.md5(css.encode()).hexdigest()[:10]


@functools.cache
def asset(url):
    """/static/x.js(?v=…) → /static/x.js?v=<content hash>."""
    path = url.split("?")[0]
    return f"{path}?v={hashlib.md5((STATIC / path.removeprefix('/static/')).read_bytes()).hexdigest()[:10]}"


def icon(name, cls=""):
    """Inline SVG from the sprite in _icons.html."""
    return Markup(f'<svg class="i {cls}" aria-hidden="true"><use href="#i-{name}"/></svg>')


tpl = Jinja2Templates(HERE / "templates")
tpl.env.globals.update(fx=features, asset=asset, icon=icon, css_url=lambda: f"/assets/app.css?v={_bundle()[1]}")


async def scheduler():
    """Refresh ~2 min after start if the last run is stale, then daily at REFRESH_HOUR (local time)."""
    await asyncio.sleep(120)
    while True:
        last = store.meta_get("last_refresh", 0)
        now = datetime.datetime.now()
        due_today = now.hour >= config.REFRESH_HOUR and datetime.datetime.fromtimestamp(last).date() < now.date()
        if config.TMDB_KEY and config.JELLYFIN_KEY and (time.time() - last > 20 * 3600 or due_today):
            await pipeline.refresh()
        await asyncio.sleep(600)


@contextlib.asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.add_middleware(GZipMiddleware, minimum_size=1024)      # pages are ~100 KB of repetitive HTML → ~12 KB


@app.get("/assets/app.css")
async def css_bundle():
    return Response(_bundle()[0], media_type="text/css", headers={"Cache-Control": IMMUTABLE})


@app.exception_handler(httpx.HTTPError)
async def upstream_down(req: Request, ex):
    """TMDB / Sonarr / Chaptarr unreachable (the hotspot drops connections now and then): say so, don't 500."""
    log.warning("upstream error on %s: %s", req.url.path, ex)
    if req.headers.get("x-requested-with") == "fetch" or req.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "A service didn't answer — try again in a moment."}, status_code=503)
    return tpl.TemplateResponse(req, "error.html", {}, status_code=503)


# ---------------- auth: same idea as Tunarr — sign-in page + long-lived cookie ----------------
PUBLIC = ("/login", "/static/", "/assets/", "/manifest.json", "/lists/")


def _basic_ok(req):
    h = req.headers.get("authorization", "")
    want = "Basic " + base64.b64encode(f"{config.USER}:{config.PASSWORD}".encode()).decode()
    return hmac.compare_digest(h, want)


@app.middleware("http")
async def auth(req: Request, call_next):
    p = req.url.path
    if p.startswith(PUBLIC):
        r = await call_next(req)
        if p.startswith("/static/") and "v" in req.query_params and r.status_code == 200:
            r.headers["Cache-Control"] = IMMUTABLE        # url changes with the content (asset())
        return r
    if hmac.compare_digest(req.cookies.get("discover", ""), config.SESSION):
        return await call_next(req)
    if p in ("/api/summary", "/api/tonight", "/api/radar") and _basic_ok(req):       # Homepage tiles only
        return await call_next(req)
    if req.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"error": "signed out"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(req: Request):
    return tpl.TemplateResponse(req, "login.html", {"err": ""})


@app.post("/login")
async def login(req: Request):
    f = urllib.parse.parse_qs((await req.body()).decode())
    ok = (hmac.compare_digest(f.get("user", [""])[0].strip().lower(), config.USER)
          & hmac.compare_digest(f.get("pw", [""])[0], config.PASSWORD)) and bool(config.PASSWORD)
    if not ok:
        await asyncio.sleep(1)                            # slow down guessing
        return tpl.TemplateResponse(req, "login.html", {"err": "Wrong username or password"}, status_code=401)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("discover", config.SESSION, max_age=31536000, httponly=True, samesite="lax")
    return r


@app.get("/logout")
async def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("discover")
    return r


@app.get("/manifest.json")
async def manifest():
    return {"name": "Discover", "short_name": "Discover", "start_url": "/", "display": "standalone",
            "background_color": "#0a0b0f", "theme_color": "#0a0b0f",
            "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}


# ---------------- pages ----------------
def cards_for(picks):
    """Pick rows → card dicts (item metadata + pick + your feedback/wants), enriched by features."""
    items = store.items_get(p["key"] for p in picks)
    fb, wants = store.feedback(), store.wants()
    out = [{**items[p["key"]], **p, "fb": fb.get(p["key"]), "wanted": p["key"] in wants}
           for p in picks if p["key"] in items and fb.get(p["key"]) != "dislike"]
    for enrich in features.CARD_DATA:
        enrich(out)
    return out


def render_page(req, tab, cards, template="page.html", **extra):
    """Every full page: the shell (page.html) needs tab, status, query and the Wanted count."""
    return tpl.TemplateResponse(req, template, {"tab": tab, "title": TITLES.get(tab), "cards": cards,
                                                "status": _status(), "q": req.query_params,
                                                "wanted_n": len(store.wants()), **extra})


def _status():
    last = store.meta_get("last_refresh")
    return {"running": pipeline.state["running"], "step": pipeline.state["step"], "error": pipeline.state["error"],
            "last": datetime.datetime.fromtimestamp(last).strftime("%d %b, %H:%M") if last else None,
            "setup": [n for n, v in (("TMDB_KEY", config.TMDB_KEY), ("JELLYFIN_KEY", config.JELLYFIN_KEY),
                                     ("GEMINI_KEY", config.GEMINI_KEY), ("TRAKT_CLIENT_ID", config.TRAKT_CLIENT_ID))
                      if not v]}


@app.get("/")
async def home():
    return RedirectResponse("/movies", status_code=303)


@app.get("/card/{key}", response_class=HTMLResponse)
async def card(req: Request, key: str):
    it = store.items_get([key]).get(key)
    if not it:
        return Response(status_code=404)
    pick = next((p for p in store.picks(it["kind"]) if p["key"] == key), {"key": key, "why": "", "because": []})
    return tpl.TemplateResponse(req, "_card.html", {"c": (cards_for([pick]) or [{**it, **pick}])[0]})


# ---------------- actions (called from the page with fetch) ----------------
async def _sync(kind):
    http = clients.Http()
    try:
        await clients.arr_sync_discover_list(http, "radarr" if kind == "movie" else "sonarr")
    except Exception as ex:
        log.warning("list sync: %s", ex)
    finally:
        await http.close()


@app.post("/act/{action}/{key}")
async def act(action: str, key: str, bg: BackgroundTasks):
    kind = key.split(":")[0]
    if action == "want":
        store.add_want(key)
        bg.add_task(_sync, kind)                          # Sonarr/Radarr pick it up right away
    elif action == "unwant":
        store.remove_want(key)
    elif action in ("like", "dislike"):
        store.set_feedback(key, None if store.feedback().get(key) == action else action)
    elif action == "undo":
        store.set_feedback(key, None)
    else:
        return Response(status_code=404)
    return {"ok": True}


@app.post("/refresh")
async def refresh(bg: BackgroundTasks):
    if not pipeline.state["running"]:
        bg.add_task(pipeline.refresh)
    return {"ok": True}


@app.get("/api/status")
async def status():
    return _status()


@app.get("/api/summary")
async def summary():
    s = _status()
    return {"movies": len(store.picks("movie")), "shows": len(store.picks("tv")), "wanted": len(store.wants()),
            "updated": "Refreshing…" if s["running"] else (s["last"] or "Never")}


for r in features.ROUTERS:          # feature routes first, so the catch-all tab route below can't shadow them
    app.include_router(r)


@app.get("/{tab}", response_class=HTMLResponse)
async def page(req: Request, tab: str):
    if tab == "wanted":
        wants = store.wants()
        picks = [{"key": k, "score": 0, "why": "", "because": []} for k in sorted(wants, key=lambda k: -wants[k])]
        return render_page(req, tab, cards_for(picks))
    if tab not in KINDS:
        return Response(status_code=404)
    cards = cards_for(store.picks(KINDS[tab]))
    for f in features.FILTERS:
        cards = f(cards, req.query_params)
    return render_page(req, tab, cards)
