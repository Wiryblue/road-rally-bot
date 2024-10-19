import json
import discord
from discord import app_commands
from discord.ext import commands
import os

# Intents and bot setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # Enable this for reading messages
bot = commands.Bot(command_prefix='!', intents=intents)

# Placeholder for tasks and submissions
tasks = {}
submissions = {}

# Load configuration file
config_file = open("config.json")
config = json.load(config_file)

# Setup hook to sync slash commands
@bot.event
async def setup_hook():
    try:
        synced = await bot.tree.sync()
        print("Synced Commands: " + str(synced))
    except Exception as e:
        print(f"Error syncing commands: {e}")

# Ready event
@bot.event
async def on_ready():
    print("Road Rally Bot is running")

# Slash command to assign a task
@bot.tree.command(name="assign_task")
@app_commands.describe(task_id="The task ID", task_type="The type of task", task_description="Description of the task")
async def assign_task(interaction: discord.Interaction, task_id: int, task_type: str, task_description: str):
    """Assign a task to all teams with a specified type (time-sensitive, destination, all-day)."""
    if task_type not in ['time-sensitive', 'destination', 'all-day']:
        await interaction.response.send_message("Invalid task type. Please use 'time-sensitive', 'destination', or 'all-day'.")
        return

    tasks[task_id] = {
        'description': task_description,
        'type': task_type,
        'submissions': {},
        'points': 0
    }
    await interaction.response.send_message(f'Task {task_id} assigned ({task_type}): {task_description}')

# Slash command for teams to submit a task
@bot.tree.command(name="submit_task")
@app_commands.describe(task_id="The task ID you are submitting for")
async def submit_task(interaction: discord.Interaction, task_id: int):
    """Teams submit their task submissions."""

    if interaction.user.id not in submissions:
        submissions[interaction.user.id] = {}

    if task_id in tasks:
        await interaction.user.send('Please send your submission (image/video).')

        def check(m):
            return m.author == interaction.user and (m.attachments or m.content)

        msg = await bot.wait_for('message', check=check)

        if msg.attachments:
            for attachment in msg.attachments:
                submissions[interaction.user.id][task_id] = attachment.url

            await interaction.user.send('Submission received!')

            # Notify the moderator
            moderator_channel = bot.get_channel(config["moderator_channel"])  # Replace with actual channel ID
            await moderator_channel.send(f'Team {interaction.user.name} submitted for task {task_id}: {attachment.url}')
            await interaction.response.send_message(f"Submission received for task {task_id}", ephemeral=True)
        else:
            await interaction.user.send('Please send an attachment or a valid message.')
    else:
        await interaction.response.send_message('Task not found.', ephemeral=True)

# Slash command for the moderator to check submissions
@bot.tree.command(name="check_submissions")
@app_commands.describe(task_id="The task ID to check submissions for")
async def check_submissions(interaction: discord.Interaction, task_id: int):
    """Check all submissions for a specific task."""
    if task_id in tasks:
        submission_list = "\n".join(
            [f'Team {team_id}: {url}' for team_id, url in submissions.items() if task_id in submissions[team_id]])
        if submission_list:
            await interaction.response.send_message(f'Submissions for Task {task_id}:\n{submission_list}')
        else:
            await interaction.response.send_message(f'No submissions found for Task {task_id}.')
    else:
        await interaction.response.send_message('Task not found.')

# Slash command to assign points
@bot.tree.command(name="assign_points")
@app_commands.describe(task_id="The task ID", team_id="The team ID", points="Points to assign")
async def assign_points(interaction: discord.Interaction, task_id: int, team_id: int, points: int):
    """Assign points to a team for a specific task."""
    if task_id in tasks and team_id in submissions:
        tasks[task_id]['submissions'][team_id] = points
        await interaction.response.send_message(f'Assigned {points} points to Team {bot.get_user(team_id).name} for Task {task_id}.')
    else:
        await interaction.response.send_message('Task or team not found.')


# Database setup remains the same

# Register the slash commands for creating teams
@bot.tree.command(name="create_team", description="Create a new team by grouping members")
@discord.app_commands.describe(team_name="The name of the team", members="The members to be added to the team")
async def create_team(interaction: discord.Interaction, team_name: str, members: discord.User):
    # Ensure the command is only used in a specific server
    if interaction.guild.id != YOUR_SERVER_ID:
        await interaction.response.send_message("This command can only be used in the specified server.",
                                                ephemeral=True)
        return

    # Check if the user has the "Moderator" role
    moderator_role = discord.utils.get(interaction.guild.roles, name="Moderator")
    if moderator_role not in interaction.user.roles:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    # Add team and members to the database
    for member in members:
        cursor.execute('INSERT INTO teams (team_name, member_id) VALUES (?, ?)', (team_name, str(member.id)))
    conn.commit()

    await interaction.response.send_message(f'Team "{team_name}" created successfully with {len(members)} members.')


# Helper function to find which team a user belongs to (unchanged)
def get_team_for_member(member_id):
    cursor.execute('SELECT team_name FROM teams WHERE member_id = ?', (str(member_id),))
    team = cursor.fetchone()
    if team:
        return team[0]
    return None


# Slash command for task submission
@bot.tree.command(name="submit_task", description="Submit a task for your team")
@discord.app_commands.describe(task_id="ID of the task you are submitting")
async def submit_task(interaction: discord.Interaction, task_id: int):
    # Get the user's team
    team_name = get_team_for_member(interaction.user.id)

    if team_name:
        await interaction.response.send_message(f'Team "{team_name}" submitted task {task_id}.')
    else:
        await interaction.response.send_message("You are not part of any team!", ephemeral=True)


# Slash command to list all teams (moderator only)
@bot.tree.command(name="list_teams", description="List all the teams")
async def list_teams(interaction: discord.Interaction):
    # Check if the user has the "Moderator" role
    moderator_role = discord.utils.get(interaction.guild.roles, name="Moderator")
    if moderator_role not in interaction.user.roles:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    cursor.execute('SELECT DISTINCT team_name FROM teams')
    teams = cursor.fetchall()
    if teams:
        team_list = '\n'.join([team[0] for team in teams])
        await interaction.response.send_message(f"Teams:\n{team_list}")
    else:
        await interaction.response.send_message("No teams have been created yet.", ephemeral=True)


# Sync the slash commands with Discord when bot is ready
@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=YOUR_SERVER_ID))  # Sync the slash commands to your specific server
    print(f'Bot {bot.user} is ready and slash commands are synced.')


bot.run(config["bot_token"])  # Use your bot token from config
