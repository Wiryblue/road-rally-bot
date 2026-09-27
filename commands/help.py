import discord
from discord import app_commands

from utils import is_admin

PLAYER_HELP = (
    "**How Road Rally works**\n"
    "Your team visits each location and completes photo/video tasks. Judges review every "
    "submission, and accepted tasks earn points.\n\n"
    "**Commands**\n"
    "• `/my_tasks` — your tasks for the current location. Pick one from the menu to submit.\n"
    "• `/submit` — start typing a task name, then attach your photo or video.\n"
    "• `/team` — your teammates and points.\n"
    "• `/leaderboard` — the standings (when visible).\n\n"
    "**Tips**\n"
    "• You'll get a DM when a submission is accepted or denied.\n"
    "• Denied? Just submit again. A new upload replaces one that's still in review.\n"
    "• Videos work too — keep them short so they upload quickly."
)

ADMIN_HELP = (
    "**Setup:** `/load_tasks` (safe to re-run; updates existing tasks) → `/create_team` → `/start_game`\n"
    "**Teams:** `/list_teams`, `/rename_team`, `/remove_team`\n"
    "**Scoring:** Accept/Deny in the moderator channel, `/add_points`, `/remove_points`\n"
    "**Leaderboard:** `/toggle_leaderboard` (admins can always view it)"
)


def setup_help(tree: app_commands.CommandTree):

    @tree.command(name="help", description="How the game works and what each command does")
    @app_commands.describe(public="Post it in the channel for everyone (e.g. to pin it)")
    async def help_command(interaction: discord.Interaction, public: bool = False):
        embed = discord.Embed(title="🏁 Road Rally Help", description=PLAYER_HELP, color=discord.Color.blurple())
        admin = interaction.guild is not None and is_admin(interaction.user)
        # Admin commands stay out of public posts.
        if admin and not public:
            embed.add_field(name="🛠️ Game Admin", value=ADMIN_HELP, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=not (public and admin))
