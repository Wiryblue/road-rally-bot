import discord
from discord import app_commands
from discord.ui import Button, View
from database import cursor, db
from config import config
from utils import reject_if_not_admin, is_admin


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
        title=f"🏁 {team_name} completed a task!",
        description=f"> {task_desc}",
        color=discord.Color.green()
    )
    embed.add_field(name="Points", value=str(points))
    embed.set_image(url=photo_url)
    embed.set_footer(text=f"Awarded by {interaction.user.name}")

    await channel.send(embed=embed)


# ---------- Modal ----------
class ScoreModal(discord.ui.Modal, title="Enter Task Score"):
    def __init__(self, team_id: int, task_id: int, max_points: int, task_desc: str, photo_url: str):
        super().__init__()
        self.team_id = team_id
        self.task_id = task_id
        self.max_points = max_points
        self.task_desc = task_desc
        self.photo_url = photo_url

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

        cursor.execute("UPDATE submissions SET status='Accepted' WHERE team_id=? AND task_id=?", (self.team_id, self.task_id))
        cursor.execute("UPDATE teams SET points=points+? WHERE id=?", (awarded, self.team_id))
        db.commit()

        await interaction.response.send_message(f"✅ Task accepted ({awarded} pts).", ephemeral=True)
        await post_to_spectator(interaction, self.team_id, self.task_desc, self.photo_url, awarded)


# ---------- Command Setup ----------
def setup_game(tree: app_commands.CommandTree):

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

        await interaction.followup.send("Please upload a photo for this task.", ephemeral=True)

        def check(m): return m.author == interaction.user and len(m.attachments) > 0
        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=300)
        except:
            await interaction.followup.send("⏰ Timeout. Try again.", ephemeral=True)
            return

        photo_url = msg.attachments[0].url
        cursor.execute("""
            INSERT INTO submissions(team_id, task_id, status, photo_url)
            VALUES (?, ?, 'Pending', ?)
            ON CONFLICT(team_id, task_id) DO UPDATE SET status='Pending', photo_url=excluded.photo_url
        """, (team_id, task_id, photo_url))
        db.commit()

        # notify mod channel
        mod_channel_id = config.get("moderator_channel")
        channel = interaction.client.get_channel(mod_channel_id)
        if not channel:
            await interaction.followup.send("Moderator channel not found.", ephemeral=True)
            return

        embed = discord.Embed(title="📸 New Submission", description=f"Task: {desc}", color=discord.Color.orange())
        embed.add_field(name="Team ID", value=str(team_id))
        embed.add_field(name="Submitted By", value=interaction.user.mention)
        embed.set_image(url=photo_url)

        review_view = View()

        # Accept Button
        async def accept_callback(btn_inter: discord.Interaction):
            if not is_admin(btn_inter.user):
                await btn_inter.response.send_message("Not authorized.", ephemeral=True)
                return

            if judge == 1:
                await btn_inter.response.send_modal(
                    ScoreModal(team_id, task_id, pts, desc, photo_url)
                )
            else:
                cursor.execute("UPDATE submissions SET status='Accepted' WHERE team_id=? AND task_id=?", (team_id, task_id))
                cursor.execute("UPDATE teams SET points=points+? WHERE id=?", (pts, team_id))
                db.commit()
                await btn_inter.response.send_message("✅ Task accepted.", ephemeral=True)
                await post_to_spectator(btn_inter, team_id, desc, photo_url, pts)

        accept_button = Button(label="Accept", style=discord.ButtonStyle.success)
        accept_button.callback = accept_callback
        review_view.add_item(accept_button)

        # Deny Button
        async def deny_callback(btn_inter: discord.Interaction):
            if not is_admin(btn_inter.user):
                await btn_inter.response.send_message("Not authorized.", ephemeral=True)
                return

            class DenyModal(discord.ui.Modal, title="Reason for Denial"):
                reason = discord.ui.TextInput(label="Reason", required=True)

                async def on_submit(self, inter):
                    cursor.execute("UPDATE submissions SET status='Denied' WHERE team_id=? AND task_id=?", (team_id, task_id))
                    db.commit()
                    await inter.response.send_message("Submission denied.", ephemeral=True)
                    # DM the submitter
                    await interaction.user.send(f"❌ Your submission for '{desc}' was denied. Reason: {self.reason.value}")

            await btn_inter.response.send_modal(DenyModal())

        deny_button = Button(label="Deny", style=discord.ButtonStyle.danger)
        deny_button.callback = deny_callback
        review_view.add_item(deny_button)

        await channel.send(embed=embed, view=review_view)
        await interaction.followup.send("📩 Submission sent for review!", ephemeral=True)
