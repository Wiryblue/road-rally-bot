import asyncio
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ui import View
from database import (
    cursor,
    db,
    get_active_location,
    get_team_id,
    get_team_member_ids,
    set_active_location,
)
from config import config
from utils import reject_if_not_admin, is_admin, dm_user, dm_many, chunk_lines
from sheets import load_tasks_from_sheet


VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}


# ---------- Helpers ----------
def get_tasks_with_status(team_id, location):
    cursor.execute("""
        SELECT t.id, t.description, t.points,
               COALESCE(s.status, 'Not Submitted') AS status
        FROM tasks t
        LEFT JOIN submissions s
               ON t.id = s.task_id AND s.team_id = ?
        WHERE (t.location = ? OR t.location = 0)
    """, (team_id, location))
    return cursor.fetchall()


def get_task_by_id(task_id):
    cursor.execute("SELECT id, location, description, points, judge FROM tasks WHERE id = ?", (task_id,))
    return cursor.fetchone()


def get_team_name(team_id):
    cursor.execute("SELECT name FROM teams WHERE id = ?", (team_id,))
    row = cursor.fetchone()
    return row[0] if row else f"Team {team_id}"


def _extension(attachment: discord.Attachment) -> str:
    return os.path.splitext(attachment.filename)[1].lower()


def is_video(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("video/") or _extension(attachment) in VIDEO_EXTENSIONS


def is_image(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("image/") or _extension(attachment) in IMAGE_EXTENSIONS


async def send_with_media(
    channel: discord.abc.Messageable,
    embed: discord.Embed,
    attachment: Optional[discord.Attachment],
    filename_stem: str,
    fallback_url: Optional[str] = None,
    view: Optional[View] = None,
) -> discord.Message:
    """Send an embed together with a photo or video.

    Embeds can only display images, so videos are uploaded as a file next to the embed.
    The media is re-uploaded rather than linked because Discord CDN links expire.
    If the file is too big for this server's upload limit, fall back to posting the link.
    """
    kwargs = {"view": view} if view is not None else {}

    if attachment is not None:
        video = is_video(attachment)
        filename = f"{filename_stem}{_extension(attachment) or ('.mp4' if video else '.png')}"
        media_embed = embed.copy()
        if video:
            media_embed.add_field(name="🎥 Video", value="Attached to this message.", inline=False)
        else:
            media_embed.set_image(url=f"attachment://{filename}")
        try:
            file = await attachment.to_file(filename=filename)
            return await channel.send(embed=media_embed, file=file, **kwargs)
        except discord.HTTPException as exc:
            # Usually 413 (over this server's upload limit); fall back to a link.
            print(f"Re-upload of {attachment.filename} failed ({exc}); falling back to link.")
            fallback_url = attachment.url

    if fallback_url:
        # A bare link on its own message lets Discord render a playable preview.
        await channel.send(fallback_url)
        embed.add_field(name="📎 Media", value=f"[Open original]({fallback_url})", inline=False)
    return await channel.send(embed=embed, **kwargs)


async def close_review_message(message: discord.Message, note: str, color: discord.Color) -> None:
    """Remove the buttons from a review message and stamp it with the outcome."""
    embed = message.embeds[0].copy() if message.embeds else discord.Embed()
    embed.color = color
    embed.set_footer(text=note)
    try:
        await message.edit(embed=embed, view=None)
    except discord.HTTPException as exc:
        print(f"Unable to update review message {message.id}: {exc}")


async def close_previous_review_message(channel, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        old_message = await channel.fetch_message(message_id)
    except discord.HTTPException as exc:
        print(f"Unable to fetch previous review message {message_id}: {exc}")
        return
    await close_review_message(old_message, "Superseded by a newer upload", discord.Color.dark_grey())


async def post_to_spectator(client: discord.Client, review_message: discord.Message, team_id, task_id, task_desc, points, awarded_by):
    """Repost accepted media to the highlights channel."""
    channel_id = config.get("spectator_channel")
    channel = client.get_channel(channel_id) if channel_id else None
    if not channel:
        return

    embed = discord.Embed(
        title="🏁 Task Completed!",
        description=f"> {task_desc}",
        color=discord.Color.green()
    )
    embed.add_field(name="Team", value=get_team_name(team_id), inline=False)
    embed.add_field(name="Points", value=str(points))
    embed.set_footer(text=f"Awarded by {awarded_by}")

    attachment = review_message.attachments[0] if review_message.attachments else None
    fallback_url = None
    if attachment is None:
        cursor.execute("SELECT photo_url FROM submissions WHERE team_id=? AND task_id=?", (team_id, task_id))
        row = cursor.fetchone()
        fallback_url = row[0] if row else None
    try:
        await send_with_media(channel, embed, attachment, f"highlight_team{team_id}", fallback_url=fallback_url)
    except discord.HTTPException as exc:
        print(f"Failed to post highlight: {exc}")


async def finish_accept(interaction: discord.Interaction, review_message: discord.Message,
                        team_id: int, task_id: int, desc: str, awarded: int) -> None:
    # Only a still-pending submission attached to *this* message can be accepted, so
    # double clicks, two admins, or stale buttons can never award points twice.
    cursor.execute(
        "UPDATE submissions SET status='Accepted' "
        "WHERE team_id=? AND task_id=? AND status='Pending' AND message_id=?",
        (team_id, task_id, review_message.id),
    )
    if cursor.rowcount == 0:
        db.commit()
        await interaction.response.send_message("This submission was already reviewed or replaced.", ephemeral=True)
        await close_review_message(review_message, "Already reviewed", discord.Color.dark_grey())
        return
    cursor.execute("UPDATE teams SET points=points+? WHERE id=?", (awarded, team_id))
    db.commit()

    await interaction.response.send_message(f"✅ Task accepted ({awarded} pts).", ephemeral=True)
    await close_review_message(
        review_message, f"✅ Accepted by {interaction.user.display_name} · {awarded} pts", discord.Color.green()
    )
    await asyncio.gather(
        dm_many(
            interaction.client,
            get_team_member_ids(team_id),
            f"✅ Your team's submission for '{desc}' was accepted for {awarded} point{'s' if awarded != 1 else ''}!",
        ),
        post_to_spectator(interaction.client, review_message, team_id, task_id, desc, awarded, interaction.user.display_name),
    )


# ---------- Modals ----------
class ScoreModal(discord.ui.Modal, title="Enter Task Score"):
    def __init__(self, team_id: int, task_id: int, max_points: int, task_desc: str, review_message: discord.Message):
        super().__init__()
        self.team_id = team_id
        self.task_id = task_id
        self.max_points = max_points
        self.task_desc = task_desc
        self.review_message = review_message

        self.score = discord.ui.TextInput(label=f"Score (0-{max_points})", required=True, max_length=6)
        self.add_item(self.score)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            awarded = int(self.score.value)
        except ValueError:
            await interaction.response.send_message("Score must be a whole number. Click Accept to try again.", ephemeral=True)
            return

        if not 0 <= awarded <= self.max_points:
            await interaction.response.send_message(
                f"Score must be between 0 and {self.max_points}. Click Accept to try again.", ephemeral=True
            )
            return

        await finish_accept(interaction, self.review_message, self.team_id, self.task_id, self.task_desc, awarded)


class DenyModal(discord.ui.Modal, title="Reason for Denial"):
    def __init__(self, team_id: int, task_id: int, task_desc: str, review_message: discord.Message):
        super().__init__()
        self.team_id = team_id
        self.task_id = task_id
        self.task_desc = task_desc
        self.review_message = review_message
        self.reason = discord.ui.TextInput(label="Reason", required=True, style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        # Same guard as accepting: never flip an Accepted submission back to Denied.
        cursor.execute(
            "UPDATE submissions SET status='Denied' "
            "WHERE team_id=? AND task_id=? AND status='Pending' AND message_id=?",
            (self.team_id, self.task_id, self.review_message.id),
        )
        db.commit()
        if cursor.rowcount == 0:
            await interaction.response.send_message("This submission was already reviewed or replaced.", ephemeral=True)
            await close_review_message(self.review_message, "Already reviewed", discord.Color.dark_grey())
            return

        await interaction.response.send_message("Submission denied.", ephemeral=True)
        await close_review_message(
            self.review_message,
            f"❌ Denied by {interaction.user.display_name}: {self.reason.value}"[:2048],
            discord.Color.red(),
        )
        cursor.execute("SELECT submitter_id FROM submissions WHERE team_id=? AND task_id=?", (self.team_id, self.task_id))
        row = cursor.fetchone()
        recipients = [row[0]] if row and row[0] else get_team_member_ids(self.team_id)
        await dm_many(
            interaction.client,
            recipients,
            f"❌ Your submission for '{self.task_desc}' (task {self.task_id}) was denied.\n"
            f"Reason: {self.reason.value}\n\nYou can resubmit with `/submit task_id:{self.task_id}`.",
        )


# ---------- Persistent review buttons ----------
# The team/task ids live in the button's custom_id, so buttons keep working after a bot restart.
class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"review:(?P<action>accept|deny):(?P<team>\d+):(?P<task>\d+)"):
    def __init__(self, action: str, team_id: int, task_id: int):
        accept = action == "accept"
        super().__init__(
            discord.ui.Button(
                label="Accept" if accept else "Deny",
                style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                custom_id=f"review:{action}:{team_id}:{task_id}",
            )
        )
        self.action = action
        self.team_id = team_id
        self.task_id = task_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], int(match["team"]), int(match["task"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        task_info = get_task_by_id(self.task_id)
        if not task_info:
            await interaction.response.send_message("Task not found.", ephemeral=True)
            return
        _, _, desc, pts, judge = task_info
        review_message = interaction.message

        if self.action == "deny":
            await interaction.response.send_modal(DenyModal(self.team_id, self.task_id, desc, review_message))
        elif judge == 1:
            await interaction.response.send_modal(ScoreModal(self.team_id, self.task_id, pts, desc, review_message))
        else:
            await finish_accept(interaction, review_message, self.team_id, self.task_id, desc, pts)


def make_review_view(team_id: int, task_id: int) -> View:
    view = View(timeout=None)
    view.add_item(ReviewButton("accept", team_id, task_id))
    view.add_item(ReviewButton("deny", team_id, task_id))
    return view


async def handle_submission(interaction: discord.Interaction, task_id: int, media: Optional[discord.Attachment]) -> None:
    """Shared submit flow for /submit and the task picker. The interaction must already be deferred."""
    team_id = get_team_id(interaction.user.id)
    if team_id is None:
        await interaction.followup.send("You're not on a team. Ask a Game Admin to add you.", ephemeral=True)
        return

    task_info = get_task_by_id(task_id)
    if not task_info:
        await interaction.followup.send("Task not found. Check `/my_tasks` for valid IDs.", ephemeral=True)
        return
    _, loc, desc, pts, judge = task_info
    if loc != get_active_location() and loc >= 1:
        await interaction.followup.send("That task isn't active right now.", ephemeral=True)
        return

    cursor.execute(
        "SELECT status, message_id FROM submissions WHERE team_id=? AND task_id=?",
        (team_id, task_id),
    )
    existing_submission = cursor.fetchone()
    if existing_submission and existing_submission[0] == "Accepted":
        await interaction.followup.send("✅ This task has already been accepted for your team.", ephemeral=True)
        return

    mod_channel_id = config.get("moderator_channel")
    channel = interaction.client.get_channel(mod_channel_id) if mod_channel_id else None
    if not channel:
        await interaction.followup.send("Moderator channel not found. Please tell a Game Admin.", ephemeral=True)
        return

    if media is None:
        prompt = f"📸 Send a photo or video for **{desc}** in this chat within 5 minutes."
        if existing_submission and existing_submission[0] == "Pending":
            prompt += "\n⏳ You already have one in review — a new upload will replace it."
        await interaction.followup.send(prompt, ephemeral=True)

        def check(m):
            return m.author == interaction.user and any(is_image(a) or is_video(a) for a in m.attachments)
        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Timed out. Run `/submit` again when you're ready.", ephemeral=True)
            return
        media = next(a for a in msg.attachments if is_image(a) or is_video(a))
    elif not (is_image(media) or is_video(media)):
        await interaction.followup.send("That file isn't a photo or video. Please try again.", ephemeral=True)
        return

    # Re-read after the upload: the previous submission may have been reviewed meanwhile.
    cursor.execute(
        "SELECT status, message_id FROM submissions WHERE team_id=? AND task_id=?",
        (team_id, task_id),
    )
    current = cursor.fetchone()
    if current and current[0] == "Accepted":
        await interaction.followup.send(
            "✅ Your last upload was already accepted, so there's no need to re-submit.",
            ephemeral=True,
        )
        return
    is_resubmission = current is not None
    if current and current[0] == "Pending":
        await close_previous_review_message(channel, current[1])

    embed = discord.Embed(
        title="🔁 Resubmission" if is_resubmission else "📸 New Submission",
        description=f"**Task #{task_id}:** {desc}",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Team", value=f"{get_team_name(team_id)} (ID {team_id})")
    embed.add_field(name="Submitted By", value=interaction.user.mention)
    embed.add_field(name="Points", value=f"up to {pts}" if judge == 1 else str(pts))

    try:
        review_message = await send_with_media(
            channel, embed, media, f"team{team_id}_task{task_id}", view=make_review_view(team_id, task_id)
        )
    except discord.HTTPException as exc:
        print(f"Failed to post submission for review: {exc}")
        await interaction.followup.send("❌ Couldn't send your submission to the judges. Please try again.", ephemeral=True)
        return

    stored_url = review_message.attachments[0].url if review_message.attachments else media.url
    cursor.execute(
        """
        INSERT INTO submissions(team_id, task_id, status, photo_url, message_id, submitter_id)
        VALUES (?, ?, 'Pending', ?, ?, ?)
        ON CONFLICT(team_id, task_id)
        DO UPDATE SET status='Pending', photo_url=excluded.photo_url,
                      message_id=excluded.message_id, submitter_id=excluded.submitter_id
        """,
        (team_id, task_id, stored_url, review_message.id, interaction.user.id),
    )
    db.commit()

    await interaction.followup.send("📩 Submission sent for review! You'll get a DM when it's judged.", ephemeral=True)


class TaskPicker(discord.ui.Select):
    """Dropdown under /my_tasks so players can submit without typing a task ID."""

    def __init__(self, tasks):
        options = [
            discord.SelectOption(label=f"#{tid} · {desc}"[:100], value=str(tid), description=f"{pts} pts")
            for tid, desc, pts, status in tasks
            if status != "Accepted"
        ][:25]
        super().__init__(placeholder="📸 Pick a task to submit…", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await handle_submission(interaction, int(self.values[0]), None)


def open_tasks_for(user_id: int):
    team_id = get_team_id(user_id)
    if team_id is None:
        return []
    return [t for t in get_tasks_with_status(team_id, get_active_location()) if t[3] != "Accepted"]


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

        set_active_location(location)

        cursor.execute("SELECT DISTINCT discord_id FROM users")
        user_ids = [discord_id for (discord_id,) in cursor.fetchall()]
        instruction_message = (
            f"🏁 **Location {location} is live!**\n\n"
            "• `/my_tasks` — see your tasks, then pick one from the menu to submit\n"
            "• `/submit` — start typing a task name and attach your photo/video\n"
            "• `/team` — see your teammates and points\n"
            "• `/leaderboard` — see how your team is doing\n\n"
            "Good luck!"
        )
        delivered = await dm_many(interaction.client, user_ids, instruction_message)
        await interaction.followup.send(
            f"Game started for location {location}! Notified {delivered}/{len(user_ids)} players.", ephemeral=True
        )

    @tree.command(name="load_tasks", description="Load tasks from a Google Sheet")
    @app_commands.describe(sheet_name="The name of the Google Sheet to load tasks from")
    async def load_tasks(interaction: discord.Interaction, sheet_name: str):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        added, updated, error = await load_tasks_from_sheet(sheet_name)
        message = error or f"Loaded {sheet_name}: {added} new tasks, {updated} updated."
        await interaction.followup.send(message, ephemeral=True)

    # --- My Tasks ---
    @tree.command(name="my_tasks", description="View your tasks for the current location")
    async def my_tasks(interaction: discord.Interaction):
        team_id = get_team_id(interaction.user.id)
        if team_id is None:
            await interaction.response.send_message("You're not on a team. Ask a Game Admin to add you.", ephemeral=True)
            return

        location = get_active_location()
        tasks = get_tasks_with_status(team_id, location)
        if not tasks:
            await interaction.response.send_message("No tasks are active right now — hang tight!", ephemeral=True)
            return

        icons = {"Accepted": "✅", "Pending": "🟡", "Denied": "🔁"}
        lines = [f"{icons.get(status, '⬜')} **#{tid}** · {desc} · *{pts} pts*" for tid, desc, pts, status in tasks]
        done = sum(1 for *_, status in tasks if status == "Accepted")

        # Embed descriptions hold 4096 chars (fields cap at 25), so split long task lists.
        embeds = []
        for i, chunk in enumerate(chunk_lines(lines, limit=4000)):
            embed = discord.Embed(description=chunk, color=discord.Color.blurple())
            if i == 0:
                embed.title = f"📋 {get_team_name(team_id)} — Location {location} ({done}/{len(tasks)} done)"
            embeds.append(embed)
        embeds[-1].set_footer(text="✅ accepted · 🟡 in review · 🔁 denied, resubmit · ⬜ not started")

        view = discord.utils.MISSING
        if done < len(tasks):
            view = View(timeout=600)
            view.add_item(TaskPicker(tasks))
        await interaction.response.send_message(embeds=embeds[:10], view=view, ephemeral=True)

    # --- Submit Photo/Video ---
    @tree.command(name="submit", description="Submit a photo or video for a task")
    @app_commands.describe(task_id="Start typing to pick a task", media="Your photo or video (optional — you can also upload after)")
    async def submit(interaction: discord.Interaction, task_id: int, media: Optional[discord.Attachment] = None):
        await interaction.response.defer(ephemeral=True)
        await handle_submission(interaction, task_id, media)

    @submit.autocomplete("task_id")
    async def submit_task_autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [
            app_commands.Choice(name=f"#{tid} · {desc}"[:100], value=tid)
            for tid, desc, pts, status in open_tasks_for(interaction.user.id)
            if current in f"{tid} {desc}".lower()
        ][:25]
