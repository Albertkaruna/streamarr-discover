"""Settings from environment (docker-compose passes them from ~/servarr/.env)."""
import hashlib
import hmac
import os
from pathlib import Path


def env(name, default=""):
    return os.environ.get(name, default).strip()


DATA = Path(env("DISCOVER_DATA", "/data"))
PASSWORD = env("DISCOVER_PASSWORD")
USER = env("DISCOVER_USER", "karuna")

JELLYFIN_URL = env("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_KEY = env("JELLYFIN_KEY")
JELLYFIN_USER = env("JELLYFIN_USER", "karuna")

SONARR_URL = env("SONARR_URL", "http://sonarr:8989")
SONARR_KEY = env("SONARR_KEY")
RADARR_URL = env("RADARR_URL", "http://radarr:7878")
RADARR_KEY = env("RADARR_KEY")

TMDB_KEY = env("TMDB_KEY")              # v3 API key or v4 read token — both work
TRAKT_CLIENT_ID = env("TRAKT_CLIENT_ID")  # public endpoints only, no user login
GEMINI_KEY = env("GEMINI_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.5-flash-lite")   # 3.8 Flash is 429 on the free tier
GEMINI_FALLBACK = env("GEMINI_FALLBACK", "gemini-3.8-flash")  # if the main one is over quota / unavailable
GEMINI_EMBED_MODEL = env("GEMINI_EMBED_MODEL", "gemini-embedding-001")

REFRESH_HOUR = int(env("DISCOVER_REFRESH_HOUR", "5"))   # daily refresh, local time
SELF_URL = env("DISCOVER_SELF_URL", "http://discover:5050")  # how Sonarr/Radarr reach us

# Derived secrets: survive restarts, all change when the password changes.
SESSION = hmac.new(PASSWORD.encode(), b"discover-session", hashlib.sha256).hexdigest()
LIST_KEY = hmac.new(PASSWORD.encode(), b"discover-lists", hashlib.sha256).hexdigest()[:32]
