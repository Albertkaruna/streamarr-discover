"""Feature registry. Each feature is one module in this package that registers what it adds:

    from discover import features as fx
    fx.router.get(...)                       # or: fx.ROUTERS.append(my_router)
    fx.REFRESH_HOOKS.append(async fn(ctx))   # per kind, after ranking, before picks are saved; may edit ctx["ranked"]
    fx.POST_REFRESH.append(async fn(ctx))    # once after both kinds; ctx has http, items (all hydrated), seeds_w
    fx.FILTERS.append(fn(cards, query) -> cards)        # Movies/Shows page filtering (query = request.query_params)
    fx.TABS.append(("/path", "Label", "emoji"))          # extra main tab (keep these few — phone bottom bar)
    fx.NAV.append(("/path", "Label"))                    # small link in the top bar (settings-like pages)
    fx.SEED_HOOKS.append(fn(seeds_w, info))              # adjust taste weights {key: w} before candidates are
                                                         # gathered; info: lib (Jellyfin rows), fb, owned, jf_keys
    fx.CARD_SLOTS.append("features/x_slot.html")         # included inside each card body (variable: c)
    fx.CARD_ACTIONS.append("features/x_act.html")        # included in each card's button row (variable: c)
    fx.PAGE_TOP.append("features/x_top.html")            # included above the grid on Movies/Shows (tab, cards, q)
    fx.CSS.append("/static/x.css"); fx.JS.append("/static/x.js")
    fx.CARD_DATA.append(fn(cards) -> None)               # enrich card dicts in place before render

Modules are imported by load() in alphabetical order; keep features independent of each other.
"""
import importlib
import pkgutil

from fastapi import APIRouter

ROUTERS, REFRESH_HOOKS, POST_REFRESH, FILTERS, SEED_HOOKS, NAV = [], [], [], [], [], []
TABS, CARD_SLOTS, CARD_ACTIONS, PAGE_TOP, CSS, JS, CARD_DATA = [], [], [], [], [], [], []
router = APIRouter()
ROUTERS.append(router)
_loaded = False


def load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not m.name.startswith("_"):
            importlib.import_module(f"{__name__}.{m.name}")
