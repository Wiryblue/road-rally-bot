import discord
from discord import app_commands
from database import fetch_leaderboard
from utils import reject_if_not_admin

leaderboard_visible = True

def setup_leaderboard(tree: app_commands.CommandTree):

    @tree.command(name="leaderboard", description="View current leaderboard")
    async def leaderboard(interaction: discord.Interaction):
        data = fetch_leaderboard()
        if not data:
            await interaction.response.send_message("No teams yet.", ephemeral=True)
            return
        if not leaderboard_visible:
            await interaction.response.send_message("Leaderboard hidden.", ephemeral=True)
            return

        embed = discord.Embed(title="Leaderboard", color=discord.Color.blurple())
        for idx, (team_name, points) in enumerate(data):
            medal = ["🥇", "🥈", "🥉"][idx] if idx < 3 else "🔹"
            embed.add_field(name=f"{medal} {team_name}", value=f"{points} points", inline=False)

        await interaction.response.send_message(embed=embed)

    @tree.command(name="toggle_leaderboard", description="Toggle leaderboard visibility")
    async def toggle_leaderboard(interaction: discord.Interaction):
        if await reject_if_not_admin(interaction):
            return

        global leaderboard_visible
        leaderboard_visible = not leaderboard_visible
        state = "visible" if leaderboard_visible else "hidden"
        await interaction.response.send_message(f"Leaderboard is now {state}.", ephemeral=True)
