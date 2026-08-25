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

# --- Web dashboard ---
# Render (and most "web service" hosting tiers) expect the process to
# bind a port, or the deploy is eventually killed even if the bot itself
# is working fine via long-polling. This also doubles as the requested
# "home page" to view/control the queue from a browser.
PORT = int(os.environ.get("PORT", 10000))
# Set this to something secret in your env vars — without it the
# dashboard has no authentication and anyone with the URL could control
# the bot (skip/stop/pause). Leave unset only for local testing.
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

# --- YouTube cookies (optional but usually required on cloud hosts) ---
# YouTube increasingly demands "sign in to confirm you're not a bot" for
# requests from datacenter IPs like Render's. Point this at a cookies.txt
# exported from a real logged-in browser session to fix it. See README.
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")
