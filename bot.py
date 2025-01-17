import json

import discord
import requests
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View

import asyncio
import sqlite3
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Initialize bot
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

Game_status = 0
leaderboard_visible = True

# Load configuration file
config_file = open("config.json")
config = json.load(config_file)
# Database setup
db = sqlite3.connect("game.db")
cursor = db.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, discord_id INTEGER, team_id INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY, name TEXT, points INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, location INTEGER, description TEXT, points INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    team_id INTEGER,
    task_id INTEGER,
    message_id INTEGER,
    status TEXT,
    photo_url TEXT,
    UNIQUE(team_id, task_id)
)''')

db.commit()

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(credentials)

def load_tasks_from_sheet(sheet_name):
    try:
        sheet = client.open(sheet_name).sheet1
        tasks = sheet.get_all_records()
        for task in tasks:
            location = task["Location"]
            description = task["Description"]
            points = task["Points"]
            cursor.execute("INSERT INTO tasks (location, description, points) VALUES (?, ?, ?)", (location, description, points))
        db.commit()
        return True
    except Exception as e:
        print(f"Error loading tasks from sheet: {e}")
        return False

# Helper functions

async def send_to_channel_review(interaction: discord.Interaction ,channel_id: int, task_id:int,  photo_url:str):
    await interaction.response.defer()
    channel = bot.get_channel(channel_id)
    task_description = get_task_by_id(task_id)
    await interaction.followup.send(task_description)
    await channel.send(photo_url)

async def get_task_by_id(task_id):
    cursor.execute("SELECT id, location, description, points FROM tasks WHERE id = ?", (task_id,))
    result = cursor.fetchone()
    return result


def get_tasks(location):
    cursor.execute("SELECT id, description, points FROM tasks WHERE location = ?", (location,))
    return cursor.fetchall()

def update_points(team_id, points):
    cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (points, team_id))
    db.commit()
def add_user_to_team(user_id, team_id):
    cursor.execute("INSERT INTO users (discord_id, team_id) VALUES (?, ?)", (user_id, team_id))
    db.commit()

# Pagination class
class TaskPaginator:
    def __init__(self, tasks, per_page=1):
        self.tasks = tasks
        self.per_page = per_page
        self.page = 0

    def get_page(self):
        start = self.page * self.per_page
        end = start + self.per_page
        return self.tasks[start:end]

    def has_next(self):
        return (self.page + 1) * self.per_page < len(self.tasks)

    def has_previous(self):
        return self.page > 0

    def next_page(self):
        if self.has_next():
            self.page += 1

    def previous_page(self):
        if self.has_previous():
            self.page -= 1

# Event listeners
@bot.event
async def setup_hook():
    try:
        synced = await bot.tree.sync()
        print("Synced Commands: " + str(synced))
    except Exception as e:
        print("Booooof Something went wrong")

@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")

# Slash commands
@tree.command(name="create_team", description="Create a new team")
@app_commands.describe(team_name="The name of the team")
async def create_team(interaction: discord.Interaction, team_name: str, user1: discord.Member, user2: discord.Member = None, user3: discord.Member = None, user4: discord.Member = None, user5: discord.Member = None, user6: discord.Member = None):
    await interaction.response.defer()
    try:
        if not any(role.name == "Game Admin" for role in interaction.user.roles):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
    except AttributeError:
        await interaction.followup.send("Can't use Command in DM", ephemeral=True)
        return
    users = [user1, user2, user3, user4, user5, user6]

    cursor.execute("INSERT INTO teams (name, points) VALUES (?, 0)", (team_name,))
    team_id = cursor.lastrowid
    for user in users:
        if user is None:
            break
        add_user_to_team(user.id, team_id)

    db.commit()
    await interaction.followup.send(f"Team '{team_name}' created successfully!")

@tree.command(name="start_game", description="Start the game for a specific location")
@app_commands.describe(location="The location ID to start")
async def start_game(interaction: discord.Interaction, location: int):
    await interaction.response.defer()
    try:
        if not any(role.name == "Game Admin" for role in interaction.user.roles):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
    except AttributeError:
        await interaction.followup.send("Can't use Command in DM", ephemeral=True)
        return
    tasks = get_tasks(location)
    if not tasks:
        await interaction.followup.send("No tasks available for this location.")
        return
    global Game_status
    Game_status = location
    await interaction.followup.send(f"Game started for location {location}!")

@tree.command(name="load_tasks", description="Load tasks from a Google Sheet")
@app_commands.describe(sheet_name="The name of the Google Sheet to load tasks from")
async def load_tasks(interaction: discord.Interaction, sheet_name: str):
    await interaction.response.defer()
    try:
        if not any(role.name == "Game Admin" for role in interaction.user.roles):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
    except AttributeError:
        await interaction.followup.send("Can't use Command in DM", ephemeral=True)
        return

    success = load_tasks_from_sheet(sheet_name)
    if success:
        await interaction.followup.send(f"Tasks loaded successfully from {sheet_name}.")
    else:
        await interaction.followup.send(f"Failed to load tasks from {sheet_name}.")



@tree.command(name="my_tasks", description="View your tasks for the current location")
async def my_tasks(interaction: discord.Interaction):
    await interaction.response.defer()

    user_id = interaction.user.id
    cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        await interaction.followup.send("You are not assigned to a team!", ephemeral=True)
        return

    team_id = result[0]
    global Game_status
    tasks = get_tasks(Game_status)

    if not tasks:
        await interaction.followup.send("No tasks available for your location.", ephemeral=True)
        return

    paginator = TaskPaginator(tasks)
    embed = discord.Embed(title="Tasks", description="Here are your tasks:")
    for task in paginator.get_page():
        embed.add_field(name=f"Task ID: {task[0]}", value=f"{task[1]} ({task[2]} points)", inline=False)

    # Define navigation buttons
    class TaskView(View):
        def __init__(self):
            super().__init__()
            self.current_task_index = 0

        @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary)
        async def previous_button(self, interaction: discord.Interaction, button: Button):
            if paginator.has_previous():
                paginator.previous_page()
                embed.clear_fields()
                for task in paginator.get_page():
                    embed.add_field(name=f"Task ID: {task[0]}", value=f"{task[1]} ({task[2]} points)", inline=False)
                await interaction.response.edit_message(embed=embed)

        @discord.ui.button(label="✅ Submit", style=discord.ButtonStyle.primary)
        async def submit_button(self, interaction: discord.Interaction, button: Button):
            current_task = paginator.get_page()[self.current_task_index]
            task_id = current_task[0]
            await submit_task(interaction, task_id)

        @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
        async def next_button(self, interaction: discord.Interaction, button: Button):
            if paginator.has_next():
                paginator.next_page()
                embed.clear_fields()
                for task in paginator.get_page():
                    embed.add_field(name=f"Task ID: {task[0]}", value=f"{task[1]} ({task[2]} points)", inline=False)
                await interaction.response.edit_message(embed=embed)

    view = TaskView()
    await interaction.followup.send(embed=embed, view=view)




async def submit_task(interaction: discord.Interaction, task_id: int):
    user_id = interaction.user.id
    cursor.execute("SELECT id, team_id FROM users WHERE discord_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        await interaction.followup.send("You are not registered!", ephemeral=True)
        return

    user_db_id, team_id = result

    # Check for existing submission
    cursor.execute("SELECT status, message_id FROM submissions WHERE team_id = ? AND task_id = ?", (team_id, task_id))
    existing_submission = cursor.fetchone()

    action_message = "submitted"
    overwrite = False
    message_id = None

    if existing_submission:
        status, message_id = existing_submission
        if status == "Accepted":
            await interaction.response.send_message(
                "Your submission for this task has already been accepted. Resubmission is not allowed.", ephemeral=True
            )
            return
        elif status in ("Pending", "Denied"):
            overwrite = True
            action_message = "resubmitted"

        if overwrite and message_id:
            try:
                # Fetch the old message from the database
                channel_id = config.get('moderator_channel')
                channel = interaction.client.get_channel(channel_id)
                if channel:
                    old_message = await channel.fetch_message(message_id)
                    if old_message:
                        # Disable all buttons in the view
                        for component in old_message.components:
                            for item in component.children:
                                item.disabled = True  # Disable the button

                        # Create a new view with updated components
                        updated_view = View()
                        for component in old_message.components:
                            for item in component.children:
                                updated_view.add_item(item)

                        # Edit the old message to update the view
                        await old_message.edit(view=updated_view)
            except Exception as e:
                print(f"Error fetching or editing old message: {e}")

    # Insert or update submission
    cursor.execute(
        """
        INSERT INTO submissions (team_id, task_id, status, message_id, photo_url)
        VALUES (?, ?, 'Pending', NULL, NULL)
        ON CONFLICT(team_id, task_id)
        DO UPDATE SET status = 'Pending', photo_url = NULL
        """,
        (team_id, task_id)
    )
    db.commit()

    # Create and send the new submission view
    submit_button = Button(label="Submit Photo", style=discord.ButtonStyle.primary)

    async def handle_photo_submission(interaction: discord.Interaction):
        submitter = await interaction.client.fetch_user(user_id)
        await interaction.response.send_message(f"Please upload your photo to be {action_message}:", ephemeral=True)

        def check(msg):
            return msg.author == interaction.user and len(msg.attachments) > 0

        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=300)  # 5-minute timeout
        except asyncio.TimeoutError:
            await interaction.followup.send("Photo submission timed out. Please try again.", ephemeral=True)
            return

        photo_url = msg.attachments[0].url

        # Update the photo URL in the database
        cursor.execute(
            "UPDATE submissions SET photo_url = ?, status = 'Pending' WHERE team_id = ? AND task_id = ?",
            (photo_url, team_id, task_id)
        )
        db.commit()
        await interaction.followup.send("Photo Submission Complete!", ephemeral=True)

        # Notify the server channel with Accept and Deny buttons
        channel_id = config.get('moderator_channel')
        channel = interaction.client.get_channel(channel_id)
        if channel:
            embed = discord.Embed(
                title=f"Task {action_message.capitalize()}",
                description=f"New {action_message} for task {task_id}"
            )
            embed.add_field(name="Team ID", value=team_id, inline=True)
            embed.add_field(name="Submitted By", value=interaction.user.mention, inline=True)

            def is_video_url(url):
                response = requests.head(url)
                content_type = response.headers.get('Content-Type', '')
                return 'video' in content_type
            is_video = is_video_url(photo_url)
            if is_video:
                embed.add_field(name="Video Submission",
                                value=photo_url, inline=True)

            else:
                embed.set_image(url=photo_url)



            review_view = View()

            # Accept Button
            async def accept_callback(interaction: discord.Interaction):
                if not "Game Admin" in interaction.user.roles:
                    await interaction.response.send_message("You are not authorized to perform this action.",
                                                            ephemeral=True)
                    return

                cursor.execute("UPDATE submissions SET status = 'Accepted' WHERE team_id = ? AND task_id = ?",
                               (team_id, task_id))
                id, location, description, points = await get_task_by_id(task_id)
                cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (points, team_id))
                db.commit()
                await interaction.response.send_message("Submission accepted and points added.", ephemeral=True)

                # Disable all buttons
                for child in review_view.children:
                    child.disabled = True
                await interaction.message.edit(view=review_view)

            accept_button = Button(label="Accept", style=discord.ButtonStyle.success)
            accept_button.callback = accept_callback
            review_view.add_item(accept_button)

            # Deny Button
            async def deny_callback(interaction: discord.Interaction):
                if not any(role.name == "Game Admin" for role in interaction.user.roles):
                    await interaction.response.send_message("You are not authorized to perform this action.",
                                                            ephemeral=True)
                    return

                await interaction.response.send_message("Please provide a reason for denial:", ephemeral=True)

                def check(msg):
                    return msg.author == interaction.user

                try:
                    msg = await interaction.client.wait_for("message", check=check, timeout=300)  # 5-minute timeout
                except asyncio.TimeoutError:
                    await interaction.followup.send("Timed out while waiting for denial reason.", ephemeral=True)
                    return

                denial_reason = msg.content
                cursor.execute("UPDATE submissions SET status = 'Denied' WHERE team_id = ? AND task_id = ?",
                               (team_id, task_id))
                db.commit()

                await submitter.send(f"Submission denied for Task ID {task_id}. Reason: {denial_reason}")
                await interaction.followup.send("Submission denied.", ephemeral=True)

                # Disable all buttons
                for child in review_view.children:
                    child.disabled = True
                await interaction.message.edit(view=review_view)

            deny_button = Button(label="Deny", style=discord.ButtonStyle.danger)
            deny_button.callback = deny_callback
            review_view.add_item(deny_button)

            try:
                if is_video:
                    await channel.send(photo_url)
                new_message = await channel.send(embed=embed, view=review_view)

                # Update the new message ID in the database
                cursor.execute(
                    "UPDATE submissions SET message_id = ? WHERE team_id = ? AND task_id = ?",
                    (new_message.id, team_id, task_id)
                )
                db.commit()
            except discord.NotFound:
                await interaction.followup.send("Failed to post the message for review.", ephemeral=True)

    submit_button.callback = handle_photo_submission

    main_view = View()
    main_view.add_item(submit_button)

    await interaction.response.send_message(f"Task ready for {action_message}. Please submit your photo:", view=main_view, ephemeral=True)

@tree.command(name="toggle_leaderboard", description="Toggle the visibility of the leaderboard")
async def toggle_leaderboard(interaction: discord.Interaction):
    if "Game Admin" in interaction.user.roles:
        await interaction.response.send_message("You are not authorized to toggle the leaderboard visibility.", ephemeral=True)
        return

    global leaderboard_visible

    # Toggle the visibility state
    leaderboard_visible = not leaderboard_visible

    # Inform the admin about the current visibility
    if leaderboard_visible:
        await interaction.response.send_message("The leaderboard is now visible to teams.", ephemeral=True)
    else:
        await interaction.response.send_message("The leaderboard is now hidden from teams.", ephemeral=True)
def fetch_leaderboard():
    # Get all teams sorted by points in descending order
    cursor.execute("SELECT name, points FROM teams ORDER BY points DESC")
    leaderboard = cursor.fetchall()
    return leaderboard


@tree.command(name="leaderboard", description="View the current leaderboard")
async def leaderboard(interaction: discord.Interaction):
    # Ensure only the Game Admin can view the leaderboard
    await interaction.response.defer()
    leaderboard = fetch_leaderboard()

    if not leaderboard:
        await interaction.followup.send("No teams have been registered yet.", ephemeral=True)
        return
    if not leaderboard_visible:
        await interaction.followup.send("Leaderboard as been Disabled by game admin.", ephemeral=True)
        return
    # Create the embed for the leaderboard
    embed = discord.Embed(
        title="Leaderboard",
        description="Here are the top teams for the current game!",
        color=discord.Color.blurple()  # You can change the embed color
    )

    # Add the leaderboard content to the embed
    for idx, (team_name, points) in enumerate(leaderboard):
        # Emoji bar based on the team's points
        bar = "🔹" * (points // 10)  # You can scale this based on the points
        # Color code the top 3 positions
        if idx == 0:  # First place: Gold
            embed.add_field(name=f"🥇 {team_name}", value=f"{points} points {bar}", inline=False)
        elif idx == 1:  # Second place: Silver
            embed.add_field(name=f"🥈 {team_name}", value=f"{points} points {bar}", inline=False)
        elif idx == 2:  # Third place: Bronze
            embed.add_field(name=f"🥉 {team_name}", value=f"{points} points {bar}", inline=False)
        else:  # Other positions
            embed.add_field(name=team_name, value=f"{points} points {bar}", inline=False)

    # Send the leaderboard embed
    await interaction.followup.send(embed=embed)

bot.run(config['bot_token'])

