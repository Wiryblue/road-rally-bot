import asyncio
import time

import discord
from discord import app_commands
from config import config
from database import fetch_leaderboard, get_setting, set_setting
from utils import reject_if_not_admin, is_admin, chunk_lines, progress_bar

# Point changes arriving within this window are folded into a single message edit.
LIVE_REFRESH_DELAY = 5
_refresh_task: asyncio.Task | None = None


def leaderboard_visible():
    return get_setting("leaderboard_visible", "1") == "1"


def build_leaderboard_embed() -> discord.Embed:
    data = fetch_leaderboard()
    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())
    if not data:
        embed.description = "No teams yet."
        return embed

    medals = ["🥇", "🥈", "🥉"]
    top = max(data[0][1], 1)
    lines = [
        f"{medals[idx] if idx < 3 else f'`{idx + 1}.`'} **{team_name}** — {points} pts\n"
        f"{progress_bar(max(points, 0), top)}"
        for idx, (team_name, points) in enumerate(data)
    ]
    embed.description = chunk_lines(lines, limit=3900)[0] + f"\n\nUpdated <t:{int(time.time())}:R>"
    return embed


def request_leaderboard_refresh(client: discord.Client) -> None:
    """Schedule an update of the live leaderboard message (debounced)."""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        return
    _refresh_task = asyncio.create_task(_refresh_live_leaderboard(client))


async def _refresh_live_leaderboard(client: discord.Client) -> None:
    await asyncio.sleep(LIVE_REFRESH_DELAY)
    channel_id = get_setting("live_leaderboard_channel") or config.get("leaderboard_channel") or config.get("spectator_channel")
    channel = client.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        return

    if leaderboard_visible():
        embed = build_leaderboard_embed()
    else:
        embed = discord.Embed(title="🏆 Leaderboard", description="🙈 Hidden for now — stay tuned!", color=discord.Color.dark_grey())

    message_id = get_setting("live_leaderboard_message")
    try:
        if message_id:
            await channel.get_partial_message(int(message_id)).edit(embed=embed)
            return
    except discord.NotFound:
        pass  # someone deleted it; post a fresh one below
    except discord.HTTPException as exc:
        print(f"Failed to update live leaderboard: {exc}")
        return

    try:
        message = await channel.send(embed=embed)
        set_setting("live_leaderboard_message", message.id)
    except discord.HTTPException as exc:
        print(f"Failed to post live leaderboard: {exc}")


def setup_leaderboard(tree: app_commands.CommandTree):

    @tree.command(name="leaderboard", description="View current leaderboard")
    async def leaderboard(interaction: discord.Interaction):
        # Admins can always peek, even while it's hidden from players.
        hidden = not leaderboard_visible()
        if hidden and not (interaction.guild and is_admin(interaction.user)):
            await interaction.response.send_message("🙈 The leaderboard is hidden right now — stay tuned!", ephemeral=True)
            return

        embed = build_leaderboard_embed()
        if hidden:
            embed.set_footer(text="Hidden from players")
        await interaction.response.send_message(embed=embed, ephemeral=hidden)

    @tree.command(name="toggle_leaderboard", description="Toggle leaderboard visibility")
    @app_commands.guild_only()
    async def toggle_leaderboard(interaction: discord.Interaction):
        if await reject_if_not_admin(interaction):
            return

        visible = not leaderboard_visible()
        set_setting("leaderboard_visible", "1" if visible else "0")
        state = "visible" if visible else "hidden"
        await interaction.response.send_message(f"Leaderboard is now {state}.", ephemeral=True)
        request_leaderboard_refresh(interaction.client)

    @tree.command(name="live_leaderboard", description="Post a self-updating leaderboard in this channel")
    @app_commands.guild_only()
    async def live_leaderboard(interaction: discord.Interaction):
        if await reject_if_not_admin(interaction):
            return
        set_setting("live_leaderboard_channel", interaction.channel_id)
        set_setting("live_leaderboard_message", "")
        await interaction.response.send_message(
            "Live leaderboard will appear here in a few seconds and update itself as points change. Pin it!",
            ephemeral=True,
        )
        request_leaderboard_refresh(interaction.client)
