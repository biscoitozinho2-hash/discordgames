import os
import json
import asyncio
import logging
import threading
import datetime

import discord
from discord.ext import tasks
from flask import Flask

import game_catalog

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GAMES_FILE = "games.json"
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("presence-bot")

ACTIVITY_TYPE_MAP = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
    "streaming": discord.ActivityType.streaming,
}

# ---------------------------------------------------------------------------
# Flask keep-alive server (for Render + external pingers like UptimeRobot)
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive.", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_flask():
    # Threaded Flask dev server is fine for a lightweight ping endpoint.
    app.run(host="0.0.0.0", port=PORT)


def start_keep_alive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    log.info(f"Keep-alive web server started on 0.0.0.0:{PORT}")


# ---------------------------------------------------------------------------
# Games list loading
# ---------------------------------------------------------------------------
# Types for which it makes sense to auto-look-up an official app_id/icon.
_AUTO_RESOLVABLE_TYPES = {"playing", "watching", "competing"}


def load_games():
    with open(GAMES_FILE, "r", encoding="utf-8") as f:
        games = json.load(f)

    if not isinstance(games, list) or not games:
        raise ValueError("games.json must contain a non-empty list of game entries.")

    for entry in games:
        if "type" not in entry or "name" not in entry or "duration" not in entry:
            raise ValueError(f"Invalid game entry, missing required keys: {entry}")
        activity_type = entry["type"].lower()
        if activity_type not in ACTIVITY_TYPE_MAP:
            raise ValueError(f"Unknown activity type '{entry['type']}' in entry: {entry}")
        if activity_type == "streaming" and "url" not in entry:
            raise ValueError(f"Streaming activity requires a 'url' key: {entry}")

        # --- Automatic app_id / image resolution -------------------------
        # If the entry doesn't already have a manually-set app_id, look it
        # up in Discord's own public catalog of detectable games. This is
        # the same name+icon every Discord user sees for that game — no
        # Developer Portal, no manual copy-pasting of asset keys.
        if activity_type in _AUTO_RESOLVABLE_TYPES and "app_id" not in entry:
            resolved_id = game_catalog.resolve(entry["name"])
            if resolved_id:
                entry["app_id"] = resolved_id
            else:
                log.warning(
                    f"'{entry['name']}' not found in Discord's detectable-games catalog "
                    f"— it will show as plain text with no cover image. "
                    f"If it's a real game, check the spelling; otherwise set 'app_id' "
                    f"manually in games.json for custom/unlisted entries."
                )

    return games


def build_activity(entry: dict) -> discord.Activity:
    activity_type_str = entry["type"].lower()
    
    # === 1. برمجة سبوتيفاي بشكل حقيقي 100% ===
    if activity_type_str == "listening" and entry["name"].lower() == "spotify":
        # حساب وقت البداية والنهاية لظهور شريط الوقت (Progress Bar)
        start_time = datetime.datetime.utcnow()
        end_time = start_time + datetime.timedelta(seconds=entry.get("duration", 180))
        
        return discord.Spotify(
            title=entry.get("song_title", "Unknown Song"),
            artist=entry.get("artist", "Unknown Artist"),
            album=entry.get("album", "Unknown Album"),
            track_id=entry.get("track_id", "0"),
            start=start_time,
            end=end_time
        )
        
    # === 2. برمجة الألعاب بشكل احترافي مع الصور والتفاصيل ===
    activity_type = ACTIVITY_TYPE_MAP.get(activity_type_str, discord.ActivityType.playing)
    
    kwargs = {
        "type": activity_type,
        "name": entry["name"]
    }
    
    if "app_id" in entry:
        kwargs["application_id"] = int(entry["app_id"])
    if "details" in entry:
        kwargs["details"] = entry["details"]  # السطر الأول تحت اسم اللعبة
    if "state" in entry:
        kwargs["state"] = entry["state"]      # السطر الثاني تحت اسم اللعبة
        
    # إضافة روابط الصور
    assets = {}
    if "large_image" in entry:
        assets["large_image"] = entry["large_image"]
    if "small_image" in entry:
        assets["small_image"] = entry["small_image"]
        
    if assets:
        kwargs["assets"] = assets

    if activity_type == discord.ActivityType.streaming:
        return discord.Streaming(name=entry["name"], url=entry.get("url", "https://twitch.tv/discord"))
        
    return discord.Activity(**kwargs)




# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------
client = discord.Client()


# Shared state for the presence cycle
_games = []
_current_index = 0
_cycle_task_lock = asyncio.Lock()


@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")
    await game_catalog.ensure_loaded()  # load Discord's detectable-games catalog once
    if not presence_cycle.is_running():
        presence_cycle.start()
    if not refresh_catalog.is_running():
        refresh_catalog.start()


@client.event
async def on_disconnect():
    log.warning("Disconnected from Discord. discord.py will attempt to auto-reconnect.")


@client.event
async def on_resumed():
    log.info("Session resumed after reconnect.")


@client.event
async def on_error(event_method, *args, **kwargs):
    log.exception(f"Unhandled exception in event: {event_method}")


# ---------------------------------------------------------------------------
# Presence cycling loop
#
# We don't use a fixed-interval @tasks.loop(seconds=X) because each entry can
# have a *different* duration. Instead we run one continuous loop task that
# sleeps for the duration specified per-entry, and reloads games.json each
# lap so you can edit it live without restarting the bot.
# ---------------------------------------------------------------------------
@tasks.loop(seconds=1, count=1)  # runs the body once; the real looping is internal
async def presence_cycle():
    global _games, _current_index

    async with _cycle_task_lock:
        while not client.is_closed():
            try:
                _games = load_games()
            except Exception as e:
                log.error(f"Failed to load {GAMES_FILE}: {e}. Retrying in 30s.")
                await asyncio.sleep(30)
                continue

            if _current_index >= len(_games):
                _current_index = 0

            entry = _games[_current_index]

            try:
                activity = build_activity(entry)
                await client.change_presence(activity=activity)
                log.info(
                    f"Presence set: [{entry['type']}] {entry['name']} "
                    f"for {entry['duration']}s"
                )
            except discord.ConnectionClosed:
                log.warning("Connection closed while setting presence; will retry after reconnect.")
                await asyncio.sleep(5)
                continue
            except Exception as e:
                log.error(f"Failed to set presence for entry {entry}: {e}")
                await asyncio.sleep(5)

            duration = max(1, int(entry.get("duration", 60)))
            _current_index = (_current_index + 1) % len(_games)

            await asyncio.sleep(duration)


@presence_cycle.before_loop
async def before_presence_cycle():
    await client.wait_until_ready()


@presence_cycle.error
async def presence_cycle_error(error):
    log.exception(f"presence_cycle crashed: {error}")
    # Restart the loop after a short delay so a single bad exception
    # doesn't permanently kill presence updates.
    await asyncio.sleep(10)
    if not presence_cycle.is_running():
        presence_cycle.start()


# ---------------------------------------------------------------------------
# Periodically refresh Discord's detectable-games catalog so newly added
# games (or ones that weren't found on first boot) get picked up without
# needing a restart.
# ---------------------------------------------------------------------------
@tasks.loop(hours=24)
async def refresh_catalog():
    await game_catalog.ensure_loaded(force=True)


# ---------------------------------------------------------------------------
# Top-level run loop with reconnect/backoff handling
# ---------------------------------------------------------------------------
async def run_bot_forever():
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set.")

    backoff = 5
    max_backoff = 300

    while True:
        try:
            log.info("Starting Discord client...")
            await client.start(TOKEN)
        except discord.LoginFailure:
            log.critical("Invalid bot token. Fix DISCORD_BOT_TOKEN and restart.")
            raise
        except (discord.ConnectionClosed, discord.GatewayNotFound, OSError) as e:
            log.error(f"Connection error: {e}. Reconnecting in {backoff}s...")
        except Exception as e:
            log.exception(f"Unexpected error: {e}. Reconnecting in {backoff}s...")
        finally:
            if not client.is_closed():
                await client.close()

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)  # exponential backoff, capped


def main():
    start_keep_alive()
    while True:
        try:
            asyncio.run(run_bot_forever())
        except discord.LoginFailure:
            break  # bad token, no point retrying
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception:
            log.exception("Fatal error in main loop, restarting entire process in 15s.")
            import time
            time.sleep(15)


if __name__ == "__main__":
    main()
