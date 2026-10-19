"""Build the weekly message and print it; with --send, also send it to my Telegram chat.

    python weekly_message.py [--db PATH] [--since 2026-10-01] [--as-of YYYY-MM-DD] [--send]

Without --send nothing is sent anywhere except the aggregate numbers to the AI model, and nothing
is logged. With --send, one row goes into the weekly_messages table (which path wrote the message).
Runs on a systemd timer on Sundays at 13:30 Prague time (systemd/oura-weekly.timer).
"""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import analysis
import verdict
from db import DB_FILE, connect

TELEGRAM_TIMEOUT_SECONDS = 30


def send_telegram(text, token, chat_id, post=requests.post):
    """Send plain text (no markdown parsing). Errors never include the URL, which contains the bot token."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = post(url, json={"chat_id": chat_id, "text": text}, timeout=TELEGRAM_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram send failed ({type(error).__name__})") from None


def log_message(db_path, message, label, sent):
    """Record which path wrote this message (AI or template) in the database, for counting later.
    Stores no message text. A failure to log is reported but never stops the message."""
    try:
        with closing(connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO weekly_messages (sent_at, source, attempts, label, problems, sent) VALUES (?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), message["source"], message["attempts"],
                 label, json.dumps(message["problems"]), int(sent)))
    except sqlite3.Error as error:
        print(f"[could not write the log ({type(error).__name__}); the message itself is unaffected]", file=sys.stderr)


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
        label = verdict.primary(result)["label"]
        try:
            send_telegram(message["text"], token, chat_id)
        except RuntimeError:
            log_message(args.db, message, label, sent=False)      # the path is still worth counting
            raise
        log_message(args.db, message, label, sent=True)
        print("[sent]", file=sys.stderr)


if __name__ == "__main__":
    main()
