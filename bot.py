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
cursor.execute('''CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, location INTEGER, description TEXT, points INTEGER, judge INTEGER)''')
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
            # If Judge is 1 then on accept it uses the point total as the max total
            # and when the accept button is pressed it prompts for a score; if higher than the max it reprompts.
            judge = task["Judge"]
            cursor.execute("INSERT INTO tasks (location, description, points, judge) VALUES (?, ?, ?, ?)", (location, description, points, judge))
        db.commit()
        return True
    except Exception as e:
        print(f"Error loading tasks from sheet: {e}")
        return False

# New helper: get tasks along with submission status for a team and location.
def get_tasks_with_status(team_id, location):
    cursor.execute(
        """
        SELECT t.id, t.description, t.points, COALESCE(s.status, 'Not Submitted') as status
        FROM tasks t
        LEFT JOIN submissions s ON t.id = s.task_id AND s.team_id = ?
        WHERE t.location = ?
        """, (team_id, location)
    )
    return cursor.fetchall()

async def send_to_channel_review(interaction: discord.Interaction, channel_id: int, task_id: int, photo_url: str):
    await interaction.response.defer()
    channel = bot.get_channel(channel_id)
    task_description = await get_task_by_id(task_id)
    await interaction.followup.send(task_description)
    await channel.send(photo_url)

# Modified to include the judge column.
async def get_task_by_id(task_id):
    cursor.execute("SELECT id, location, description, points, judge FROM tasks WHERE id = ?", (task_id,))
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
    # Check if the user invoking this command has the Game Admin role
    try:
        if interaction.guild is None:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
        if not any(role.name == "Game Admin" for role in interaction.user.roles):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
    except AttributeError:
        await interaction.followup.send("Can't use Command in DM", ephemeral=True)
        return
    users = [user1, user2, user3, user4, user5, user6]

    # Create the team first.
    cursor.execute("INSERT INTO teams (name, points) VALUES (?, 0)", (team_name,))
    team_id = cursor.lastrowid

    # Check each provided user and only add them if they're not already on a team.
    duplicate_users = []
    valid_users = []
    for user in users:
        if user is None:
            break
        cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (user.id,))
        if cursor.fetchone():
            duplicate_users.append(user.name)
        else:
            add_user_to_team(user.id, team_id)
            valid_users.append(user.name)

    db.commit()
    response = f"Team '{team_name}' created successfully!\nAdded members: {', '.join(valid_users) if valid_users else 'None'}."
    if duplicate_users:
        response += f"\nSkipped (already on a team): {', '.join(duplicate_users)}."
    await interaction.followup.send(response)

@tree.command(name="start_game", description="Start the game for a specific location")
@app_commands.describe(location="The location ID to start")
async def start_game(interaction: discord.Interaction, location: int):
    await interaction.response.defer()
    try:
        if interaction.guild is None:
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
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

    # DM all team members with instructions
    cursor.execute("SELECT DISTINCT discord_id FROM users")
    user_ids = cursor.fetchall()  # List of tuples like [(discord_id,), (discord_id,), ...]
    instruction_message = (
        f"Hello!\n\nThe game has started for location {location}!\n\n"
        "Use `/my_tasks` to view your tasks.\n\n"
        "When you're ready to submit a task, use `/submit task_id:<your task id>` and follow the prompts to upload your photo.\n\n"
        "You can also check out the leaderboard using `/leaderboard` to see how your team is doing.\n\n"
        "Good luck!"
    )

    for (discord_id,) in user_ids:
        try:
            user = await bot.fetch_user(discord_id)
            await user.send(instruction_message)
        except Exception as e:
            print(f"Failed to DM user {discord_id}: {e}")

@tree.command(name="load_tasks", description="Load tasks from a Google Sheet")
@app_commands.describe(sheet_name="The name of the Google Sheet to load tasks from")
async def load_tasks(interaction: discord.Interaction, sheet_name: str):
    await interaction.response.defer()
    try:
        if interaction.guild is None:
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
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

@tree.command(name="my_tasks", description="View your tasks for the current location along with their completion status")
async def my_tasks(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        await interaction.followup.send("You are not assigned to a team!", ephemeral=True)
        return

    team_id = result[0]
    global Game_status
    tasks = get_tasks_with_status(team_id, Game_status)

    if not tasks:
        await interaction.followup.send("No tasks available for your location.", ephemeral=True)
        return

    embed = discord.Embed(title="Tasks", description="Here are your tasks and their statuses:")
    all_done = True
    for task in tasks:
        task_id, description, points, status = task
        # Map status to icon:
        if status == "Accepted":
            emoji = "✅"  # Done
        elif status == "Pending":
            emoji = "🟡"  # Pending grading
            all_done = False
        else:
            emoji = "❌"  # Not done
            all_done = False
        embed.add_field(name=f"Task ID: {task_id}", value=f"{description} ({points} points) - Status: {emoji}", inline=False)

    if all_done:
        embed.add_field(
            name="Fanfare!",
            value="🎉🎊🏆 Congratulations! All tasks for this location are complete! Enjoy your victory! 🎆✨",
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="submit", description="Submit your task photo using task ID")
@app_commands.describe(task_id="The ID of the task you are submitting for")
async def submit(interaction: discord.Interaction, task_id: int):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    cursor.execute("SELECT id, team_id FROM users WHERE discord_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        await interaction.followup.send("You are not registered!", ephemeral=True)
        return

    user_db_id, team_id = result

    # Retrieve the task and check if it's for the current game location.
    task_info = await get_task_by_id(task_id)
    if not task_info:
        await interaction.followup.send("Task not found.", ephemeral=True)
        return
    # *** Modified: Unpack the task description ***
    _, task_location, task_description, _, _ = task_info
    if task_location != Game_status:
        await interaction.followup.send("This task is not for the current game location.", ephemeral=True)
        return

    # Check for an existing submission.
    cursor.execute("SELECT status, message_id FROM submissions WHERE team_id = ? AND task_id = ?", (team_id, task_id))
    existing_submission = cursor.fetchone()

    action_message = "submitted"
    overwrite = False
    message_id = None

    if existing_submission:
        status, message_id = existing_submission
        if status == "Accepted":
            await interaction.followup.send(
                "Your submission for this task has already been accepted. Resubmission is not allowed.", ephemeral=True
            )
            return
        elif status in ("Pending", "Denied"):
            overwrite = True
            action_message = "resubmitted"

        if overwrite and message_id:
            try:
                # Fetch the old message from the moderator channel.
                channel_id = config.get('moderator_channel')
                channel = interaction.client.get_channel(channel_id)
                if channel:
                    old_message = await channel.fetch_message(message_id)
                    if old_message:
                        # Disable all buttons in the view.
                        for component in old_message.components:
                            for item in component.children:
                                item.disabled = True
                        # Create a new view with updated components.
                        updated_view = View()
                        for component in old_message.components:
                            for item in component.children:
                                updated_view.add_item(item)
                        # Edit the old message to update the view.
                        await old_message.edit(view=updated_view)
            except Exception as e:
                print(f"Error fetching or editing old message: {e}")

    # Insert or update the submission record.
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

    # Create first embed for Step 1 with a full-size image.
    instruction_embed1 = discord.Embed(
        title="Step 1: Message Icon",
        description="First, click on the **message icon** as shown below."
    )
    instruction_embed1.set_image(url="attachment://road_rally_instruction_pt1.jpg")

    # Create second embed for Step 2 with a full-size image.
    instruction_embed2 = discord.Embed(
        title="Step 2: Plus Icon",
        description="Then, click on the **plus icon** and simply send your photo. The bot will handle everything else."
    )
    instruction_embed2.set_image(url="attachment://road_rally_instruction_pt2.jpg")

    # Attach local files; ensure the file names and paths are correct.
    file1 = discord.File("road_rally_instruction_pt1.jpg", filename="road_rally_instruction_pt1.jpg")
    file2 = discord.File("road_rally_instruction_pt2.jpg", filename="road_rally_instruction_pt2.jpg")

    await interaction.followup.send(embeds=[instruction_embed1, instruction_embed2], ephemeral=True,
                                    files=[file1, file2])

    # Wait for the user to upload the photo.
    def check(msg):
        return msg.author == interaction.user and len(msg.attachments) > 0

    try:
        msg = await interaction.client.wait_for("message", check=check, timeout=300)  # 5-minute timeout.
    except asyncio.TimeoutError:
        await interaction.followup.send("Photo submission timed out. Please try again.", ephemeral=True)
        return

    photo_url = msg.attachments[0].url

    # Update the photo URL in the database.
    cursor.execute(
        "UPDATE submissions SET photo_url = ?, status = 'Pending' WHERE team_id = ? AND task_id = ?",
        (photo_url, team_id, task_id)
    )
    db.commit()
    await interaction.followup.send("Photo submission complete!", ephemeral=True)

    # Notify the moderator channel with Accept and Deny buttons.
    channel_id = config.get('moderator_channel')
    channel = interaction.client.get_channel(channel_id)
    if channel:
        # *** Modified: Using task_description instead of task_id in the embed ***
        embed = discord.Embed(
            title=f"Task {action_message.capitalize()}",
            description=f"New {action_message} for task: {task_description}"
        )
        embed.add_field(name="Team ID", value=team_id, inline=True)
        embed.add_field(name="Submitted By", value=interaction.user.mention, inline=True)

        def is_video_url(url):
            response = requests.head(url)
            content_type = response.headers.get('Content-Type', '')
            return 'video' in content_type

        is_video = is_video_url(photo_url)
        if is_video:
            embed.add_field(name="Video Submission", value=photo_url, inline=True)
        else:
            embed.set_image(url=photo_url)

        review_view = View()

        # Accept Button callback.
        async def accept_callback(interaction: discord.Interaction):
            if not any(role.name == "Game Admin" for role in interaction.user.roles):
                await interaction.response.send_message("You are not authorized to perform this action.", ephemeral=True)
                return

            cursor.execute("SELECT status FROM submissions WHERE team_id = ? AND task_id = ?", (team_id, task_id))
            current_status = cursor.fetchone()
            if current_status and current_status[0] == "Accepted":
                await interaction.response.send_message("This task is already marked as done.", ephemeral=True)
                for child in review_view.children:
                    child.disabled = True
                await interaction.message.edit(view=review_view)
                return

            task_info = await get_task_by_id(task_id)
            if not task_info:
                await interaction.response.send_message("Task not found.", ephemeral=True)
                return
            _id, location, description, points, judge = task_info

            if judge == 1:
                await interaction.response.send_message(f"Enter a score for this submission (max {points} points):", ephemeral=True)
                def check_score(msg):
                    return msg.author == interaction.user and msg.content.isdigit()
                valid_score = None
                while valid_score is None:
                    try:
                        score_msg = await interaction.client.wait_for("message", check=check_score, timeout=300)
                        score = int(score_msg.content)
                        if score > points:
                            await interaction.followup.send(f"Score cannot exceed max points ({points}). Please try again.", ephemeral=True)
                        else:
                            valid_score = score
                    except asyncio.TimeoutError:
                        await interaction.response.send_message("Score submission timed out.", ephemeral=True)
                        return
                awarded_points = valid_score
            else:
                awarded_points = points

            cursor.execute("UPDATE submissions SET status = 'Accepted' WHERE team_id = ? AND task_id = ?", (team_id, task_id))
            cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (awarded_points, team_id))
            db.commit()
            await interaction.response.send_message("Submission accepted and points added.", ephemeral=True)

            cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
            team_members = cursor.fetchall()
            for (discord_id,) in team_members:
                try:
                    team_user = await interaction.client.fetch_user(discord_id)
                    await team_user.send(
                        f"Team Update: Your submission for task ID {task_id} has been accepted! Your team has earned {awarded_points} points."
                    )
                except Exception as e:
                    print(f"Failed to send DM to user {discord_id}: {e}")

            for child in review_view.children:
                child.disabled = True
            await interaction.message.edit(view=review_view)

        accept_button = Button(label="Accept", style=discord.ButtonStyle.success)
        accept_button.callback = accept_callback
        review_view.add_item(accept_button)

        # Deny Button callback.
        async def deny_callback(interaction: discord.Interaction):
            if not any(role.name == "Game Admin" for role in interaction.user.roles):
                await interaction.response.send_message("You are not authorized to perform this action.", ephemeral=True)
                return
            cursor.execute("SELECT status FROM submissions WHERE team_id = ? AND task_id = ?", (team_id, task_id))
            current_status = cursor.fetchone()
            if current_status and (current_status[0] == "Accepted" or current_status[0] == "Denied"):
                await interaction.response.send_message("This task is already marked", ephemeral=True)
                for child in review_view.children:
                    child.disabled = True
                await interaction.message.edit(view=review_view)
                return

            await interaction.response.send_message("Please provide a reason for denial:", ephemeral=True)

            def check_deny(msg):
                return msg.author == interaction.user

            try:
                msg = await interaction.client.wait_for("message", check=check_deny, timeout=300)
            except asyncio.TimeoutError:
                await interaction.followup.send("Timed out while waiting for denial reason.", ephemeral=True)
                return

            denial_reason = msg.content
            cursor.execute("UPDATE submissions SET status = 'Denied' WHERE team_id = ? AND task_id = ?", (team_id, task_id))
            db.commit()

            submitter = await interaction.client.fetch_user(user_id)
            await submitter.send(f"Submission denied for Task ID {task_id}. Reason: {denial_reason}")
            await interaction.followup.send("Submission denied.", ephemeral=True)

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
            cursor.execute("UPDATE submissions SET message_id = ? WHERE team_id = ? AND task_id = ?",
                           (new_message.id, team_id, task_id))
            db.commit()
        except discord.NotFound:
            await interaction.followup.send("Failed to post the message for review.", ephemeral=True)

@tree.command(name="toggle_leaderboard", description="Toggle the visibility of the leaderboard")
async def toggle_leaderboard(interaction: discord.Interaction):
    # Check if the command is used in a DM
    if interaction.guild is None:
        await interaction.response.send_message("You are not authorized to toggle the leaderboard visibility.", ephemeral=True)
        return

    # Then check if the user has the "Game Admin" role
    if not any(role.name == "Game Admin" for role in interaction.user.roles):
        await interaction.response.send_message("You are not authorized to toggle the leaderboard visibility.", ephemeral=True)
        return

    global leaderboard_visible
    leaderboard_visible = not leaderboard_visible

    if leaderboard_visible:
        await interaction.response.send_message("The leaderboard is now visible to teams.", ephemeral=True)
    else:
        await interaction.response.send_message("The leaderboard is now hidden from teams.", ephemeral=True)

def fetch_leaderboard():
    cursor.execute("SELECT name, points FROM teams ORDER BY points DESC")
    leaderboard = cursor.fetchall()
    return leaderboard

@tree.command(name="leaderboard", description="View the current leaderboard")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    leaderboard_data = fetch_leaderboard()

    if not leaderboard_data:
        await interaction.followup.send("No teams have been registered yet.", ephemeral=True)
        return
    if not leaderboard_visible:
        await interaction.followup.send("Leaderboard has been disabled by game admin.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Leaderboard",
        description="Here are the top teams for the current game!",
        color=discord.Color.blurple()
    )

    for idx, (team_name, points) in enumerate(leaderboard_data):
        bar = "🔹" * (points // 10)
        if idx == 0:
            embed.add_field(name=f"🥇 {team_name}", value=f"{points} points {bar}", inline=False)
        elif idx == 1:
            embed.add_field(name=f"🥈 {team_name}", value=f"{points} points {bar}", inline=False)
        elif idx == 2:
            embed.add_field(name=f"🥉 {team_name}", value=f"{points} points {bar}", inline=False)
        else:
            embed.add_field(name=team_name, value=f"{points} points {bar}", inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="add_points", description="Add points to a team")
@app_commands.describe(team_id="The ID of the team", points="Points to add")
async def add_points(interaction: discord.Interaction, team_id: int, points: int):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=False)
        return
    if not any(role.name == "Game Admin" for role in interaction.user.roles):
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=False)
        return

    # Ask the Game Admin for a reason for adding points.
    await interaction.followup.send("Please provide a reason for adding points:", ephemeral=False)

    def check(msg):
        return msg.author == interaction.user

    try:
        msg = await interaction.client.wait_for("message", check=check, timeout=300)  # 5-minute timeout
    except asyncio.TimeoutError:
        await interaction.followup.send("Timed out while waiting for a reason.", ephemeral=False)
        return
    reason = msg.content

    # Update team points in the database.
    cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (points, team_id))
    db.commit()
    await interaction.followup.send(f"Added {points} points to team ID {team_id}. Reason: {reason}", ephemeral=False)

    # Notify all team members via DM.
    cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
    team_members = cursor.fetchall()
    for (discord_id,) in team_members:
        try:
            team_user = await interaction.client.fetch_user(discord_id)
            await team_user.send(f"Team Update: {points} points have been added to your team.\nReason: {reason}")
        except Exception as e:
            print(f"Failed to send DM to user {discord_id}: {e}")

@tree.command(name="remove_points", description="Remove points from a team")
@app_commands.describe(team_id="The ID of the team", points="Points to remove")
async def remove_points(interaction: discord.Interaction, team_id: int, points: int):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=False)
        return
    if not any(role.name == "Game Admin" for role in interaction.user.roles):
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=False)
        return

    # Ask Game Admin for a reason for the point deduction.
    await interaction.followup.send("Please provide a reason for removing points:", ephemeral=False)

    def check(msg):
        return msg.author == interaction.user

    try:
        msg = await interaction.client.wait_for("message", check=check, timeout=300)  # Wait up to 5 minutes.
    except asyncio.TimeoutError:
        await interaction.followup.send("Timed out while waiting for a reason.", ephemeral=True)
        return
    reason = msg.content

    # Update team points in the database.
    cursor.execute("UPDATE teams SET points = points - ? WHERE id = ?", (points, team_id))
    db.commit()
    await interaction.followup.send(f"Removed {points} points from team ID {team_id}. Reason: {reason}", ephemeral=True)

    # Notify all team members via DM.
    cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
    team_members = cursor.fetchall()
    for (discord_id,) in team_members:
        try:
            team_user = await interaction.client.fetch_user(discord_id)
            await team_user.send(f"Team Update: {points} points have been deducted from your team.\nReason: {reason}")
        except Exception as e:
            print(f"Failed to send DM to user {discord_id}: {e}")

@tree.command(name="list_teams", description="Private list of teams and their members")
async def list_teams(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
        return
    if not any(role.name == "Game Admin" for role in interaction.user.roles):
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
        return

    cursor.execute("SELECT id, name, points FROM teams")
    teams = cursor.fetchall()
    response_message = ""
    for team in teams:
        team_id, team_name, points = team
        cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
        users_in_team = cursor.fetchall()
        member_names = []
        for (discord_id,) in users_in_team:
            user_obj = interaction.client.get_user(discord_id)
            if user_obj is None:
                try:
                    user_obj = await interaction.client.fetch_user(discord_id)
                except Exception:
                    user_obj = None
            if user_obj:
                member_names.append(user_obj.name)
            else:
                member_names.append(f"Unknown({discord_id})")
        members_str = ", ".join(member_names) if member_names else "No members"
        response_message += f"**Team {team_name} (ID: {team_id}, Points: {points})**\nMembers: {members_str}\n\n"
    await interaction.followup.send(response_message, ephemeral=True)

@tree.command(name="rename_team", description="Rename an existing team (Game Admin only)")
@app_commands.describe(team_id="The ID of the team to rename", new_name="The new name for the team")
async def rename_team(interaction: discord.Interaction, team_id: int, new_name: str):
    await interaction.response.defer(ephemeral=True)
    # Only allow command if used in a guild by a Game Admin
    if interaction.guild is None or not any(role.name == "Game Admin" for role in interaction.user.roles):
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
        return

    # Check if the team exists
    cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
    team = cursor.fetchone()
    if team is None:
        await interaction.followup.send(f"Team with ID {team_id} not found.", ephemeral=True)
        return

    # Update the team's name in the database
    cursor.execute("UPDATE teams SET name = ? WHERE id = ?", (new_name, team_id))
    db.commit()
    await interaction.followup.send(f"Team renamed successfully to '{new_name}'.", ephemeral=True)


@tree.command(name="remove_team", description="Remove an existing team (Game Admin only)")
@app_commands.describe(team_id="The ID of the team to remove")
async def remove_team(interaction: discord.Interaction, team_id: int):
    await interaction.response.defer(ephemeral=True)
    # Only allow command if used in a guild by a Game Admin
    if interaction.guild is None or not any(role.name == "Game Admin" for role in interaction.user.roles):
        await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
        return

    # Check if the team exists
    cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
    team = cursor.fetchone()
    if team is None:
        await interaction.followup.send(f"Team with ID {team_id} not found.", ephemeral=True)
        return

    # Delete the team and any associated users from the database
    cursor.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    cursor.execute("DELETE FROM users WHERE team_id = ?", (team_id,))
    db.commit()
    await interaction.followup.send(f"Team with ID {team_id} has been removed.", ephemeral=True)






bot.run(config['bot_token'])
