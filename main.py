import discord
from discord.ext import commands
from config import BOT_TOKEN
from commands.game import setup_game
from commands.leaderboard import setup_leaderboard
from commands.points import setup_points

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f"✅ Bot logged in as {bot.user}")

# Register command sets
setup_teams(tree)
setup_game(tree)
setup_leaderboard(tree)
setup_points(tree)

bot.run(BOT_TOKEN)
