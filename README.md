# Discover · by Streamarr

Personal movie & TV recommendations for the Streamarr home server — learned from what you actually watch,
with one-tap **+ Want** that flows straight into Sonarr/Radarr. Songs and books come next.

## How it decides

1. **Your taste (seeds).** Every movie/series in Jellyfin gets a weight from how you watched it:
   finished ≫ half-watched; a finished series beats sampling two episodes; favourites and rewatches add;
   bailing early counts *against*; older watches fade (1-year half-life-ish). Things you asked Sonarr/Radarr
   for, your **+ Want**s and your 👍 count as interest; ✕ counts as dislike.
2. **Candidates.** For your 40 strongest seeds: TMDB *recommended* (people who liked X liked Y), TMDB *similar*,
   Trakt *related* (public data, no login), plus the other work of directors/creators you keep coming back to.
   A title suggested by several of your favourites collects more **support**.
3. **Score** (per kind):
   - **support** — log-scaled, from step 2
   - **content match** — tf-idf cosine of genres, TMDB keywords, directors/creators/writers/cast, language, decade
     vs. your weighted profile (minus a dislike profile)
   - **meaning match** — Gemini embeddings of plot + themes; mean of the 3 closest seeds (so you can have
     several tastes at once), pushed down when close to something you disliked
   - **quality** — Bayesian rating (a 9.2 from 15 votes doesn't beat an 8.4 from 20k)
   - `0.35 support + 0.25 content + 0.25 meaning + 0.15 quality` (no embeddings → `0.45/0.35/0.20`)
4. **Variety.** MMR re-rank: each next pick trades score against similarity to picks already shown.
5. **Gemini re-rank.** One call per kind per day: the top 40 + your loved/disliked lists → reordered by fit
   (tone, pacing, themes) with a one-line *why*. Final order = 50/50 blend with the engine, so one odd answer
   can't wreck the list. Without a Gemini key everything still works, reasons fall back to "Because you watched…".

Excluded: anything you own, already asked for, wanted, or ✕'d; unreleased; < 30 votes; adult.

## Features (one module each in `discover/features/`, plugged in via the registry in `features/__init__.py`)

| Where | What |
|---|---|
| Movies / Shows | genre, mood (Gemini-tagged), language, Short / Binge-able / New-to-me / Hidden-gems chips · Tonight's pick · "?" explain sheet · ▶ trailer · More like this · download status + size |
| + Want sheet | seasons (shows) · **Tamil audio if available** · 720p / 1080p → one of four Sonarr/Radarr lists (plain, Tamil, 720p, Tamil 720p) |
| New | new seasons of your shows (one-tap "Get season") · fresh work from directors/creators/actors you love |
| Books | Open Library + Gemini + "the book it was based on" · cross-media "Because you loved X…" (book, album, documentary) |
| Music | similar artists/songs (ListenBrainz, Deezer, iTunes previews) from Jellyfin music + Tunarr playlists · "+ Tunarr" |
| Rate · Report · Settings | rate your library (Loved/OK/Didn't like) · want-rate + self-tuning weights · language mix (Off/Some/Lots) |

Sonarr/Radarr setup for the Want options (idempotent, prints what it does): `python -m discover.features.want_arr [--apply]`
— run it inside the container so the live database learns which lists exist.

## + Want → download

Discover publishes two wishlist feeds that Sonarr/Radarr follow as import lists named **Streamarr Discover**:
`/lists/sonarr` (Sonarr *Custom List*, `tvdbId`) and `/lists/radarr` (Radarr *StevenLu Custom*, `imdb_id`),
protected by a key derived from the password. Tapping **+ Want** triggers that list's sync immediately.
Removing a want → Sonarr/Radarr unmonitor it on the next sync (files kept) — same rule as the Trakt list.

## Run

Part of the servarr compose (`~/servarr/docker-compose.yml`, service `discover`, port 5050), built from this repo.
Data (SQLite: cache, metadata, embeddings, feedback, wants, picks) in `~/servarr/config/discover/` — covered by
the weekly Streamarr backup. Refreshes ~2 min after start if stale, then daily at 05:00, or **↻ Refresh now**.

| env | what |
|---|---|
| `DISCOVER_PASSWORD` | sign-in password (user `karuna`) — also derives the session + list keys |
| `JELLYFIN_KEY` | Jellyfin API key (reads your history) |
| `TMDB_KEY` | themoviedb.org API key or read token — **required** |
| `TRAKT_CLIENT_ID` | Trakt app client id — optional, adds "related" |
| `GEMINI_KEY` | Google AI Studio key — optional, adds embeddings + re-rank + reasons |
| `SONARR_KEY`, `RADARR_KEY` | to skip what's already tracked and trigger instant list syncs |
| `GEMINI_MODEL` / `GEMINI_FALLBACK` / `GEMINI_EMBED_MODEL` | default `gemini-3.5-flash-lite` / `gemini-3.8-flash` (3.8 Flash is rate-limited on the free tier) / `gemini-embedding-001` |

Gemini free tier: ~2 generate calls + a few embedding batches per day (embeddings are cached forever).

## Develop

```
python -m tests          # engine tests, no extra packages
docker compose -f ~/servarr/docker-compose.yml up -d --build discover
```

Code: `engine.py` (taste + ranking, pure), `pipeline.py` (one refresh), `clients.py` (APIs + cache),
`ai.py` (Gemini), `app.py` (web), `store.py` (SQLite).

## Next

Songs (ListenBrainz/Last.fm similar artists + Tunarr playlists) · books (Hardcover) · cross-media picks
("loved *Dark* → this book, this soundtrack") · "More like this" from any title · language filters.
