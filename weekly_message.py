"""Build the weekly message and print it; with --send, also send it to my Telegram chat.

    python weekly_message.py [--db PATH] [--since 2026-10-01] [--as-of YYYY-MM-DD] [--send]

Without --send nothing is sent anywhere except the aggregate numbers to the AI model.
Runs on a systemd timer on Sundays at 13:30 Prague time (systemd/oura-weekly.timer).
"""
import argparse
import os
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

import analysis
import verdict
from db import DB_FILE

TELEGRAM_TIMEOUT_SECONDS = 30


def send_telegram(text, token, chat_id, post=requests.post):
    """Send plain text (no markdown parsing). Errors never include the URL, which contains the bot token."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = post(url, json={"chat_id": chat_id, "text": text}, timeout=TELEGRAM_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram send failed ({type(error).__name__})") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DB_FILE))
    parser.add_argument("--since", default=analysis.DEFAULT_SINCE)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--send", action="store_true", help="send to Telegram instead of only printing")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    with closing(analysis.open_read_only(args.db)) as conn:
        result = analysis.analyse(conn, args.since, args.as_of)
    message = verdict.build_message(result)
    print(message["text"])
    print(f"\n[source: {message['source']}, attempts: {message['attempts']}]", file=sys.stderr)
    for number, problems in enumerate(message["problems"], 1):
        if problems:
            print(f"[attempt {number} rejected: {'; '.join(problems)}]", file=sys.stderr)

    if args.send:
        token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env to send.")
        send_telegram(message["text"], token, chat_id)
        print("[sent]", file=sys.stderr)


if __name__ == "__main__":
    main()
