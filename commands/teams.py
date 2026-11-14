import logging
import discord
from discord import app_commands
from typing import Optional, List

from database import cursor, db, add_user_to_team, get_team_id
from commands.leaderboard import leaderboard_visible, request_leaderboard_refresh
from utils import reject_if_not_admin, chunk_lines

log = logging.getLogger("rally.teams")


async def _gather_member_names(interaction: discord.Interaction, team_id: int) -> List[str]:
    cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
    members = []
    for (discord_id,) in cursor.fetchall():
        # Guild/user caches first; only hit the API for people the bot hasn't seen.
        user = interaction.guild.get_member(discord_id) or interaction.client.get_user(discord_id)
        if user is None:
            try:
                user = await interaction.client.fetch_user(discord_id)
            except discord.HTTPException:
                user = None
        members.append(user.display_name if user else f"Unknown({discord_id})")
    return members


def build_team_embed(user_id: int) -> Optional[discord.Embed]:
    team_id = get_team_id(user_id)
    if team_id is None:
        return None

    cursor.execute("SELECT name, points FROM teams WHERE id = ?", (team_id,))
    name, points = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) + 1 FROM teams WHERE points > ?", (points,))
    rank = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM submissions WHERE team_id = ? AND status = 'Accepted'", (team_id,))
    completed = cursor.fetchone()[0]

    cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
    members = "\n".join(f"• <@{discord_id}>" for (discord_id,) in cursor.fetchall())

    embed = discord.Embed(title=f"🚗 {name}", color=discord.Color.blurple())
    embed.add_field(name="Points", value=str(points))
    embed.add_field(name="Tasks completed", value=str(completed))
    # Rank would give away standings while the leaderboard is hidden.
    if leaderboard_visible():
        embed.add_field(name="Rank", value=f"#{rank}")
    embed.add_field(name="Members", value=members or "None", inline=False)
    return embed


def setup_teams(tree: app_commands.CommandTree):

    @tree.command(name="team", description="See your team, teammates, and points")
    async def team(interaction: discord.Interaction):
        embed = build_team_embed(interaction.user.id)
        if embed is None:
            await interaction.response.send_message("You're not on a team. Ask a Game Admin to add you.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="create_team", description="Create a new team")
    @app_commands.describe(
        team_name="The name of the team",
        user1="Team member",
        user2="Team member",
        user3="Team member",
        user4="Team member",
        user5="Team member",
        user6="Team member",
    )
    @app_commands.guild_only()
    async def create_team(
        interaction: discord.Interaction,
        team_name: str,
        user1: discord.Member,
        user2: Optional[discord.Member] = None,
        user3: Optional[discord.Member] = None,
        user4: Optional[discord.Member] = None,
        user5: Optional[discord.Member] = None,
        user6: Optional[discord.Member] = None,
    ):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        users = [user for user in (user1, user2, user3, user4, user5, user6) if user is not None]
        cursor.execute("INSERT INTO teams (name, points) VALUES (?, 0)", (team_name,))
        team_id = cursor.lastrowid

        duplicate_users = []
        added_users = []
        added_ids = []
        for user in users:
            cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (user.id,))
            if cursor.fetchone():
                duplicate_users.append(user.display_name)
                continue
            add_user_to_team(user.id, team_id)
            added_users.append(user.display_name)
            added_ids.append(user.id)

        db.commit()
        log.info("TEAM CREATED %r (id %s) by %s: added %s, skipped %s", team_name, team_id, interaction.user, added_users, duplicate_users)

        response = [f"Team '{team_name}' created successfully! (ID: {team_id})"]
        response.append("Added members: " + (", ".join(added_users) if added_users else "None"))
        if duplicate_users:
            response.append("Skipped (already on a team): " + ", ".join(duplicate_users))

        await interaction.followup.send("\n".join(response), ephemeral=True)
        request_leaderboard_refresh(interaction.client)

        # Imported here because the player module builds on this one.
        from commands.player import send_instructions
        await send_instructions(
            interaction.client, added_ids, f"👋 Welcome to Road Rally! You're on **{team_name}**."
        )

    @tree.command(name="list_teams", description="Private list of teams and their members")
    @app_commands.guild_only()
    async def list_teams(interaction: discord.Interaction):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        cursor.execute("SELECT id, name, points FROM teams")
        teams = cursor.fetchall()
        if not teams:
            await interaction.followup.send("No teams registered yet.", ephemeral=True)
            return

        lines = []
        for team_id, team_name, points in teams:
            members = await _gather_member_names(interaction, team_id)
            member_str = ", ".join(members) if members else "No members"
            lines.append(f"**Team {team_name} (ID: {team_id}, Points: {points})**\nMembers: {member_str}\n")

        # Messages cap at 2000 characters, so long rosters are split.
        for chunk in chunk_lines(lines):
            await interaction.followup.send(chunk, ephemeral=True)

    @tree.command(name="rename_team", description="Rename an existing team (Game Admin only)")
    @app_commands.describe(team_id="The ID of the team to rename", new_name="The new name for the team")
    @app_commands.guild_only()
    async def rename_team(interaction: discord.Interaction, team_id: int, new_name: str):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
        if cursor.fetchone() is None:
            await interaction.followup.send(f"Team with ID {team_id} not found.", ephemeral=True)
            return

        cursor.execute("UPDATE teams SET name = ? WHERE id = ?", (new_name, team_id))
        db.commit()
        log.info("TEAM RENAMED id %s to %r by %s", team_id, new_name, interaction.user)
        await interaction.followup.send(f"Team renamed successfully to '{new_name}'.", ephemeral=True)
        request_leaderboard_refresh(interaction.client)

    @tree.command(name="remove_team", description="Remove an existing team (Game Admin only)")
    @app_commands.describe(team_id="The ID of the team to remove")
    @app_commands.guild_only()
    async def remove_team(interaction: discord.Interaction, team_id: int):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
        team = cursor.fetchone()
        if team is None:
            await interaction.followup.send(f"Team with ID {team_id} not found.", ephemeral=True)
            return

        cursor.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        cursor.execute("DELETE FROM users WHERE team_id = ?", (team_id,))
        cursor.execute("DELETE FROM submissions WHERE team_id = ?", (team_id,))
        db.commit()
        log.warning("TEAM REMOVED %r (id %s) by %s", team[0], team_id, interaction.user)
        await interaction.followup.send(f"Team '{team[0]}' and its members have been removed.", ephemeral=True)
        request_leaderboard_refresh(interaction.client)

