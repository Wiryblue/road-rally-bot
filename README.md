# Road Rally Bot

Road Rally Bot is a Discord slash-command bot designed for scavenger-hunt or rally-style events. It helps organizers manage teams, load tasks from Google Sheets, collect photographic submissions, and award points while keeping players updated via DMs.

## Features
- **Team management:** `/create_team`, `/rename_team`, `/remove_team`, and `/list_teams` let game admins organize participants and view their current rosters.
- **Task orchestration:** `/start_game` selects the active location, notifies every registered player, and unlocks `/my_tasks` for that stage. `/load_tasks` syncs tasks from a Google Sheet.
- **Submission & review workflow:** Players upload photos through `/submit`. The bot handles re-uploads, prevents duplicate scoring, DMs the player upon acceptance/denial, and reposts accepted photos to a spectator channel.
- **Points & leaderboard controls:** `/add_points`, `/remove_points`, and `/leaderboard` manage or display scores, while `/toggle_leaderboard` hides the standings when needed.

## Requirements
- Python 3.11+
- Discord bot token with the `applications.commands` scope and the **Message Content Intent** enabled.
- Google service account credentials with access to the Sheets document that stores tasks.
- SQLite (ships with Python) for persistent storage.

### Python dependencies
Install the libraries with pip:
```bash
pip install discord.py gspread oauth2client
```

## Configuration
1. **Create credentials folder**
   - Place your Google service account JSON in `configs/credentials.json`.

2. **Create the config file**
   - Copy the template below to `configs/config.json` and fill in your values:
```json
{
  "bot_token": "YOUR_BOT_TOKEN",
  "moderator_channel": 123456789012345678,
  "spectator_channel": 987654321098765432,
  "server_id": 112233445566778899
}
```
   - `moderator_channel`: Channel ID where review embeds and Accept/Deny buttons are posted.
   - `spectator_channel`: Optional highlights channel for accepted submissions (omit or set to `null` to disable).
   - `server_id`: Guild ID where slash commands should be registered.

3. **Assign roles**
   - Moderators must have a Discord role named **Game Admin** (see `utils.py`) to run administrative commands.

## Database
The bot automatically creates `game.db` with the following tables:
- `users` (Discord member to team mapping)
- `teams` (team name & cumulative points)
- `tasks` (location, description, base points, judge type)
- `submissions` (tracks status, photo URL, and moderation message IDs)

Populate `tasks` manually or via `/load_tasks`. Users are added to teams through `/create_team` which also records their Discord IDs in `users`.

## Running the bot
1. Ensure your virtual environment is active and dependencies installed.
2. Initialize any seed data (teams, tasks) if needed.
3. Launch the bot:
```bash
python main.py
```
4. Invite the bot to your guild with the correct OAuth scopes (`bot` + `applications.commands`) and give it permission to read/send messages in the moderator & spectator channels.

## Slash command quick reference
| Command | Audience | Purpose |
| --- | --- | --- |
| `/create_team` | Game Admin | Create a team and assign up to six members. |
| `/list_teams` | Game Admin | View teams with their members and points. |
| `/rename_team`, `/remove_team` | Game Admin | Maintain team metadata. |
| `/start_game` | Game Admin | Set the active location and DM all registered players with instructions. |
| `/load_tasks` | Game Admin | Pull task definitions from Google Sheets. |
| `/my_tasks` | Players | View the active location's tasks plus status indicators. |
| `/submit` | Players | Upload or re-upload a photo for a task. |
| `/add_points`, `/remove_points` | Game Admin | Manually adjust a team's score. |
| `/leaderboard`, `/toggle_leaderboard` | All / Admin | Show or hide the current standings. |

## Troubleshooting tips
- **Buttons stop working:** Ensure the bot can manage the original review message. Re-uploads automatically disable previous buttons; deleting the message manually can cause fetch errors but will not break future submissions.
- **Duplicate scoring:** The bot double-checks database state during scoring and after uploads. If you still see duplicates, verify only one judge role has permission to press Accept.
- **Google Sheets import fails:** Confirm the sheet name matches exactly and that the service account email has at least Viewer access to the document.

For additional customization—such as alternate role names or storage backends—extend the helper functions in `utils.py` and `database.py`.
