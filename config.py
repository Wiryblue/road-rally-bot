import json
import os

# Load config
with open("configs/config.json") as f:
    config = json.load(f)

# Prefer the environment so the token doesn't have to live in a file.
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or config["bot_token"]
MOD_CHANNEL = config.get("moderator_channel")

_gclient = None


def get_gclient():
    """Google Sheets client, created on first use so startup doesn't wait on Google."""
    global _gclient
    if _gclient is None:
        import gspread
        _gclient = gspread.service_account(
            filename="configs/credentials.json",
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
        )
    return _gclient
