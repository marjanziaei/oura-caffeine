"""Show the chat IDs of people who recently messaged your bot, so you can put yours in .env.

1. In Telegram, open your bot and send it any message (e.g. "hi").
2. Run:  python find_chat_id.py   (while bot.py is NOT running; both read the same updates)
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv


def telegram(token, method):
    """Call a Bot API method. Errors never include the URL, because it contains the token."""
    try:
        body = requests.get(f"https://api.telegram.org/bot{token}/{method}", timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        raise SystemExit(f"Couldn't reach Telegram ({type(e).__name__}).") from None
    if not body.get("ok"):
        raise SystemExit(f"Telegram refused {method}: {body.get('description')}")
    return body["result"]


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in .env first.")

    print(f"Bot: @{telegram(token, 'getMe')['username']}")
    chats = {}
    for update in telegram(token, "getUpdates"):
        message = update.get("message") or update.get("edited_message")
        if message:
            sender = message.get("from", {})
            chats[message["chat"]["id"]] = (message["chat"]["type"], sender.get("first_name"), sender.get("username"))

    if not chats:
        print("No messages yet. Send your bot a message in Telegram, then run this again.")
    for chat_id, (chat_type, name, username) in chats.items():
        print(f"chat ID {chat_id}: {chat_type} chat with {name} (@{username})")


if __name__ == "__main__":
    main()
