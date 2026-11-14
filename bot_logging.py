"""Logging and health diagnostics for the bot.

Writes to the console and to rotating files in ``logs/`` (small, so the Pi's SD card is fine):
  * logs/bot.log     everything at LOG_LEVEL (default INFO): every command, button, game event
  * logs/errors.log  warnings and errors only, so problems are easy to find

It also watches for the usual causes of "Unknown interaction" (10062) errors: a blocked
event loop, a slow connection to Discord, a wrong system clock, and a second copy of the bot
running with the same token.

Settings (environment variables): LOG_LEVEL=DEBUG|INFO|WARNING, LOG_DIR=logs
"""
import asyncio
import email.utils
import logging
import logging.handlers
import os
import platform
import resource
import socket
import sys
import time
from collections import deque

import aiohttp
import discord

LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Discord gives a bot 3 seconds to acknowledge an interaction.
ACK_DEADLINE = 3.0
SLOW_RECEIPT = 1.5       # warn when an interaction already arrives this old
LOOP_LAG_WARN = 0.5      # warn when the event loop stalls this long
HEALTH_INTERVAL = 300    # seconds between health summaries

log = logging.getLogger("rally")
_started = time.monotonic()
_loop_lag_samples: deque[tuple[float, float]] = deque(maxlen=1200)  # (monotonic time, lag)
_clock_offset: float | None = None  # local clock minus Discord's, in seconds
_counts = {"interactions": 0, "errors": 0, "expired": 0, "double_ack": 0}


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    everything = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    everything.setFormatter(fmt)

    problems = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "errors.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    problems.setLevel(logging.WARNING)
    problems.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for handler in (console, everything, problems):
        root.addHandler(handler)

    # discord.py is chatty at DEBUG; keep its connection events but not every HTTP call.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)

    log.info(
        "Starting on host=%s pid=%s python=%s discord.py=%s",
        socket.gethostname(), os.getpid(), platform.python_version(), discord.__version__,
    )


# ---------- Interaction details ----------
def describe(interaction: discord.Interaction) -> str:
    data = interaction.data or {}
    if interaction.type == discord.InteractionType.application_command:
        what = f"/{interaction.command.qualified_name if interaction.command else data.get('name', '?')}"
    elif interaction.type == discord.InteractionType.autocomplete:
        what = f"autocomplete /{data.get('name', '?')}"
    elif interaction.type == discord.InteractionType.component:
        values = data.get("values")
        what = f"component {data.get('custom_id', '?')}" + (f" values={values}" if values else "")
    elif interaction.type == discord.InteractionType.modal_submit:
        what = f"modal {data.get('custom_id', '?')}"
    else:
        what = str(interaction.type)
    where = "DM" if interaction.guild is None else f"guild={interaction.guild_id} channel={interaction.channel_id}"
    return f"{what} by {interaction.user} ({interaction.user.id}) in {where} [id={interaction.id}]"


def interaction_age(interaction: discord.Interaction) -> float:
    """Seconds since Discord created the interaction, corrected for this machine's clock error."""
    age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
    return age - (_clock_offset or 0.0)


def recent_loop_lag(window: float = 10.0) -> float:
    cutoff = time.monotonic() - window
    return max((lag for t, lag in _loop_lag_samples if t >= cutoff), default=0.0)


async def on_interaction(interaction: discord.Interaction) -> None:
    """Listener: logs every interaction the moment it arrives."""
    _counts["interactions"] += 1
    interaction.extras.setdefault("received", time.monotonic())
    age = interaction_age(interaction)
    if age > SLOW_RECEIPT:
        log.warning(
            "Arrived late (%.1fs old, %.1fs left to answer): %s. Loop lag %.2fs, gateway latency %.0fms.",
            age, ACK_DEADLINE - age, describe(interaction), recent_loop_lag(), interaction.client.latency * 1000,
        )
    else:
        log.info("→ %s (age %.0fms)", describe(interaction), age * 1000)


async def on_app_command_completion(interaction: discord.Interaction, command) -> None:
    received = interaction.extras.get("received", time.monotonic())
    elapsed = time.monotonic() - received
    level = logging.WARNING if elapsed > 10 else logging.INFO
    log.log(level, "✓ /%s by %s finished in %.2fs", command.qualified_name, interaction.user, elapsed)


def diagnose(interaction: discord.Interaction, error: BaseException) -> bool:
    """Log an interaction failure with a likely cause. Returns True if it was a timing problem."""
    original = getattr(error, "original", error)
    age = interaction_age(interaction)
    lag = recent_loop_lag(30)
    who = describe(interaction)

    if isinstance(original, discord.HTTPException) and original.code == 40060:
        _counts["double_ack"] += 1
        log.error(
            "ALREADY ANSWERED (40060): %s. Something else replied to this interaction first. "
            "Almost always a SECOND COPY OF THE BOT running with the same token (another terminal, "
            "PyCharm, or the Pi). Run only one.", who,
        )
        return True

    if isinstance(original, discord.NotFound) and original.code == 10062:
        _counts["expired"] += 1
        if lag >= LOOP_LAG_WARN:
            cause = (f"the bot was frozen for {lag:.1f}s (event loop blocked: slow code, an overloaded "
                     "or sleeping machine). See 'Event loop blocked' warnings.")
        elif age > ACK_DEADLINE:
            cause = (f"it was already {age:.1f}s old: slow network to Discord, or a reconnect. "
                     f"Gateway latency is {interaction.client.latency * 1000:.0f}ms.")
        else:
            cause = ("it was only %.1fs old and the bot wasn't frozen, so most likely another copy of the "
                     "bot with the same token answered it first. Check the Pi and any terminals/PyCharm." % age)
        log.error("EXPIRED (10062 Unknown interaction): %s. Likely cause: %s", who, cause)
        return True

    _counts["errors"] += 1
    log.error("Error in %s (age %.1fs): %r", who, age, original, exc_info=original)
    return False


async def tell_user_something_broke(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send("⚠️ Something went wrong. Please try again.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Something went wrong. Please try again.", ephemeral=True)
    except discord.HTTPException:
        pass


def install_component_error_logging() -> None:
    """Route button/dropdown/modal errors through diagnose() too (they bypass the command tree)."""

    async def view_on_error(self, interaction, error, item, /):
        if not diagnose(interaction, error):
            await tell_user_something_broke(interaction)

    async def modal_on_error(self, interaction, error, /):
        if not diagnose(interaction, error):
            await tell_user_something_broke(interaction)

    discord.ui.View.on_error = view_on_error
    discord.ui.Modal.on_error = modal_on_error


# ---------- Background health checks ----------
async def check_clock() -> None:
    """Compare this machine's clock with Discord's. A Pi without internet time can drift a lot."""
    global _clock_offset
    try:
        async with aiohttp.ClientSession() as session:
            sent = time.time()
            async with session.get("https://discord.com/api/v10/gateway", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                received = time.time()
                server = email.utils.parsedate_to_datetime(resp.headers["Date"]).timestamp()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never stop the bot
        log.warning("Couldn't check the system clock against Discord: %r", exc)
        return
    # The Date header has 1s resolution, so only trust offsets well beyond that.
    offset = (sent + received) / 2 - server
    _clock_offset = offset if abs(offset) > 2 else 0.0
    if abs(offset) > 2:
        log.warning(
            "System clock is %.1fs %s Discord's. Fix it (sudo timedatectl set-ntp true on the Pi); "
            "timing numbers in these logs are corrected for it.", abs(offset), "ahead of" if offset > 0 else "behind",
        )
    else:
        log.info("System clock OK (within 2s of Discord).")


async def watch_event_loop() -> None:
    """Detect anything that freezes the bot. A freeze over ~3s makes commands expire."""
    interval = 0.25  # short, so a freeze is measured close to its real length
    while True:
        before = time.monotonic()
        await asyncio.sleep(interval)
        lag = time.monotonic() - before - interval
        _loop_lag_samples.append((time.monotonic(), lag))
        if lag >= LOOP_LAG_WARN:
            log.warning("Event loop blocked for %.2fs. Commands arriving during this may expire.", lag)


def _memory_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes on macOS, KB on Linux


def health_summary(client: discord.Client) -> str:
    uptime = int(time.monotonic() - _started)
    return (
        f"uptime {uptime // 3600}h{uptime % 3600 // 60:02d}m · gateway {client.latency * 1000:.0f}ms · "
        f"max loop lag (5m) {recent_loop_lag(300):.2f}s · peak memory {_memory_mb():.0f}MB · "
        f"interactions {_counts['interactions']} · expired {_counts['expired']} · "
        f"double-answered {_counts['double_ack']} · other errors {_counts['errors']} · "
        f"host {socket.gethostname()} pid {os.getpid()}"
    )


async def report_health(client: discord.Client) -> None:
    await client.wait_until_ready()
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        log.info("Health: %s", health_summary(client))


def start_background_checks(client: discord.Client) -> None:
    for coro in (check_clock(), watch_event_loop(), report_health(client)):
        asyncio.create_task(coro)


def install(bot: discord.Client) -> None:
    """Hook the listeners above into the bot."""
    bot.add_listener(on_interaction, "on_interaction")
    bot.add_listener(on_app_command_completion, "on_app_command_completion")

    async def on_connect():
        log.info("Connected to Discord gateway.")

    async def on_disconnect():
        log.warning("Disconnected from Discord (discord.py reconnects automatically). Commands fail until it's back.")

    async def on_resumed():
        log.info("Gateway session resumed.")

    bot.add_listener(on_connect, "on_connect")
    bot.add_listener(on_disconnect, "on_disconnect")
    bot.add_listener(on_resumed, "on_resumed")
    install_component_error_logging()
