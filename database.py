import sqlite3

db = sqlite3.connect("game.db")
cursor = db.cursor()

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
    UNIQUE(team_id, task_id)
);
""")
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
