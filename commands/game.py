import logging
import asyncio
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse
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
from utils import reject_if_not_admin, is_admin, dm_many, chunk_lines, progress_bar
from commands.leaderboard import request_leaderboard_refresh
from sheets import load_tasks_from_sheet

log = logging.getLogger("rally.game")


VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv", ".3gp"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}

# Free Discord accounts can't send files over 10 MB, which rules out most phone videos.
# Players can paste a link instead (Google Drive, YouTube, iCloud, Streamable...).
UPLOAD_LIMIT_HINT = (
    "Video too big to send? Discord caps uploads at 10 MB. Trim it, or upload it to "
    "Google Drive / YouTube (unlisted) / iCloud and paste the link instead."
)
URL_PATTERN = re.compile(r"https?://[^\s<>]+")

# How long a player has to send their photo/video after picking a task in /my_tasks.
# Generous because big videos can take a while to upload on cell data.
UPLOAD_WINDOW = 10 * 60


@dataclass
class Media:
    """A submission, kept as a pointer to the player's own message. Never downloaded."""
    url: str
    channel_id: int | None = None  # set for uploaded files, which the bot forwards
    message_id: int | None = None
    jump_url: str | None = None


# The task each player most recently picked; picking again cancels the older pick,
# so one upload can never be submitted for two tasks.
pending_picks: dict[int, object] = {}

# Messages already consumed by a "send your photo now" prompt, so the DM listener
# doesn't also reply to them.
claimed_uploads: set[int] = set()


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


def is_video_url(url: str) -> bool:
    return os.path.splitext(urlparse(url).path)[1].lower() in VIDEO_EXTENSIONS


def is_video(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("video/") or _extension(attachment) in VIDEO_EXTENSIONS


def is_image(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("image/") or _extension(attachment) in IMAGE_EXTENSIONS


def is_image_url(url: str) -> bool:
    return os.path.splitext(urlparse(url).path)[1].lower() in IMAGE_EXTENSIONS


def media_from_message(message: discord.Message) -> Optional[Media]:
    """The submission in a message: its photo/video files, else the first link in its text."""
    if any(is_image(a) or is_video(a) for a in message.attachments):
        first = next(a for a in message.attachments if is_image(a) or is_video(a))
        return Media(first.url, message.channel.id, message.id, message.jump_url)
    match = URL_PATTERN.search(message.content or "")
    if match:
        return Media(match.group(0).rstrip(".,)>"), jump_url=message.jump_url)
    return None


async def forward_media(client: discord.Client, channel: discord.abc.Messageable, media: Media) -> bool:
    """Forward the player's own message (with its video/photo) into a channel. No download involved."""
    if media.channel_id is None or media.message_id is None:
        return False
    source = client.get_partial_messageable(media.channel_id).get_partial_message(media.message_id)
    try:
        await source.forward(channel)
        return True
    except discord.HTTPException as exc:
        # The player deleted the message, or Discord refused the forward; fall back to the link.
        log.warning("Forwarding message %s failed, posting link instead: %s", media.message_id, exc)
        return False


async def send_with_media(
    client: discord.Client,
    channel: discord.abc.Messageable,
    embed: discord.Embed,
    media: Media,
    view: Optional[View] = None,
) -> discord.Message:
    """Post the player's media followed by an embed (and optional review buttons).

    Uploaded files are *forwarded* from the player's DM, so videos play right in the channel
    and nothing is downloaded or re-uploaded. Links are posted on their own so Discord
    renders a preview.
    """
    if await forward_media(client, channel, media):
        kind = "🎥 Video" if is_video_url(media.url) else "📸 Photo"
        embed.add_field(name=kind, value="Forwarded above", inline=False)
    elif media.channel_id is None and is_image_url(media.url):
        embed.set_image(url=media.url)
    else:
        await channel.send(media.url)
        label = "🎥 Video" if is_video_url(media.url) else "🔗 Link"
        embed.add_field(name=label, value=f"Preview above · [open]({media.url})", inline=False)
    return await channel.send(embed=embed, **({"view": view} if view is not None else {}))


async def close_review_message(message: discord.Message, note: str, color: discord.Color) -> None:
    """Remove the buttons from a review message and stamp it with the outcome."""
    embed = message.embeds[0].copy() if message.embeds else discord.Embed()
    embed.color = color
    embed.set_footer(text=note)
    try:
        await message.edit(embed=embed, view=None)
    except discord.HTTPException as exc:
        log.warning("Unable to update review message %s: %s", message.id, exc)


async def close_previous_review_message(channel, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        old_message = await channel.fetch_message(message_id)
    except discord.HTTPException as exc:
        log.warning("Unable to fetch previous review message %s: %s", message_id, exc)
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

    cursor.execute(
        "SELECT photo_url, source_channel_id, source_message_id FROM submissions WHERE team_id=? AND task_id=?",
        (team_id, task_id),
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return
    try:
        await send_with_media(client, channel, embed, Media(row[0], row[1], row[2]))
    except discord.HTTPException as exc:
        log.warning("Failed to post highlight: %s", exc)


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
    log.info("ACCEPTED team %s task %s for %s pts by %s", team_id, task_id, awarded, interaction.user)

    await interaction.response.send_message(f"✅ Task accepted ({awarded} pts).", ephemeral=True)
    await close_review_message(
        review_message, f"✅ Accepted by {interaction.user.display_name} · {awarded} pts", discord.Color.green()
    )
    request_leaderboard_refresh(interaction.client)
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

        log.info("DENIED team %s task %s by %s: %s", self.team_id, self.task_id, interaction.user, self.reason.value)
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
            f"Reason: {self.reason.value}\n\nTo try again, run `/my_tasks`, pick task #{self.task_id} and send a new photo, video or link.",
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


async def _say(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def handle_submission(interaction: discord.Interaction, task_id: int) -> None:
    """Runs after a player picks a task in /my_tasks: wait for their upload, then send it to the judges."""
    user = interaction.user
    team_id = get_team_id(user.id)
    if team_id is None:
        await _say(interaction, "You're not on a team. Ask a Game Admin to add you.")
        return

    # The /my_tasks list can be old, so re-check the task against the live game.
    task_info = get_task_by_id(task_id)
    if not task_info:
        await _say(interaction, "That task no longer exists. Run `/my_tasks` again.")
        return
    _, loc, desc, pts, judge = task_info
    if loc != get_active_location() and loc >= 1:
        await _say(interaction, "That task isn't active anymore. Run `/my_tasks` for the current list.")
        return

    cursor.execute("SELECT status FROM submissions WHERE team_id=? AND task_id=?", (team_id, task_id))
    existing_submission = cursor.fetchone()
    if existing_submission and existing_submission[0] == "Accepted":
        await _say(interaction, "✅ This task has already been accepted for your team.")
        return

    mod_channel_id = config.get("moderator_channel")
    channel = interaction.client.get_channel(mod_channel_id) if mod_channel_id else None
    if not channel:
        await _say(interaction, "Moderator channel not found. Please tell a Game Admin.")
        return

    pick = object()
    pending_picks[user.id] = pick
    prompt = (
        f"📸 Now send your photo, video or link for **#{task_id} · {desc}** in this chat "
        f"(you have {UPLOAD_WINDOW // 60} minutes).\n-# {UPLOAD_LIMIT_HINT}"
    )
    if existing_submission and existing_submission[0] == "Pending":
        prompt += "\n⏳ You already have one in review — this upload will replace it."
    await _say(interaction, prompt)

    def check(m):
        if (pending_picks.get(user.id) is pick and m.author.id == user.id
                and m.channel.id == interaction.channel_id and media_from_message(m)):
            if m.guild is None:  # only the DM listener reads (and clears) this set
                claimed_uploads.add(m.id)
            return True
        return False
    try:
        msg = await interaction.client.wait_for("message", check=check, timeout=UPLOAD_WINDOW)
    except asyncio.TimeoutError:
        if pending_picks.get(user.id) is pick:
            pending_picks.pop(user.id, None)
            await _say(interaction, f"⏰ No upload for **#{task_id}** yet. Pick it again in `/my_tasks` when you're ready.")
        return  # otherwise the player picked another task since; that pick handles it
    pending_picks.pop(user.id, None)
    media = media_from_message(msg)

    # Re-read after the upload: the previous submission may have been reviewed meanwhile.
    cursor.execute(
        "SELECT status, message_id FROM submissions WHERE team_id=? AND task_id=?",
        (team_id, task_id),
    )
    current = cursor.fetchone()
    if current and current[0] == "Accepted":
        await _say(interaction, "✅ A teammate's upload for this task was just accepted, so this one wasn't needed.")
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
    embed.add_field(name="Submitted By", value=user.mention)
    embed.add_field(name="Points", value=f"up to {pts}" if judge == 1 else str(pts))

    try:
        review_message = await send_with_media(
            interaction.client, channel, embed, media, view=make_review_view(team_id, task_id)
        )
    except discord.HTTPException as exc:
        log.error("Failed to post submission (team %s, task %s) for review: %s", team_id, task_id, exc)
        await _say(interaction, "❌ Couldn't send your submission to the judges. Pick the task in `/my_tasks` and try again.")
        return

    cursor.execute(
        """
        INSERT INTO submissions(team_id, task_id, status, photo_url, message_id, submitter_id,
                                source_channel_id, source_message_id)
        VALUES (?, ?, 'Pending', ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, task_id)
        DO UPDATE SET status='Pending', photo_url=excluded.photo_url,
                      message_id=excluded.message_id, submitter_id=excluded.submitter_id,
                      source_channel_id=excluded.source_channel_id,
                      source_message_id=excluded.source_message_id
        """,
        (team_id, task_id, media.url, review_message.id, user.id, media.channel_id, media.message_id),
    )
    db.commit()

    log.info(
        "SUBMITTED team %s task %s by %s (%s, %s)", team_id, task_id, user,
        "forwarded upload" if media.channel_id else "link", "resubmission" if is_resubmission else "new",
    )
    await _say(interaction, f"📩 **#{task_id} · {desc}** sent for review! You'll get a DM when it's judged.")

    # Keep teammates in sync so they don't redo the same task.
    teammates = [uid for uid in get_team_member_ids(team_id) if uid != user.id]
    await dm_many(
        interaction.client,
        teammates,
        f"🟡 {user.display_name} submitted **#{task_id} · {desc}** — waiting on the judges.",
    )


class TaskSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"rally:pick_task"):
    """The "pick a task to submit" dropdown under /my_tasks.

    A DynamicItem with a fixed custom_id, so it never times out and keeps working after a
    bot restart. Each pick re-reads the live game state, so an old list can't submit a
    finished or inactive task.
    """

    def __init__(self, tasks=()):
        options = [
            discord.SelectOption(label=f"#{tid} · {desc}"[:100], value=str(tid), description=f"{pts} pts")
            for tid, desc, pts, status in tasks
            if status != "Accepted"
        ][:25]
        super().__init__(
            discord.ui.Select(
                custom_id="rally:pick_task",
                placeholder="📸 Pick a task to submit…",
                options=options or [discord.SelectOption(label="-", value="0")],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        task_id = int(interaction.data["values"][0])
        # Refresh the list in place: clears the selection (so the same task can be picked
        # again) and drops tasks finished since /my_tasks was run.
        await interaction.response.edit_message(**build_tasks_response(interaction.user.id))
        await handle_submission(interaction, task_id)


def build_tasks_response(user_id: int) -> dict:
    """Message kwargs for a player's task list (/my_tasks)."""
    team_id = get_team_id(user_id)
    if team_id is None:
        return {"content": "You're not on a team. Ask a Game Admin to add you.", "embeds": [], "view": None}

    location = get_active_location()
    tasks = get_tasks_with_status(team_id, location)
    if not tasks:
        return {"content": "No tasks are active right now — hang tight!", "embeds": [], "view": None}

    icons = {"Accepted": "✅", "Pending": "🟡", "Denied": "🔁"}
    lines = [f"{icons.get(status, '⬜')} **#{tid}** · {desc} · *{pts} pts*" for tid, desc, pts, status in tasks]
    done = sum(1 for *_, status in tasks if status == "Accepted")

    # Embed descriptions hold 4096 chars (fields cap at 25), so split long task lists.
    embeds = []
    for i, chunk in enumerate(chunk_lines(lines, limit=3900)):
        embed = discord.Embed(description=chunk, color=discord.Color.blurple())
        if i == 0:
            embed.title = f"📋 {get_team_name(team_id)} — Location {location}"
            embed.description = f"{progress_bar(done, len(tasks))} **{done}/{len(tasks)} done**\n\n{chunk}"
        embeds.append(embed)
    embeds[-1].set_footer(
        text="✅ accepted · 🟡 in review · 🔁 denied, resubmit · ⬜ not started\n"
        "To submit: pick the task below, then send your photo or video."
    )
    response = {"content": None, "embeds": embeds[:10], "view": None}
    open_tasks = [t for t in tasks if t[3] != "Accepted"]
    if open_tasks:
        view = View(timeout=None)
        view.add_item(TaskSelect(open_tasks))
        response["view"] = view
    return response


# ---------- Command Setup ----------
def setup_game(tree: app_commands.CommandTree):

    @tree.command(name="start_game", description="Start the game for a specific location")
    @app_commands.describe(location="The location ID to start")
    @app_commands.guild_only()
    async def start_game(interaction: discord.Interaction, location: int):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        cursor.execute("SELECT 1 FROM tasks WHERE location = ?", (location,))
        if cursor.fetchone() is None:
            await interaction.followup.send("No tasks available for this location.", ephemeral=True)
            return

        set_active_location(location)
        log.info("GAME STARTED at location %s by %s", location, interaction.user)

        cursor.execute("SELECT DISTINCT discord_id FROM users")
        user_ids = [discord_id for (discord_id,) in cursor.fetchall()]
        # Imported here because the player module builds on this one.
        from commands.player import send_instructions
        delivered = await send_instructions(
            interaction.client, user_ids, f"🏁 **Location {location} is live!** Good luck!"
        )
        await interaction.followup.send(
            f"Game started for location {location}! Notified {delivered}/{len(user_ids)} players.", ephemeral=True
        )

    @tree.command(name="announce", description="DM an announcement to every player (or one team)")
    @app_commands.describe(message="What to tell players", team_id="Only this team (optional)")
    @app_commands.guild_only()
    async def announce(interaction: discord.Interaction, message: str, team_id: Optional[int] = None):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if team_id is None:
            cursor.execute("SELECT DISTINCT discord_id FROM users")
            user_ids = [discord_id for (discord_id,) in cursor.fetchall()]
        else:
            user_ids = get_team_member_ids(team_id)
        delivered = await dm_many(interaction.client, user_ids, f"📣 **Announcement**\n{message}")
        log.info("ANNOUNCE by %s to %d/%d players: %s", interaction.user, delivered, len(user_ids), message)
        await interaction.followup.send(f"Delivered to {delivered}/{len(user_ids)} players.", ephemeral=True)

    @tree.command(name="queue", description="Show submissions waiting for review")
    @app_commands.guild_only()
    async def queue(interaction: discord.Interaction):
        if await reject_if_not_admin(interaction):
            return
        cursor.execute(
            """
            SELECT s.task_id, s.message_id, t.name, k.description
            FROM submissions s
            JOIN teams t ON t.id = s.team_id
            JOIN tasks k ON k.id = s.task_id
            WHERE s.status = 'Pending' AND s.message_id IS NOT NULL
            ORDER BY s.id
            """
        )
        pending = cursor.fetchall()
        if not pending:
            await interaction.response.send_message("🎉 Review queue is empty.", ephemeral=True)
            return

        channel_id = config.get("moderator_channel")
        lines = [
            f"• [{team} · #{task_id} {desc[:60]}](https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id})"
            for task_id, message_id, team, desc in pending[:20]
        ]
        embed = discord.Embed(
            title=f"🕒 {len(pending)} waiting for review",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        if len(pending) > 20:
            embed.set_footer(text=f"Showing the oldest 20 of {len(pending)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="load_tasks", description="Load tasks from a Google Sheet")
    @app_commands.describe(sheet_name="The name of the Google Sheet to load tasks from")
    @app_commands.guild_only()
    async def load_tasks(interaction: discord.Interaction, sheet_name: str):
        if await reject_if_not_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        added, updated, error = await load_tasks_from_sheet(sheet_name)
        message = error or f"Loaded {sheet_name}: {added} new tasks, {updated} updated."
        await interaction.followup.send(message, ephemeral=True)

    @tree.command(name="delete_all_tasks", description="Delete every task (and its submissions) so you can reload the sheet")
    @app_commands.describe(confirm="Type DELETE to confirm. This can't be undone.")
    @app_commands.guild_only()
    async def delete_all_tasks(interaction: discord.Interaction, confirm: str):
        if await reject_if_not_admin(interaction):
            return
        if confirm.strip() != "DELETE":
            await interaction.response.send_message(
                "Nothing deleted. Type `DELETE` (all caps) in the confirm box to wipe every task.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        cursor.execute("SELECT message_id FROM submissions WHERE status='Pending' AND message_id IS NOT NULL")
        pending_reviews = [message_id for (message_id,) in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) FROM tasks")
        task_count = cursor.fetchone()[0]
        # Submissions go too: task IDs restart at 1 after a wipe, so old submissions would
        # otherwise attach themselves to the newly loaded tasks. Team points are kept.
        cursor.execute("DELETE FROM submissions")
        cursor.execute("DELETE FROM tasks")
        db.commit()
        pending_picks.clear()
        log.warning("DELETED ALL %d tasks and their submissions (by %s)", task_count, interaction.user)

        # Stamp reviews that were still waiting so judges don't act on deleted tasks.
        channel = interaction.client.get_channel(config.get("moderator_channel") or 0)
        if channel is not None:
            for message_id in pending_reviews:
                try:
                    message = await channel.fetch_message(message_id)
                except discord.HTTPException:
                    continue
                await close_review_message(message, "Task deleted by a Game Admin", discord.Color.dark_grey())

        await interaction.followup.send(
            f"🗑️ Deleted {task_count} task{'s' if task_count != 1 else ''} and all their submissions "
            "(team points were kept). Run `/load_tasks` to load the sheet again.",
            ephemeral=True,
        )

    # --- My Tasks ---
    @tree.command(name="my_tasks", description="View your tasks for the current location")
    async def my_tasks(interaction: discord.Interaction):
        response = build_tasks_response(interaction.user.id)
        if response["view"] is None:
            del response["view"]  # send_message wants it omitted, not None
        await interaction.response.send_message(**response, ephemeral=True)
