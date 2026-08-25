"""
Run this ONCE, locally, on your own machine (not on Render).
It logs in with YOUR Telegram account (phone number) and prints a
SESSION_STRING you paste into your .env / Render environment variables.

This account becomes the "userbot" that joins the voice chat and
streams audio/video into it — the bot token alone cannot do this,
because Telegram's Bot API has no voice-chat streaming support.

Get API_ID / API_HASH from https://my.telegram.org -> API Development Tools.

Usage:
    python generate_session.py
"""
from pyrogram import Client

def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()

    # in_memory=True -> nothing is written to disk, you just get a string back
    with Client("session_gen", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        session_string = app.export_session_string()

    print("\n" + "=" * 60)
    print("Save this as SESSION_STRING in your .env / Render env vars:")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print(
        "\nKeep this string private — it's equivalent to your account "
        "password. Anyone with it can log in as you."
    )


if __name__ == "__main__":
    main()
