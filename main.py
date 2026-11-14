import logging

import discord
from discord import app_commands
from discord.ext import commands

import bot_logging
from config import BOT_TOKEN
from commands.game import setup_game, ReviewButton, TaskSelect
from commands.leaderboard import setup_leaderboard, request_leaderboard_refresh
from commands.points import setup_points
from commands.teams import setup_teams
from commands.help import setup_help
from commands.player import setup_player
from utils import reject_if_not_admin

bot_logging.setup_logging()
log = logging.getLogger("rally")

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True


class RallyBot(commands.Bot):
    async def setup_hook(self):
        bot_logging.start_background_checks(self)
        # Re-attach review buttons and task pickers so ones posted before a restart still work.
        self.add_dynamic_items(ReviewButton, TaskSelect)
        synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))


bot = RallyBot(command_prefix="!", intents=intents)
tree = bot.tree
bot_logging.install(bot)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if not bot_logging.diagnose(interaction, error):
        await bot_logging.tell_user_something_broke(interaction)


@bot.event
async def on_ready():
    log.info("✅ Logged in as %s (%s) in %d server(s)", bot.user, bot.user.id, len(bot.guilds))
    request_leaderboard_refresh(bot)


@tree.command(name="bot_status", description="Bot health: lag, errors, and which machine it's running on")
@app_commands.guild_only()
async def bot_status(interaction: discord.Interaction):
    if await reject_if_not_admin(interaction):
        return
    summary = bot_logging.health_summary(interaction.client).replace(" · ", "\n")
    await interaction.response.send_message(
        f"**🩺 Bot status**\n{summary}\n-# Full details in logs/bot.log and logs/errors.log on that machine.",
        ephemeral=True,
    )


# Register command sets
setup_teams(tree)
setup_game(tree)
setup_leaderboard(tree)
setup_points(tree)
setup_help(tree)
setup_player(bot, tree)

# log_handler=None: logging is already set up by bot_logging (console + files).
bot.run(BOT_TOKEN, log_handler=None)
