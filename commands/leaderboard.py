import discord
from discord import app_commands
from database import fetch_leaderboard, get_setting, set_setting
from utils import reject_if_not_admin, is_admin, chunk_lines


def leaderboard_visible():
    return get_setting("leaderboard_visible", "1") == "1"


def setup_leaderboard(tree: app_commands.CommandTree):

    @tree.command(name="leaderboard", description="View current leaderboard")
    async def leaderboard(interaction: discord.Interaction):
        data = fetch_leaderboard()
        if not data:
            await interaction.response.send_message("No teams yet.", ephemeral=True)
            return
        # Admins can always peek, even while it's hidden from players.
        hidden = not leaderboard_visible()
        if hidden and not (interaction.guild and is_admin(interaction.user)):
            await interaction.response.send_message("🙈 The leaderboard is hidden right now — stay tuned!", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[idx] if idx < 3 else f'`{idx + 1}.`'} **{team_name}** — {points} pts"
            for idx, (team_name, points) in enumerate(data)
        ]
        embed = discord.Embed(title="🏆 Leaderboard", description=chunk_lines(lines, limit=4000)[0], color=discord.Color.gold())
        if hidden:
            embed.set_footer(text="Hidden from players")
        await interaction.response.send_message(embed=embed, ephemeral=hidden)

    @tree.command(name="toggle_leaderboard", description="Toggle leaderboard visibility")
    async def toggle_leaderboard(interaction: discord.Interaction):
        if await reject_if_not_admin(interaction):
            return

        visible = not leaderboard_visible()
        set_setting("leaderboard_visible", "1" if visible else "0")
        state = "visible" if visible else "hidden"
        await interaction.response.send_message(f"Leaderboard is now {state}.", ephemeral=True)
