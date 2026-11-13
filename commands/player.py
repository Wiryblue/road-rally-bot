"""Player DMs. Submitting always goes through /my_tasks: pick the task, then send the file."""
import discord
from discord import app_commands

from commands.game import UPLOAD_LIMIT_HINT, claimed_uploads, media_from_message
from utils import dm_many

HOW_TO_PLAY = (
    "**How to turn in a task**\n"
    "1. Run `/my_tasks` and pick the task from the dropdown.\n"
    "2. Send your photo or video right after, in the same chat.\n"
    "`/team` shows your points · `/help` explains everything else.\n"
    f"-# {UPLOAD_LIMIT_HINT}"
)


async def send_instructions(client: discord.Client, user_ids, intro: str) -> int:
    """DM how-to-play instructions to several players. Returns how many were delivered."""
    return await dm_many(client, user_ids, f"{intro}\n\n{HOW_TO_PLAY}")


async def on_dm_message(message: discord.Message) -> None:
    """Point players to /my_tasks when they DM the bot outside of a submission."""
    if message.guild is not None or message.author.bot:
        return
    if message.id in claimed_uploads:
        # This upload belongs to a task the player just picked in /my_tasks.
        claimed_uploads.discard(message.id)
        return

    if media_from_message(message) is not None:
        await message.reply(
            "📸 I don't know which task this is for yet. Run `/my_tasks`, pick the task, then send it again."
        )
    else:
        await message.reply(HOW_TO_PLAY)


def setup_player(bot: discord.Client, tree: app_commands.CommandTree):
    bot.add_listener(on_dm_message, "on_message")
