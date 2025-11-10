from discord import app_commands
from database import cursor, db
from utils import reject_if_not_admin

def setup_points(tree: app_commands.CommandTree):

    @tree.command(name="add_points", description="Add points to a team")
    async def add_points(interaction, team_id: int, points: int):
        if await reject_if_not_admin(interaction):
            return
        cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (points, team_id))
        db.commit()
        await interaction.response.send_message(f"Added {points} points to team {team_id}.")

    @tree.command(name="remove_points", description="Remove points from a team")
    async def remove_points(interaction, team_id: int, points: int):
        if await reject_if_not_admin(interaction):
            return
        cursor.execute("UPDATE teams SET points = points - ? WHERE id = ?", (points, team_id))
        db.commit()
        await interaction.response.send_message(f"Removed {points} points from team {team_id}.")
