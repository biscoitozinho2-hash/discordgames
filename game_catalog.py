"""
game_catalog.py
----------------
Automatically resolves a game's real Discord application_id (and therefore its
official cover image) from Discord's own public "detectable applications"
list — the exact same database Discord's client uses to auto-detect games on
people's PCs.

This means: no Developer Portal, no manual app_id, no manual image upload.
If a game's name matches an entry in Discord's own catalog, everyone (you,
your friends, anyone) sees the *exact same* name + icon that Discord itself
uses for that game.

Cache: the ~ few thousand entries are cached to disk for CACHE_TTL_SECONDS so
we don't hit Discord's API on every restart. A background task in main.py can
call ensure_loaded(force=True) periodically to pick up newly added games.
"""

import json
import time
import logging
import difflib
import unicodedata
from pathlib import Path

import aiohttp

log = logging.getLogger("presence-bot.catalog")

DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"
CACHE_FILE = Path("discord_games_cache.json")
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h
FUZZY_CUTOFF = 0.85  # how close a name must be to auto-match (0-1)

_by_exact_name: dict[str, str] = {}   # normalized name/alias -> app_id
_all_names: list[str] = []            # for fuzzy matching
_loaded = False


def _normalize(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().lower()


def _index(raw_apps: list) -> tuple[dict, list]:
    by_name = {}
    for app in raw_apps:
        app_id = app.get("id")
        if not app_id:
            continue
        names = set()
        if app.get("name"):
            names.add(app["name"])
        for alias in app.get("aliases") or []:
            names.add(alias)
        for n in names:
            by_name[_normalize(n)] = app_id
    return by_name, list(by_name.keys())


def _read_disk_cache(ignore_ttl: bool = False):
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not ignore_ttl and time.time() - data.get("fetched_at", 0) > CACHE_TTL_SECONDS:
            return None
        return data.get("apps")
    except Exception:
        return None


def _write_disk_cache(apps: list):
    try:
        CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "apps": apps}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"Could not write catalog cache to disk: {e}")


async def _fetch_from_discord() -> list:
    async with aiohttp.ClientSession() as session:
        async with session.get(DETECTABLE_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()


async def ensure_loaded(force: bool = False):
    """Load the catalog (from fresh cache, or fetch from Discord if stale/missing)."""
    global _by_exact_name, _all_names, _loaded

    if _loaded and not force:
        return

    apps = None if force else _read_disk_cache()

    if apps is None:
        try:
            apps = await _fetch_from_discord()
            _write_disk_cache(apps)
            log.info(f"Fetched {len(apps)} detectable games/apps from Discord's public catalog.")
        except Exception as e:
            log.error(f"Could not reach Discord's detectable-apps API: {e}")
            apps = _read_disk_cache(ignore_ttl=True) or []
            if apps:
                log.warning("Falling back to stale local catalog cache.")

    _by_exact_name, _all_names = _index(apps)
    _loaded = True


def resolve(name: str) -> str | None:
    """
    Return the official Discord application_id (as a string) for a game name,
    or None if no confident match was found in Discord's catalog.
    """
    key = _normalize(name)

    if key in _by_exact_name:
        return _by_exact_name[key]

    # Fuzzy fallback for near-matches (typos, slightly different casing/edition names).
    close = difflib.get_close_matches(key, _all_names, n=1, cutoff=FUZZY_CUTOFF)
    if close:
        matched_id = _by_exact_name[close[0]]
        log.info(f"Catalog: matched '{name}' -> '{close[0]}' (fuzzy) -> app_id {matched_id}")
        return matched_id

    return None
