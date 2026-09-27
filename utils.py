import asyncio

import discord

# Keep bulk DMs from tripping Discord's rate limits.
_DM_CONCURRENCY = 5


def is_admin(user):
    return any(role.name == "Game Admin" for role in getattr(user, "roles", []))

async def reject_if_not_admin(interaction):
    if interaction.guild is None or not is_admin(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return True
    return False


async def dm_user(client: discord.Client, user_id: int, message: str) -> bool:
    """Send a DM, preferring the local cache over an API lookup. Returns False on failure."""
    try:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
        await user.send(message)
        return True
    except discord.HTTPException as exc:
        print(f"Failed to DM user {user_id}: {exc}")
        return False


async def dm_many(client: discord.Client, user_ids, message: str) -> int:
    """DM several users concurrently. Returns how many DMs were delivered."""
    semaphore = asyncio.Semaphore(_DM_CONCURRENCY)

    async def send(user_id):
        async with semaphore:
            return await dm_user(client, user_id, message)

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
