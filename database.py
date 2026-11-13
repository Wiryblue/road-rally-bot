import sqlite3

db = sqlite3.connect("game.db")
cursor = db.cursor()

# WAL + NORMAL sync keeps commits fast on a Pi's SD card while staying crash-safe.
cursor.execute("PRAGMA journal_mode=WAL")
cursor.execute("PRAGMA synchronous=NORMAL")

cursor.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    discord_id INTEGER,
    team_id INTEGER
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT,
    points INTEGER
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    location INTEGER,
    description TEXT,
    points INTEGER,
    judge INTEGER
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    team_id INTEGER,
    task_id INTEGER,
    message_id INTEGER,
    status TEXT,
    photo_url TEXT,
    submitter_id INTEGER,
    UNIQUE(team_id, task_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id);
CREATE INDEX IF NOT EXISTS idx_users_team_id ON users(team_id);
CREATE INDEX IF NOT EXISTS idx_tasks_location ON tasks(location);
""")

# Older databases were created before submitter_id existed.
_submission_columns = {row[1] for row in cursor.execute("PRAGMA table_info(submissions)")}
if "submitter_id" not in _submission_columns:
    cursor.execute("ALTER TABLE submissions ADD COLUMN submitter_id INTEGER")
# Where the player's original upload lives, so the bot can forward it instead of re-uploading.
for _column in ("source_channel_id", "source_message_id"):
    if _column not in _submission_columns:
        cursor.execute(f"ALTER TABLE submissions ADD COLUMN {_column} INTEGER")
db.commit()

# --- Helpers ---
def add_user_to_team(user_id, team_id):
    cursor.execute("INSERT INTO users (discord_id, team_id) VALUES (?, ?)", (user_id, team_id))
    db.commit()

def update_points(team_id, points):
    cursor.execute("UPDATE teams SET points = points + ? WHERE id = ?", (points, team_id))
    db.commit()

def fetch_leaderboard():
    cursor.execute("SELECT name, points FROM teams ORDER BY points DESC")
    return cursor.fetchall()

def get_team_id(discord_id):
    cursor.execute("SELECT team_id FROM users WHERE discord_id = ?", (discord_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def get_team_member_ids(team_id):
    cursor.execute("SELECT discord_id FROM users WHERE team_id = ?", (team_id,))
    return [discord_id for (discord_id,) in cursor.fetchall()]

# Settings survive restarts (the active location used to reset to 0 whenever the bot rebooted).
def get_setting(key, default=None):
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def set_setting(key, value):
    cursor.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()

def get_active_location():
    return int(get_setting("active_location", 0))

def set_active_location(location):
    set_setting("active_location", location)
