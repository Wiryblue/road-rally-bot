import logging
import asyncio

import discord

log = logging.getLogger("rally.utils")

# Keep bulk DMs from tripping Discord's rate limits.
DM_CONCURRENCY = 5


def is_admin(user):
    return any(role.name == "Game Admin" for role in getattr(user, "roles", []))

async def reject_if_not_admin(interaction):
    if interaction.guild is None or not is_admin(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return True
    return False


async def dm_user(client: discord.Client, user_id: int, content: str | None = None, **kwargs) -> bool:
    """Send a DM, preferring the local cache over an API lookup. Returns False on failure."""
    try:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
        await user.send(content, **kwargs)
        return True
    except discord.HTTPException as exc:
        log.warning("Failed to DM user %s: %s", user_id, exc)
        return False


async def dm_many(client: discord.Client, user_ids, content: str | None = None, **kwargs) -> int:
    """DM several users concurrently. Returns how many DMs were delivered."""
    semaphore = asyncio.Semaphore(DM_CONCURRENCY)

    async def send(user_id):
        async with semaphore:
            return await dm_user(client, user_id, content, **kwargs)

    results = await asyncio.gather(*(send(uid) for uid in set(user_ids)))
    return sum(results)


def chunk_lines(lines, limit=1900):
    """Group lines into strings that fit under Discord's message/embed length limits."""
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def progress_bar(value, total, width=10):
    filled = round(width * value / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)
