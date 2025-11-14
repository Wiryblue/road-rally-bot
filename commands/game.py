import asyncio
import discord
from discord import app_commands
from discord.ui import Button, View
from database import cursor, db
from config import config
from utils import reject_if_not_admin, is_admin
from sheets import load_tasks_from_sheet


Game_status = 0  # current active location


# ---------- Helper ----------
def get_tasks_with_status(team_id, location):
    cursor.execute("""
        SELECT t.id, t.description, t.points,
               COALESCE(s.status, 'Not Submitted') AS status
        FROM tasks t
        LEFT JOIN submissions s
               ON t.id = s.task_id AND s.team_id = ?
        WHERE t.location = ?
    """, (team_id, location))
    return cursor.fetchall()


async def get_task_by_id(task_id):
    cursor.execute("SELECT id, location, description, points, judge FROM tasks WHERE id = ?", (task_id,))
    return cursor.fetchone()


async def _notify_user(client: discord.Client, user_id: int, message: str) -> None:
    """Send a DM to a user, ignoring failures."""
    try:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
        if user:
            await user.send(message)
    except Exception as exc:
        print(f"Failed to DM user {user_id}: {exc}")


async def disable_view_buttons(view: View, message: discord.Message) -> None:
    """Disable every button in a view and refresh the originating message."""
    for child in view.children:
        child.disabled = True
    await message.edit(view=view)


async def disable_message_components(message: discord.Message) -> None:
    """Disable the interactive components that were attached to a stored message."""
    try:
        view = View.from_message(message)
    except Exception as exc:
        print(f"Unable to reconstruct view for message {message.id}: {exc}")
        return

    await disable_view_buttons(view, message)


def submission_was_already_accepted(team_id: int, task_id: int) -> bool:
    cursor.execute(
        "SELECT status FROM submissions WHERE team_id=? AND task_id=?",
        (team_id, task_id),
    )
    row = cursor.fetchone()
    return bool(row and row[0] == "Accepted")


async def post_to_spectator(interaction, team_id, task_desc, photo_url, points):
    """Repost accepted photo to highlights channel."""
    channel_id = config.get("spectator_channel")
    if not channel_id:
        return
    channel = interaction.client.get_channel(channel_id)
    if not channel:
        return

    cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
    team_name = cursor.fetchone()
    team_name = team_name[0] if team_name else f"Team {team_id}"

    embed = discord.Embed(
        title="🏁 Task Completed!",
        description=f"> {task_desc}",
        color=discord.Color.green()
    )
    embed.add_field(name="Team", value=team_name, inline=False)
    embed.add_field(name="Points", value=str(points))
    embed.set_image(url=photo_url)
    embed.set_footer(text=f"Awarded by {interaction.user.name}")

    await channel.send(embed=embed)


async def disable_previous_review_message(channel: discord.abc.Messageable, message_id: int | None) -> None:
    """Fetch a stored moderator message and disable its buttons if it exists."""
    if not message_id:
        return

    try:
        old_message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        print(f"Unable to fetch previous review message {message_id}: {exc}")
        return

    await disable_message_components(old_message)


# ---------- Modal ----------
class ScoreModal(discord.ui.Modal, title="Enter Task Score"):
    def __init__(
        self,
        team_id: int,
        task_id: int,
        max_points: int,
        task_desc: str,
        photo_url: str,
        submitter_id: int,
        review_view: View,
        review_message: discord.Message,
    ):
        super().__init__()
        self.team_id = team_id
        self.task_id = task_id
        self.max_points = max_points
        self.task_desc = task_desc
        self.photo_url = photo_url
        self.submitter_id = submitter_id
        self.review_view = review_view
        self.review_message = review_message

        self.score = discord.ui.TextInput(label=f"Score (max {max_points})", required=True)
        self.add_item(self.score)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            awarded = int(self.score.value)
        except ValueError:
            await interaction.response.send_message("Invalid score.", ephemeral=True)
            return

        if awarded > self.max_points:
            await interaction.response.send_message(f"Score cannot exceed {self.max_points}.", ephemeral=True)
            return

        if submission_was_already_accepted(self.team_id, self.task_id):
            await interaction.response.send_message(
                "This submission was already accepted.", ephemeral=True
            )
            await disable_view_buttons(self.review_view, self.review_message)
            return

        cursor.execute("UPDATE submissions SET status='Accepted' WHERE team_id=? AND task_id=?", (self.team_id, self.task_id))
        cursor.execute("UPDATE teams SET points=points+? WHERE id=?", (awarded, self.team_id))
        db.commit()

        await interaction.response.send_message(f"✅ Task accepted ({awarded} pts).", ephemeral=True)
        await _notify_user(
            interaction.client,
            self.submitter_id,
            (
                f"✅ Your submission for '{self.task_desc}' has been accepted for "
                f"{awarded} point{'s' if awarded != 1 else ''}!"
            ),
        )
        await post_to_spectator(interaction, self.team_id, self.task_desc, self.photo_url, awarded)

        await disable_view_buttons(self.review_view, self.review_message)


# ---------- Command Setup ----------
def setup_game(tree: app_commands.CommandTree):

    @tree.command(name="start_game", description="Start the game for a specific location")
    @app_commands.describe(location="The location ID to start")
    async def start_game(interaction: discord.Interaction, location: int):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        cursor.execute("SELECT 1 FROM tasks WHERE location = ?", (location,))
        if cursor.fetchone() is None:
            await interaction.followup.send("No tasks available for this location.", ephemeral=True)
            return

        global Game_status
        Game_status = location
        await interaction.followup.send(f"Game started for location {location}!", ephemeral=True)

        cursor.execute("SELECT DISTINCT discord_id FROM users")
        user_ids = cursor.fetchall()
        instruction_message = (
            f"Hello!\n\nThe game has started for location {location}!\n\n"
            "Use `/my_tasks` to view your tasks.\n\n"
            "When you're ready to submit a task, use `/submit task_id:<your task id>` and follow the prompts to upload your photo.\n\n"
            "You can also check out the leaderboard using `/leaderboard` to see how your team is doing.\n\n"
            "Good luck!"
        )

        for (discord_id,) in user_ids:
            try:
                user = await interaction.client.fetch_user(discord_id)
                await user.send(instruction_message)
            except Exception as exc:
                print(f"Failed to DM user {discord_id}: {exc}")

    @tree.command(name="load_tasks", description="Load tasks from a Google Sheet")
    @app_commands.describe(sheet_name="The name of the Google Sheet to load tasks from")
    async def load_tasks(interaction: discord.Interaction, sheet_name: str):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        success = load_tasks_from_sheet(sheet_name)
        message = (
            f"Tasks loaded successfully from {sheet_name}."
            if success
            else f"Failed to load tasks from {sheet_name}."
        )
        await interaction.followup.send(message, ephemeral=True)

    # --- My Tasks ---
    @tree.command(name="my_tasks", description="View your tasks for the current location")
    async def my_tasks(interaction: discord.Interaction):
        user_id = interaction.user.id
        cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (user_id,))
        result = cursor.fetchone()
        if not result:
            await interaction.response.send_message("You're not on a team.", ephemeral=True)
            return

        team_id = result[0]
        tasks = get_tasks_with_status(team_id, Game_status)
        if not tasks:
            await interaction.response.send_message("No tasks for your location.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Tasks", color=discord.Color.blurple())
        for tid, desc, pts, status in tasks:
            icon = "✅" if status == "Accepted" else "🟡" if status == "Pending" else "❌"
            embed.add_field(name=f"{icon} Task {tid}", value=f"{desc} ({pts} pts) - {status}", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Submit Photo ---
    @tree.command(name="submit", description="Submit a photo for a task ID")
    @app_commands.describe(task_id="Task ID")
    async def submit(interaction: discord.Interaction, task_id: int):
        await interaction.response.defer(ephemeral=True)

        # identify team
        cursor.execute("SELECT team_id FROM users WHERE discord_id=?", (interaction.user.id,))
        r = cursor.fetchone()
        if not r:
            await interaction.followup.send("You're not registered.", ephemeral=True)
            return
        team_id = r[0]

        task_info = await get_task_by_id(task_id)
        if not task_info:
            await interaction.followup.send("Task not found.", ephemeral=True)
            return
        _, loc, desc, pts, judge = task_info
        if loc != Game_status:
            await interaction.followup.send("That task isn't active.", ephemeral=True)
            return

        cursor.execute(
            "SELECT status, message_id FROM submissions WHERE team_id=? AND task_id=?",
            (team_id, task_id),
        )
        existing_submission = cursor.fetchone()
        is_reupload = False
        previous_message_id = None
        if existing_submission:
            status, previous_message_id = existing_submission
            if status == "Accepted":
                await interaction.followup.send("✅ This task has already been accepted for your team.", ephemeral=True)
                return
            if status == "Pending":
                is_reupload = True

        submitter_id = interaction.user.id

        cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
        team_row = cursor.fetchone()
        team_name = team_row[0] if team_row else f"Team {team_id}"

        mod_channel_id = config.get("moderator_channel")
        channel = interaction.client.get_channel(mod_channel_id) if mod_channel_id else None
        if not channel:
            await interaction.followup.send("Moderator channel not found.", ephemeral=True)
            return

        prompt = "Please upload a photo for this task."
        if is_reupload:
            prompt = (
                "⏳ You already have a pending submission. Uploading a new photo will replace it "
                "and disable the old review buttons."
            )

        await interaction.followup.send(prompt, ephemeral=True)

        def check(m): return m.author == interaction.user and len(m.attachments) > 0
        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Timeout. Try again.", ephemeral=True)
            return

        photo_url = msg.attachments[0].url

        if is_reupload and previous_message_id:
            await disable_previous_review_message(channel, previous_message_id)

        embed = discord.Embed(title="📸 New Submission", description=f"Task: {desc}", color=discord.Color.orange())
        embed.add_field(name="Team ID", value=str(team_id))
        embed.add_field(name="Team", value=team_name, inline=False)
        embed.add_field(name="Submitted By", value=interaction.user.mention)
        embed.set_image(url=photo_url)

        review_view = View(timeout=None)

        # Accept Button
        async def accept_callback(btn_inter: discord.Interaction):
            if not is_admin(btn_inter.user):
                await btn_inter.response.send_message("Not authorized.", ephemeral=True)
                return

            review_message = btn_inter.message
            if review_message is None:
                await btn_inter.response.send_message("Unable to locate the review message.", ephemeral=True)
                return

            if judge == 1:
                await btn_inter.response.send_modal(
                    ScoreModal(
                        team_id,
                        task_id,
                        pts,
                        desc,
                        photo_url,
                        submitter_id,
                        review_view,
                        review_message,
                    )
                )
            else:
                if submission_was_already_accepted(team_id, task_id):
                    await btn_inter.response.send_message(
                        "This submission was already accepted.", ephemeral=True
                    )
                    await disable_view_buttons(review_view, review_message)
                    return
                cursor.execute("UPDATE submissions SET status='Accepted' WHERE team_id=? AND task_id=?", (team_id, task_id))
                cursor.execute("UPDATE teams SET points=points+? WHERE id=?", (pts, team_id))
                db.commit()
                await btn_inter.response.send_message("✅ Task accepted.", ephemeral=True)
                await _notify_user(
                    btn_inter.client,
                    submitter_id,
                    (
                        f"✅ Your submission for '{desc}' has been accepted for "
                        f"{pts} point{'s' if pts != 1 else ''}!"
                    ),
                )
                await post_to_spectator(btn_inter, team_id, desc, photo_url, pts)
                await disable_view_buttons(review_view, review_message)

        accept_button = Button(label="Accept", style=discord.ButtonStyle.success)
        accept_button.callback = accept_callback
        review_view.add_item(accept_button)

        # Deny Button
        async def deny_callback(btn_inter: discord.Interaction):
            if not is_admin(btn_inter.user):
                await btn_inter.response.send_message("Not authorized.", ephemeral=True)
                return

            review_message = btn_inter.message
            if review_message is None:
                await btn_inter.response.send_message("Unable to locate the review message.", ephemeral=True)
                return

            class DenyModal(discord.ui.Modal, title="Reason for Denial"):
                def __init__(self):
                    super().__init__()
                    self.reason = discord.ui.TextInput(label="Reason", required=True)
                    self.add_item(self.reason)

                async def on_submit(self, modal_inter: discord.Interaction):
                    cursor.execute("UPDATE submissions SET status='Denied' WHERE team_id=? AND task_id=?", (team_id, task_id))
                    db.commit()
                    await modal_inter.response.send_message("Submission denied.", ephemeral=True)
                    await _notify_user(
                        modal_inter.client,
                        submitter_id,
                        f"❌ Your submission for '{desc}' was denied. Reason: {self.reason.value}",
                    )
                    await disable_view_buttons(review_view, review_message)

            await btn_inter.response.send_modal(DenyModal())

        deny_button = Button(label="Deny", style=discord.ButtonStyle.danger)
        deny_button.callback = deny_callback
        review_view.add_item(deny_button)

        review_message = await channel.send(embed=embed, view=review_view)

        cursor.execute(
            """
            INSERT INTO submissions(team_id, task_id, status, photo_url, message_id)
            VALUES (?, ?, 'Pending', ?, ?)
            ON CONFLICT(team_id, task_id)
            DO UPDATE SET status='Pending', photo_url=excluded.photo_url, message_id=excluded.message_id
            """,
            (team_id, task_id, photo_url, review_message.id),
        )
        db.commit()

        await interaction.followup.send("📩 Submission sent for review!", ephemeral=True)
