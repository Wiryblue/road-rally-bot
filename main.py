import discord
from discord.ext import commands
from config import BOT_TOKEN
from commands.game import setup_game, ReviewButton
from commands.leaderboard import setup_leaderboard
from commands.points import setup_points
from commands.teams import setup_teams
from commands.help import setup_help

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True


class RallyBot(commands.Bot):
    async def setup_hook(self):
        # Re-attach review buttons so ones posted before a restart still work.
        self.add_dynamic_items(ReviewButton)
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} commands")


bot = RallyBot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f"✅ Bot logged in as {bot.user}")

# Register command sets
setup_teams(tree)
setup_game(tree)
setup_leaderboard(tree)
setup_points(tree)
setup_help(tree)

bot.run(BOT_TOKEN)
