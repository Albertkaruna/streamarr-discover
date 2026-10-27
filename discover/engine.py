"""Taste model + ranking. Pure functions (no network) so they're easy to test and tune.

Pipeline per media kind:
  seeds (what you watched, weighted by how much you liked it)
  → candidates (TMDB recommended/similar, Trakt related, your favourite creators' other work)
  → score = collaborative support + content match + meaning match (embeddings) + quality
  → MMR re-rank so the list isn't ten versions of the same thing.
"""
import datetime
import math

# ---------- how much a watched title says about your taste ----------


def _days_since(iso, now):
    if not iso:
        return 365
    try:  # Jellyfin sends 7-digit fractions ("…00.1234567Z"); seconds precision is plenty
        d = datetime.datetime.fromisoformat(iso[:19]).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return 365
    return max(0, (now - d).days)


def seed_weight(row, now, feedback=None):
    """Jellyfin row → weight in roughly [-1, 2]. Negative = you bailed on it."""
    if feedback == "dislike":
        return -1.0
    w = 0.0
    if row["kind"] == "movie":
        if row["played"]:
            w = 1.0
        elif row["percent"] >= 50:
            w = row["percent"] / 100
        elif 0 < row["percent"] < 25 and _days_since(row["last"], now) > 14:
            w = -0.3                                   # started, gave up
    else:
        eps, seen = row.get("episodes") or 0, row.get("watched") or 0
        if seen:
            c = seen / eps if eps else 0
            w = 0.3 + 0.9 * c                          # finished a series = strong signal
            if c < 0.15 and eps >= 8 and _days_since(row["last"], now) > 30:
                w = -0.3
    if w == 0:
        # In your library but no watch data (you often copy files to the phone, so Jellyfin never sees the
        # watch). You chose to download and keep it → moderate taste signal; real watches still dominate.
        w = 0.4
    if row["favorite"]:
        w = max(w, 0) + 0.8
    if row["playcount"] > 1:
        w += 0.3 * min(row["playcount"] - 1, 3)        # rewatches
    if feedback == "like":
        w = max(w, 0) + 0.8
    if w > 0:                                          # recent taste counts more (watched, else added)
        w *= 0.55 + 0.45 * math.exp(-_days_since(row["last"] or row.get("added"), now) / 365)
    return round(w, 3)


# ---------- content features ----------
ROLE_W = {"director": 1.5, "creator": 1.5, "writer": 1.0, "cast": 0.5}


def features(item):
    f = {f"g:{g}": 1.0 for g in item.get("genres", [])}
    f.update({f"k:{k}": 1.0 for k in item.get("keywords", [])})
    for role, name in item.get("people", []):
        f[f"p:{name}"] = max(f.get(f"p:{name}", 0), ROLE_W.get(role, 0.5))
    if item.get("lang"):
        f[f"l:{item['lang']}"] = 0.7
    if item.get("year"):
        f[f"d:{item['year'][:3]}0s"] = 0.3
    return f


def idf(items):
    df = {}
    for it in items:
        for k in features(it):
            df[k] = df.get(k, 0) + 1
    n = len(items)
    return {k: math.log((n + 1) / (v + 1)) + 1 for k, v in df.items()}


def profile(seeds, idf_):
    """Weighted sum of seed features (tf-idf). seeds: [(item, weight)]."""
    p = {}
    for item, w in seeds:
        for k, v in features(item).items():
            p[k] = p.get(k, 0) + w * v * idf_.get(k, 1)
    return p


def _norm(d):
    return math.sqrt(sum(v * v for v in d.values())) or 1.0


def sparse_cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0) for k, v in a.items()) / (_norm(a) * _norm(b))


def cos(a, b):
    # sumprod runs in C: ~30x faster than a generator, and the ranking thread then barely holds the GIL
    # (pure-Python sums here made page loads wait seconds during a refresh)
    if len(a) != len(b):
        return 0.0
    return math.sumprod(a, b) / ((math.sqrt(math.sumprod(a, a)) * math.sqrt(math.sumprod(b, b))) or 1.0)


def quality(item):
    """Bayesian rating: few votes pull toward the average, so a 9.0 from 12 people doesn't win."""
    v, r, m, c = item.get("votes", 0), item.get("rating", 0), 300, 6.6
    q = (v * r + m * c) / (v + m)
    return min(1.0, max(0.0, (q - 5.0) / 3.5))


def _minmax(xs):
    lo, hi = min(xs, default=0), max(xs, default=0)
    return [(x - lo) / (hi - lo) if hi > lo else 0.5 for x in xs]


# ---------- ranking ----------
# Component weights per mode. Module-level so the taste feature can nudge them from what you actually want;
# DEFAULT_WEIGHTS stays the anchor (tuning is bounded around it).
DEFAULT_WEIGHTS = {"emb": {"support": 0.35, "content": 0.25, "meaning": 0.25, "quality": 0.15},
                   "plain": {"support": 0.45, "content": 0.35, "quality": 0.20}}
WEIGHTS = {m: dict(w) for m, w in DEFAULT_WEIGHTS.items()}


def rank(cands, support, because, pos_seeds, neg_seeds, vecs, limit=60):
    """cands: [item]; support: key→float; because: key→[seed titles];
    pos_seeds/neg_seeds: [(item, w)]; vecs: key→embedding (may be empty)."""
    if not cands:
        return []
    idf_ = idf([c for c in cands] + [s for s, _ in pos_seeds + neg_seeds])
    p_pos = profile(pos_seeds, idf_)
    p_neg = profile([(s, -w) for s, w in neg_seeds], idf_)
    seed_vecs = [(vecs[s["key"]], w) for s, w in sorted(pos_seeds, key=lambda x: -x[1])[:40] if s["key"] in vecs]
    neg_vecs = [vecs[s["key"]] for s, _ in neg_seeds if s["key"] in vecs]
    max_sup = max(support.values(), default=1) or 1

    sup, con, emb = [], [], []
    for c in cands:
        sup.append(math.log1p(support.get(c["key"], 0)) / math.log1p(max_sup))
        fc = {k: v * idf_.get(k, 1) for k, v in features(c).items()}
        con.append(sparse_cos(fc, p_pos) - (0.5 * sparse_cos(fc, p_neg) if p_neg else 0))
        v = vecs.get(c["key"])
        if v and seed_vecs:
            sims = sorted((cos(v, sv) for sv, _ in seed_vecs), reverse=True)[:3]
            e = sum(sims) / len(sims)
            if neg_vecs:
                e -= 0.5 * max(0, max(cos(v, nv) for nv in neg_vecs) - e)
            emb.append(e)
        else:
            emb.append(None)

    has_emb = any(e is not None for e in emb)
    con_n = _minmax(con)
    emb_vals = [e for e in emb if e is not None]
    lo, hi = min(emb_vals, default=0), max(emb_vals, default=0)
    scored = []
    for i, c in enumerate(cands):
        q = quality(c)
        if has_emb:
            e = (emb[i] - lo) / (hi - lo) if emb[i] is not None and hi > lo else 0.3
            w = WEIGHTS["emb"]
            s = w["support"] * sup[i] + w["content"] * con_n[i] + w["meaning"] * e + w["quality"] * q
        else:
            w = WEIGHTS["plain"]
            s = w["support"] * sup[i] + w["content"] * con_n[i] + w["quality"] * q
        scored.append({"key": c["key"], "score": round(s, 4), "because": because.get(c["key"], [])[:3],
                       "extra": {"parts": {"support": round(sup[i], 3), "content": round(con_n[i], 3),
                                           "meaning": round(e, 3) if has_emb else None, "quality": round(q, 3)}}})
    scored.sort(key=lambda x: -x["score"])
    return mmr(scored, {c["key"]: c for c in cands}, vecs, limit)


def _sim(a, b, vecs):
    if a["key"] in vecs and b["key"] in vecs:
        return cos(vecs[a["key"]], vecs[b["key"]])
    fa = set(a.get("genres", [])) | set(a.get("keywords", []))
    fb = set(b.get("genres", [])) | set(b.get("keywords", []))
    return len(fa & fb) / (len(fa | fb) or 1)


def mmr(scored, items, vecs, limit, lam=0.75):
    """Maximal Marginal Relevance: each next pick trades score against similarity to picks so far."""
    pool, out = scored[: limit * 3], []
    pen = [0] * len(pool)          # max similarity to any pick so far — only the newest pick needs comparing each round
    while pool and len(out) < limit:
        if out:
            last = items[out[-1]["key"]]
            pen = [max(p, _sim(items[s["key"]], last, vecs)) for p, s in zip(pen, pool)]
        best = max(range(len(pool)), key=lambda i: (lam * pool[i]["score"] - (1 - lam) * pen[i], -i))
        pen.pop(best)
        out.append(pool.pop(best))
    return out


# ---------- self-tuning ----------


def _bounded_norm(raw, base, bound):
    """Sum to 1 while each weight stays within ±bound of its default (alternate clamp/normalise; defaults are
    feasible so this settles in a few rounds)."""
    w = dict(raw)
    for _ in range(50):
        t = sum(w.values()) or 1
        w = {c: min(base[c] * (1 + bound), max(base[c] * (1 - bound), v / t)) for c, v in w.items()}
    t = sum(w.values()) or 1
    return {c: round(v / t, 4) for c, v in w.items()}


def tune_weights(events, defaults=None, min_labels=15, gain=2.0, bound=0.5):
    """events: [(label, parts)] with label +1 (wanted/liked), -1 (disliked), 0 (shown, ignored).
    Components whose values separate positives from the rest get more weight, ones that don't get less.
    Returns (weights per mode, labelled count); defaults until there are min_labels labelled events."""
    defaults = defaults or DEFAULT_WEIGHTS
    labelled = sum(1 for lab, _ in events if lab)
    if labelled < min_labels or not any(lab > 0 for lab, _ in events):
        return {m: dict(w) for m, w in defaults.items()}, labelled
    pos = [p for lab, p in events if lab > 0]
    neg = [p for lab, p in events if lab <= 0]
    out = {}
    for mode, base in defaults.items():
        raw = {}
        for c, d in base.items():
            pv = [p[c] for p in pos if p.get(c) is not None]
            nv = [p[c] for p in neg if p.get(c) is not None]
            sep = sum(pv) / len(pv) - sum(nv) / len(nv) if len(pv) >= 3 and len(nv) >= 3 else 0.0
            raw[c] = d * (1 + max(-bound, min(bound, gain * sep)))
        out[mode] = _bounded_norm(raw, base, bound)
    return out, labelled


def lots_quota(ranked, lang_of, lots, share=0.3, top=40):
    """'Lots' languages (Settings) get at least `share` of the top `top` picks: their best-ranked titles are pulled up
    and interleaved evenly. Without this a mostly-English library buries them below the cut-off."""
    if not lots:
        return ranked
    want = int(share * top)
    have = [r for r in ranked[:top] if lang_of(r) in lots]
    if len(have) >= want:
        return ranked
    extra = [r for r in ranked[top:] if lang_of(r) in lots][:want - len(have)]
    if not extra:
        return ranked
    pick = set(id(r) for r in extra)
    rest = [r for r in ranked if id(r) not in pick]
    head, step = rest[:top - len(extra)], max(1, (top - len(extra)) // (len(extra) + 1))
    for i, r in enumerate(extra):                   # every `step` places, starting near the top
        head.insert(min(len(head), (i + 1) * step + i), r)
    return head + rest[top - len(extra):]
