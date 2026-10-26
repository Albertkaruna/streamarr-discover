"""Gemini: meaning-fingerprints for titles, and one daily re-rank that also writes "why you'll like it"."""
import logging

from . import clients, config, store

log = logging.getLogger("discover")


def embed_text(it):
    return (f"{it['title']} ({it.get('year', '')}). Genres: {', '.join(it.get('genres', []))}. "
            f"Themes: {', '.join(it.get('keywords', [])[:15])}. {it.get('overview', '')[:700]}")


async def ensure_embeddings(http, items):
    """Embed titles we haven't seen before; cached forever. Never fatal — ranking works without."""
    if not config.GEMINI_KEY:
        return {}
    model = config.GEMINI_EMBED_MODEL
    have = store.embeddings_get([i["key"] for i in items], model)
    todo = [i for i in items if i["key"] not in have]
    for b in range(0, len(todo), 100):
        batch = todo[b:b + 100]
        try:
            vecs = await clients.gemini_embed(http, [embed_text(i) for i in batch])
        except Exception as ex:  # quota, network: use what we have
            log.warning("embeddings stopped after %d new: %s", b, ex)
            break
        for it, v in zip(batch, vecs):
            store.embedding_put(it["key"], model, v)
            have[it["key"]] = v
    return have


def _line(it):
    return f"{it['title']} ({it.get('year', '')}) — {', '.join(it.get('genres', [])[:3])}"


async def rerank(http, kind, ranked, items, pos_seeds, neg_seeds, top=40):
    """Ask Gemini to reorder the top picks by fit and explain each. Returns ranked list with 'why' filled.
    Final order blends engine position and Gemini position, so one odd answer can't wreck the list."""
    head, tail = ranked[:top], ranked[top:]
    for r in ranked:
        r["why"] = ""
    if not (config.GEMINI_KEY and head):
        return ranked
    loved = "\n".join(f"- {_line(s)}" for s, _ in sorted(pos_seeds, key=lambda x: -x[1])[:25])
    disliked = "\n".join(f"- {_line(s)}" for s, _ in neg_seeds[:10]) or "- (none)"
    cands = "\n".join(f"{r['key']} | {_line(items[r['key']])} | {items[r['key']].get('overview', '')[:220]}"
                      for r in head)
    what = "movies" if kind == "movie" else "TV series"
    prompt = f"""You are a sharp, honest curator of {what} for one person.

Titles they loved (strongest first):
{loved}

Titles they disliked or abandoned:
{disliked}

Candidates (id | title (year) — genres | plot):
{cands}

Order ALL candidates from best to worst fit for this person. Judge tone, themes, pacing, storytelling style and
emotional feel — not just genre. For each, write "why": max 20 words, concrete, naming 1-2 of their loved titles
when it helps, no spoilers, no hype words ("masterpiece", "must-watch").
Return JSON: {{"picks": [{{"id": "<id>", "why": "<reason>"}}]}} with every candidate id exactly once."""
    try:
        out = (await clients.gemini_json(http, prompt)).get("picks", [])
    except Exception as ex:
        log.warning("gemini rerank (%s) failed: %s", kind, ex)
        return ranked
    def key(p):  # lighter models drop the "movie:" prefix → accept "123" as well as "movie:123"
        k = str(p.get("id", "")).strip() if isinstance(p, dict) else ""
        return k if k in items else f"{kind}:{k.rsplit(':', 1)[-1]}"
    out = [p for p in out if isinstance(p, dict) and key(p) in items]
    llm_pos = {key(p): i for i, p in enumerate(out)}
    why = {key(p): str(p.get("why", ""))[:200] for p in out}
    for i, r in enumerate(head):
        r["why"] = why.get(r["key"], "")
        r["_pos"] = 0.5 * i + 0.5 * llm_pos.get(r["key"], i)
    head.sort(key=lambda r: r.pop("_pos"))
    return head + tail
