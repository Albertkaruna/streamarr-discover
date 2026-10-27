"""SQLite: API cache, title metadata, embeddings, your feedback, wants, and the latest picks."""
import json
import sqlite3
import struct
import threading
import time

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, body TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS items (key TEXT PRIMARY KEY, data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, model TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS feedback (key TEXT PRIMARY KEY, signal TEXT, ts REAL);   -- like | dislike
CREATE TABLE IF NOT EXISTS wants (key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS picks (kind TEXT, pos INTEGER, key TEXT, score REAL, why TEXT, because TEXT,
                                  extra TEXT DEFAULT '{}', PRIMARY KEY (kind, pos));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    con = getattr(_local, "con", None)
    if con is None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(config.DATA / "discover.db", timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")      # WAL-safe; no fsync per commit (refresh writes hundreds, on the event loop)
        con.executescript(SCHEMA)
        if "extra" not in [r[1] for r in con.execute("PRAGMA table_info(picks)")]:   # pre-features databases
            con.execute("ALTER TABLE picks ADD COLUMN extra TEXT DEFAULT '{}'")
        _local.con = con
    return con


def cache_get(url, max_age):
    row = db().execute("SELECT body, ts FROM cache WHERE url=?", (url,)).fetchone()
    return json.loads(row[0]) if row and time.time() - row[1] < max_age else None


def cache_put(url, body):
    with db() as c:
        c.execute("REPLACE INTO cache VALUES (?,?,?)", (url, json.dumps(body), time.time()))


def item_put(key, data):
    with db() as c:
        c.execute("REPLACE INTO items VALUES (?,?,?)", (key, json.dumps(data), time.time()))


def items_get(keys):
    keys = list(keys)
    out = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = "SELECT key, data FROM items WHERE key IN (%s)" % ",".join("?" * len(chunk))
        out.update((k, json.loads(d)) for k, d in db().execute(q, chunk))
    return out


def vec_pack(v):
    return struct.pack(f"{len(v)}f", *v)


def embeddings_get(keys, model):
    keys = list(keys)
    out = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = "SELECT key, vec FROM embeddings WHERE model=? AND key IN (%s)" % ",".join("?" * len(chunk))
        for k, b in db().execute(q, [model, *chunk]):
            out[k] = list(struct.unpack(f"{len(b) // 4}f", b))
    return out


def embedding_put(key, model, vec):
    with db() as c:
        c.execute("REPLACE INTO embeddings VALUES (?,?,?)", (key, model, vec_pack(vec)))


def feedback():
    return dict(db().execute("SELECT key, signal FROM feedback"))


def set_feedback(key, signal):
    with db() as c:
        if signal:
            c.execute("REPLACE INTO feedback VALUES (?,?,?)", (key, signal, time.time()))
        else:
            c.execute("DELETE FROM feedback WHERE key=?", (key,))


def wants():
    return dict(db().execute("SELECT key, ts FROM wants"))


def add_want(key):
    with db() as c:
        c.execute("REPLACE INTO wants VALUES (?,?)", (key, time.time()))


def remove_want(key):
    with db() as c:
        c.execute("DELETE FROM wants WHERE key=?", (key,))


def save_picks(kind, picks):
    with db() as c:
        c.execute("DELETE FROM picks WHERE kind=?", (kind,))
        c.executemany("INSERT INTO picks VALUES (?,?,?,?,?,?,?)",
                      [(kind, i, p["key"], p["score"], p.get("why", ""), json.dumps(p.get("because", [])),
                        json.dumps(p.get("extra", {}))) for i, p in enumerate(picks)])


def picks(kind):
    rows = db().execute("SELECT key, score, why, because, extra FROM picks WHERE kind=? ORDER BY pos", (kind,)).fetchall()
    return [{"key": k, "score": s, "why": w, "because": json.loads(b), "extra": json.loads(x or "{}")}
            for k, s, w, b, x in rows]


def meta_get(k, default=None):
    row = db().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def pref(k, default=None):
    """User preferences (language mix, moods, …) — stored in meta as pref:<k>."""
    return meta_get(f"pref:{k}", default)


def set_pref(k, v):
    meta_put(f"pref:{k}", v)


def meta_put(k, v):
    with db() as c:
        c.execute("REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))
