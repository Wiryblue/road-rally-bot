import discord
from discord.ext import commands
import os

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # Make sure to enable this for reading messages
bot = commands.Bot(command_prefix='!', intents=intents)

# Placeholder for tasks and submissions
tasks = {}
submissions = {}

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

# Command to assign a task
@bot.command()
async def assign_task(ctx, task_id: int, *, task_description: str):
    """Assign a task to all teams."""
    tasks[task_id] = {
        'description': task_description,
        'type': 'destination',  # Placeholder for task type
        'submissions': {},
        'points': 0
    }
    await ctx.send(f'Task {task_id} assigned: {task_description}')

# Command for teams to submit a task
@bot.command()
async def submit_task(ctx, task_id: int):
    """Teams submit their task submissions."""
    if ctx.author.id not in submissions:
        submissions[ctx.author.id] = {}
    
    if task_id in tasks:
        await ctx.author.send('Please send your submission (image/video).')
        
        def check(m):
            return m.author == ctx.author and (m.attachments or m.content)

        msg = await bot.wait_for('message', check=check)

        if msg.attachments:
            for attachment in msg.attachments:
                submissions[ctx.author.id][task_id] = attachment.url

            await ctx.author.send('Submission received!')

            # Notify the moderator
            moderator_channel = bot.get_channel(YOUR_MODERATOR_CHANNEL_ID)  # Replace with actual channel ID
            await moderator_channel.send(f'Team {ctx.author.name} submitted for task {task_id}: {attachment.url}')
        else:
            await ctx.author.send('Please send an attachment or a valid message.')
    else:
        await ctx.send('Task not found.')

# Command for the moderator to check submissions
@bot.command()
async def check_submissions(ctx, task_id: int):
    """Check all submissions for a specific task."""
    if task_id in tasks:
        submission_list = "\n".join([f'Team {bot.get_user(team_id).name}: {url}' for team_id, url in submissions.items() if task_id in submissions[team_id]])
        await ctx.send(f'Submissions for Task {task_id}:\n{submission_list}')
    else:
        await ctx.send('Task not found.')

# Command to assign points
@bot.command()
async def assign_points(ctx, task_id: int, team_id: int, points: int):
    """Assign points to a team for a specific task."""
    if task_id in tasks and team_id in submissions:
        tasks[task_id]['submissions'][team_id] = points
        await ctx.send(f'Assigned {points} points to Team {bot.get_user(team_id).name} for Task {task_id}.')
    else:
        await ctx.send('Task or team not found.')

bot.run(os.getenv('YOUR_BOT_TOKEN'))  # Use your bot token
