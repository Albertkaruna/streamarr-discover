"""One refresh: read your history → build seeds → gather candidates → rank → Gemini re-rank → save picks."""
import asyncio
import datetime
import logging
import math
import time

from . import ai, clients, config, engine, features, store

log = logging.getLogger("discover")
state = {"running": False, "step": "", "error": ""}

SRC_RECS, SRC_SIMILAR, SRC_TRAKT, SRC_PEOPLE = 1.0, 0.5, 0.8, 0.4
MAX_SEEDS, MAX_CANDIDATES, MIN_VOTES = 60, 300, 30
SRC_LANG, LOTS_SEEDS, LOTS_CANDS, LOTS_MIN_VOTES = 0.3, 15, 40, 10   # "Lots" languages (Settings) get their own share


async def _resolve(http, row):
    """Jellyfin row → (kind, tmdb id), using IMDb/TVDB ids when TMDB's is missing."""
    if row["ids"].get("tmdb"):
        return row["kind"], int(row["ids"]["tmdb"])
    for src, field in (("imdb", "imdb_id"), ("tvdb", "tvdb_id")):
        if row["ids"].get(src):
            kind, tid = await clients.tmdb_find(http, row["ids"][src], field)
            if tid:
                return kind, tid
    return None, None


async def _gather(coros):
    """Run all, drop failures (one bad title shouldn't sink the refresh)."""
    res = await asyncio.gather(*coros, return_exceptions=True)
    for r in res:
        if isinstance(r, Exception):
            log.warning("skipped: %s", r)
    return [r for r in res if not isinstance(r, Exception)]


async def refresh():
    if state["running"]:
        return
    state.update(running=True, error="", step="Reading your Jellyfin history")
    t0 = time.time()
    http = clients.Http()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        fb, wants = store.feedback(), store.wants()
        lib = await clients.jellyfin_library(http)
        try:
            tracked = await clients.arr_wanted(http)
        except Exception as ex:
            log.warning("sonarr/radarr unavailable: %s", ex)
            tracked = {}

        state["step"] = "Learning your taste"
        resolved = await _gather([_resolve(http, r) for r in lib])
        seeds_w, owned = {}, set()
        for row, (kind, tid) in zip(lib, resolved):
            if tid:
                key = f"{kind}:{tid}"
                owned.add(key)
                seeds_w[key] = engine.seed_weight(row, now, fb.get(key))
        jf_keys = set(seeds_w)                               # these already include their 👍/👎
        for (kind, tid), has_file in tracked.items():
            key = f"{kind}:{tid}"
            owned.add(key)
            seeds_w.setdefault(key, 0.35)                   # you asked for it → mild interest
        for key in wants:
            seeds_w.setdefault(key, 0.5)
        for key, sig in fb.items():                          # 👍/👎 on recommendations
            if key not in jf_keys:
                seeds_w[key] = seeds_w.get(key, 0) + 0.8 if sig == "like" else -1.0
        store.meta_put("library", sorted(jf_keys))           # Jellyfin titles (for the Rate page etc.)
        for hook in features.SEED_HOOKS:
            try:
                hook(seeds_w, {"lib": lib, "fb": fb, "owned": owned, "jf_keys": jf_keys})
            except Exception:
                log.exception("seed hook %s failed", hook.__qualname__)
        seeds_w = {k: w for k, w in seeds_w.items() if w}
        seed_items = {i["key"]: i for i in await _gather(
            [clients.tmdb_item(http, *k.split(":")) for k in seeds_w])}
        exclude = owned | set(wants) | {k for k, s in fb.items() if s == "dislike"}

        # people you keep coming back to (directors/creators), across both kinds
        people = {}
        for k, w in seeds_w.items():
            if w > 0 and k in seed_items:
                for role, name in seed_items[k]["people"]:
                    if role in ("director", "creator"):
                        people[name] = people.get(name, 0) + w
        top_people = [n for n, w in sorted(people.items(), key=lambda x: -x[1])[:8] if w >= 1.5]
        person_works = dict(zip(top_people, await _gather([clients.tmdb_person_works(http, n) for n in top_people])))

        all_cands = {}
        for kind in ("movie", "tv"):
            state["step"] = f"Finding {'movies' if kind == 'movie' else 'shows'}"
            pos = sorted([(seed_items[k], w) for k, w in seeds_w.items()
                          if w > 0 and k in seed_items and k.startswith(kind)], key=lambda x: -x[1])
            neg = [(seed_items[k], w) for k, w in seeds_w.items() if w < 0 and k in seed_items and k.startswith(kind)]
            support, because = {}, {}

            def add(tid, w, seed_title, k=kind):
                key = f"{k}:{tid}"
                if key in exclude:
                    return
                support[key] = support.get(key, 0) + w
                if seed_title and seed_title not in because.setdefault(key, []):
                    because[key].append(seed_title)

            # "Lots" languages: a mostly-English library would otherwise crowd them out of the seeds and the pool
            lots = {g for g, v in (store.pref("languages") or {}).items() if v == "lots" and g != "other"}
            gen = pos[:MAX_SEEDS] + [x for x in pos[MAX_SEEDS:] if x[0].get("lang") in lots][:LOTS_SEEDS]
            related = await _gather([clients.trakt_related(http, kind, s.get("imdb")) for s, _ in gen])
            for (s, w), rel in zip(gen, related):
                for r, tid in enumerate(s["recs"][:20]):
                    add(tid, w * SRC_RECS / (1 + 0.08 * r), s["title"])
                for r, tid in enumerate(s["similar"][:12]):
                    add(tid, w * SRC_SIMILAR / (1 + 0.08 * r), s["title"])
                for r, tid in enumerate(rel):
                    add(tid, w * SRC_TRAKT / (1 + 0.08 * r), s["title"])
            for name, works in person_works.items():
                for k2, tid in works:
                    if k2 == kind:
                        add(tid, SRC_PEOPLE * math.log1p(people[name]), name)

            lang_keys = []                               # well-rated titles straight from TMDB in each "Lots" language
            for lang in sorted(lots):
                for page in (1, 2):
                    try:
                        r = await clients.tmdb(http, f"/discover/{kind}", cache=7 * 86400, with_original_language=lang,
                                               sort_by="vote_count.desc", page=page, **{"vote_average.gte": "6.5"})
                    except Exception as ex:
                        log.warning("lots-language discover %s: %s", lang, ex)
                        break
                    for r_, x in enumerate(r.get("results") or []):
                        add(x["id"], SRC_LANG / (1 + 0.05 * r_), None)
                        lang_keys.append(f"{kind}:{x['id']}")
            keys = sorted(support, key=lambda k: -support[k])[:MAX_CANDIDATES]
            keys += [k for k in dict.fromkeys(lang_keys) if k in support and k not in keys][:LOTS_CANDS]
            cands = [c for c in await _gather([clients.tmdb_item(http, kind, k.split(":")[1]) for k in keys])
                     if c["released"] and not c["adult"]
                     and c["votes"] >= (LOTS_MIN_VOTES if c.get("lang") in lots else MIN_VOTES)]   # Tamil films have few TMDB votes

            state["step"] = "Understanding plots (Gemini)"
            vecs = await ai.ensure_embeddings(http, cands + [s for s, _ in gen + neg])
            cand_by = {c["key"]: c for c in cands}
            ranked = await asyncio.to_thread(engine.rank, cands, support, because, pos, neg, vecs, limit=90)   # CPU-heavy: keep pages responsive; wider, trimmed to 60 below
            ranked = engine.lots_quota(ranked, lambda r: (cand_by.get(r["key"]) or {}).get("lang"), lots)
            state["step"] = "Picking the best (Gemini)"
            ranked = await ai.rerank(http, kind, ranked, {c["key"]: c for c in cands}, pos, neg)
            ctx = {"http": http, "kind": kind, "ranked": ranked, "cands": {c["key"]: c for c in cands},
                   "pos": pos, "neg": neg, "vecs": vecs, "support": support, "because": because,
                   "seeds_w": seeds_w, "seed_items": seed_items, "exclude": exclude, "owned": owned}
            for hook in features.REFRESH_HOOKS:
                try:
                    await hook(ctx)
                except Exception:
                    log.exception("refresh hook %s failed", hook.__qualname__)
            all_cands.update(ctx["cands"])
            store.save_picks(kind, ctx["ranked"][:60])

        post = {"http": http, "items": {**seed_items, **all_cands}, "seeds_w": seeds_w, "owned": owned,
                "exclude": exclude, "people": people}
        for hook in features.POST_REFRESH:
            state["step"] = "Extras"
            try:
                await hook(post)
            except Exception:
                log.exception("post-refresh hook %s failed", hook.__qualname__)
        store.meta_put("last_refresh", time.time())
        store.meta_put("stats", {"seeds": sum(w > 0 for w in seeds_w.values()), "seconds": round(time.time() - t0)})
        log.info("refresh done in %ds", time.time() - t0)
    except Exception as ex:
        log.exception("refresh failed")
        state["error"] = str(ex)[:300]
    finally:
        state.update(running=False, step="")
        await http.close()
