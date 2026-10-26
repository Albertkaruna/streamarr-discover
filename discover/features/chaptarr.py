"""Book wants → Chaptarr (Readarr fork, /api/v1). Ebooks go to the "Books" root folder, audiobooks to "Audiobooks";
the author is added unmonitored so only the wanted book downloads, not their whole bibliography."""
import asyncio
import logging
import os
import re
import urllib.parse

from discover import features as fx

log = logging.getLogger("discover")
URL = os.environ.get("CHAPTARR_URL", "http://chaptarr:8789")
KEY = os.environ.get("CHAPTARR_KEY", "")
MEDIA = ("ebook", "audiobook")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def best_match(results, title, author):
    """Lookup results → the one whose title (and author, if we know it) matches. Pure."""
    t, a = _norm(title), _norm(author)
    def score(b):
        bt, ba = _norm(b.get("title")), _norm((b.get("author") or {}).get("authorName") or b.get("authorTitle"))
        s = 3 if bt == t else 1 if t and (t in bt or bt in t) else 0
        if re.search(r"\b(summary|analysis|study guide|workbook|sparknotes|cliffsnotes)\b", bt):
            s -= 3                                   # not the book itself
        if a and ba:
            s += 2 if a == ba else 1 if set(a.split()) & set(ba.split()) else -2
        return s
    good = [b for b in results or [] if _norm(b.get("title")) and t and
            (t in _norm(b.get("title")) or _norm(b.get("title")) in t) and score(b) >= 2]   # title must match
    return max(good, key=score) if good else None


async def _api(http, path, method="GET", body=None):
    return await http.req(method, f"{URL}/api/v1{path}", headers={"X-Api-Key": KEY}, json_body=body, conc=2, tries=2)


async def _folders(http):
    """{media: (root path, quality profile, metadata profile)} from Chaptarr's root folders (folderType 1=audio, 2=ebook)."""
    out = {}
    for r in await _api(http, "/rootfolder"):
        if r.get("folderType") == 1:
            out["audiobook"] = (r["path"], r.get("audiobookQualityProfileId"), r.get("audiobookMetadataProfileId"))
        elif r.get("folderType") == 2:
            out["ebook"] = (r["path"], r.get("ebookQualityProfileId"), r.get("ebookMetadataProfileId"))
    return out


async def send(http, want, media):
    """Add a wanted book to Chaptarr and start its search. Returns Chaptarr book ids. Chaptarr keeps the ebook and the
    audiobook as separate entries, so "both" is two adds."""
    if not KEY:
        return []
    folders = await _folders(http)
    ids = []
    for m in (MEDIA if media == "both" else [media]):
        if m in folders and (bid := await _send_one(http, want, m, folders)):
            ids.append(bid)
    return ids


async def _send_one(http, want, m, folders):
    b = None                     # "title author" often returns study guides/summaries; the bare title finds the book
    for term in dict.fromkeys([f"{want.get('title', '')} {want.get('author') or ''}".strip(), want.get("title", "")]):
        b = best_match(await _api(http, f"/book/lookup?term={urllib.parse.quote(term)}"), want.get("title"), want.get("author"))
        if b:
            break
    if not b:
        log.info("chaptarr: no match for a wanted book")
        return None
    root, qp, mp = folders[m]
    b["author"].update(monitored=False, qualityProfileId=qp, metadataProfileId=mp, rootFolderPath=root,
                       addOptions={"monitor": "none", "searchForMissingBooks": False},
                       **{f"{m}RootFolderPath": root, f"{m}QualityProfileId": qp, f"{m}MetadataProfileId": mp})
    b.update(monitored=True, mediaType=m, ebookMonitored=m == "ebook", audiobookMonitored=m == "audiobook",
             addOptions={"searchForNewBook": True, "addType": "manual"})
    for attempt in range(4):     # 2nd media of "both": Chaptarr 500s ("Expected 1 rows") while the new author settles
        try:
            out = await _api(http, "/book", "POST", b)
            return out.get("id") if out else None
        except Exception as ex:
            if attempt == 3:
                raise
            log.info("chaptarr add retry %d: %s", attempt + 1, ex)
            await asyncio.sleep(5 * (attempt + 1))


async def unmonitor(http, book_ids):
    ids = book_ids if isinstance(book_ids, list) else [book_ids]     # older wants stored a single id
    if KEY and ids:
        await _api(http, "/book/monitor", "PUT", {"bookIds": ids, "monitored": False})


fx.chaptarr_ready = bool(KEY)
