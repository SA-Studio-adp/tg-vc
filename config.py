import os
from dotenv import load_dotenv

load_dotenv()

# --- Bot (for commands) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]

# --- Userbot (for actually streaming into the voice chat) ---
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# The group whose voice chat we stream into.
# Must be a group (not a channel) with an active or startable voice chat,
# and the userbot account must already be a member of it.
CHAT_ID = int(os.environ["CHAT_ID"])

ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
