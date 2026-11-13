import discord
from discord.ext import commands
from config import BOT_TOKEN
from commands.game import setup_game, ReviewButton, TaskSelect
from commands.leaderboard import setup_leaderboard, request_leaderboard_refresh
from commands.points import setup_points
from commands.teams import setup_teams
from commands.help import setup_help
from commands.player import setup_player

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True


class RallyBot(commands.Bot):
    async def setup_hook(self):
        # Re-attach review buttons and task pickers so ones posted before a restart still work.
        self.add_dynamic_items(ReviewButton, TaskSelect)
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} commands")


bot = RallyBot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f"✅ Bot logged in as {bot.user}")
    request_leaderboard_refresh(bot)

# Register command sets
setup_teams(tree)
setup_game(tree)
setup_leaderboard(tree)
setup_points(tree)
setup_help(tree)
setup_player(bot, tree)

bot.run(BOT_TOKEN)
