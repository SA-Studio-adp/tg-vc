import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8893555718:AAGtJPHfYXXOFTfD9NF5tBXbnT7uTh9O8aI")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004408283215")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "7990681306").split(",") if x.strip().isdigit()}
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing — copy .env.example to .env and fill it in.")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing — copy .env.example to .env and fill it in.")
