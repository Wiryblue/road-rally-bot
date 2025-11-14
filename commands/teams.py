import discord
from discord import app_commands
from typing import Optional, List

from database import cursor, db, add_user_to_team
from utils import reject_if_not_admin


async def _gather_member_names(interaction: discord.Interaction, team_id: int) -> List[str]:
    cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
    members = []
    for (discord_id,) in cursor.fetchall():
        user = interaction.client.get_user(discord_id)
        if user is None:
            try:
                user = await interaction.client.fetch_user(discord_id)
            except Exception:
                user = None
        members.append(user.name if user else f"Unknown({discord_id})")
    return members


def setup_teams(tree: app_commands.CommandTree):

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
        for user in users:
            cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (user.id,))
            if cursor.fetchone():
                duplicate_users.append(user.display_name)
                continue
            add_user_to_team(user.id, team_id)
            added_users.append(user.display_name)

        db.commit()

        response = [f"Team '{team_name}' created successfully! (ID: {team_id})"]
        response.append("Added members: " + (", ".join(added_users) if added_users else "None"))
        if duplicate_users:
            response.append("Skipped (already on a team): " + ", ".join(duplicate_users))

        await interaction.followup.send("\n".join(response), ephemeral=True)

    @tree.command(name="list_teams", description="Private list of teams and their members")
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

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @tree.command(name="rename_team", description="Rename an existing team (Game Admin only)")
    @app_commands.describe(team_id="The ID of the team to rename", new_name="The new name for the team")
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
        await interaction.followup.send(f"Team renamed successfully to '{new_name}'.", ephemeral=True)

    @tree.command(name="remove_team", description="Remove an existing team (Game Admin only)")
    @app_commands.describe(team_id="The ID of the team to remove")
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
        db.commit()
        await interaction.followup.send(f"Team '{team[0]}' and its members have been removed.", ephemeral=True)

