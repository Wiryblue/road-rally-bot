import discord
from discord import app_commands
from database import cursor, db, get_team_member_ids
from commands.leaderboard import request_leaderboard_refresh
from utils import reject_if_not_admin, dm_many


async def _adjust_points(interaction: discord.Interaction, team_id: int, delta: int, reason: str | None):
    cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (delta, team_id))
    db.commit()
    if cursor.rowcount == 0:
        await interaction.response.send_message(f"Team {team_id} not found. Check `/list_teams`.", ephemeral=True)
        return

    verb = f"Added {delta} points to" if delta >= 0 else f"Removed {-delta} points from"
    suffix = f" Reason: {reason}" if reason else ""
    await interaction.response.send_message(f"{verb} team {team_id}.{suffix}", ephemeral=True)
    request_leaderboard_refresh(interaction.client)

    change = f"+{delta}" if delta >= 0 else str(delta)
    await dm_many(interaction.client, get_team_member_ids(team_id), f"📣 Team update: {change} points.{suffix}")


def setup_points(tree: app_commands.CommandTree):

    @tree.command(name="add_points", description="Add points to a team")
    @app_commands.describe(team_id="Team ID (see /list_teams)", points="Points to add", reason="Shown to the team")
    @app_commands.guild_only()
    async def add_points(interaction: discord.Interaction, team_id: int, points: app_commands.Range[int, 1], reason: str | None = None):
        if await reject_if_not_admin(interaction):
            return
        await _adjust_points(interaction, team_id, points, reason)

    @tree.command(name="remove_points", description="Remove points from a team")
    @app_commands.describe(team_id="Team ID (see /list_teams)", points="Points to remove", reason="Shown to the team")
    @app_commands.guild_only()
    async def remove_points(interaction: discord.Interaction, team_id: int, points: app_commands.Range[int, 1], reason: str | None = None):
        if await reject_if_not_admin(interaction):
            return
        await _adjust_points(interaction, team_id, -points, reason)
