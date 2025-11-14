import json
from oauth2client.service_account import ServiceAccountCredentials
import gspread

# Load config
with open("configs/config.json") as f:
    config = json.load(f)

BOT_TOKEN = config["bot_token"]
MOD_CHANNEL = config.get("moderator_channel")

# Google Sheets client
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
credentials = ServiceAccountCredentials.from_json_keyfile_name(
    "configs/credentials.json", scope
)
gclient = gspread.authorize(credentials)
