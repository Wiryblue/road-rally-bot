import discord
from discord import app_commands

from utils import is_admin

PLAYER_HELP = (
    "**How Road Rally works**\n"
    "Your team visits each location and completes photo/video tasks. Judges review every "
    "submission, and accepted tasks earn points.\n\n"
    "**Turning in a task**\n"
    "1. Run `/my_tasks` and pick the task from the dropdown.\n"
    "2. Send your photo, video or link in the same chat. The bot forwards it to the judges.\n\n"
    "**Commands**\n"
    "• `/my_tasks` — what to do at this location, what's done, and where you submit.\n"
    "• `/team` — teammates and points. `/leaderboard` — the standings (when visible).\n\n"
    "**Tips**\n"
    "• You'll get a DM when a submission is accepted or denied, and when a teammate submits.\n"
    "• **Video won't send?** Discord caps uploads at 10 MB. Trim it, or upload it to Google Drive, "
    "YouTube (unlisted) or iCloud and paste the share link instead. Make sure the link is viewable by anyone.\n"
    "• Denied? Pick the task in `/my_tasks` again and resend. A new upload replaces one still in review.\n"
    "• Keep your upload message — deleting it removes it from the judges' view."
)

ADMIN_HELP = (
    "**Setup:** `/load_tasks` (safe to re-run) → `/create_team` (DMs members how to play) → `/start_game`\n"
    "**Start over:** `/delete_all_tasks confirm:DELETE` wipes tasks and submissions (keeps points), then `/load_tasks`\n"
    "**Live:** `/live_leaderboard` in a channel, `/announce`, `/queue` for pending reviews\n"
    "**Teams:** `/list_teams`, `/rename_team`, `/remove_team`\n"
    "**Scoring:** Accept/Deny in the moderator channel, `/add_points`, `/remove_points`\n"
    "**Leaderboard:** `/toggle_leaderboard` (admins can always view it)"
)


def build_help_embed(show_admin: bool = False) -> discord.Embed:
    embed = discord.Embed(title="🏁 Road Rally Help", description=PLAYER_HELP, color=discord.Color.blurple())
    if show_admin:
        embed.add_field(name="🛠️ Game Admin", value=ADMIN_HELP, inline=False)
    return embed


def setup_help(tree: app_commands.CommandTree):

    @tree.command(name="help", description="How the game works and what each command does")
    @app_commands.describe(public="Post it in the channel for everyone (e.g. to pin it)")
    async def help_command(interaction: discord.Interaction, public: bool = False):
        admin = interaction.guild is not None and is_admin(interaction.user)
        # Admin commands stay out of public posts.
        embed = build_help_embed(show_admin=admin and not public)
        await interaction.response.send_message(embed=embed, ephemeral=not (public and admin))
