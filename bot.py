"""Telegram bot: asks about caffeine every evening and saves the answers in daily_log.

Run:  python bot.py
      python bot.py --ask-now   (also send today's question right away, e.g. to test)

Each question's buttons carry the evening date they're about, fixed when the question
is sent. An answer tapped at 00:30, or days later, still goes to that evening.
"""
import argparse
import logging
import os
import re
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from db import connect

PRAGUE = ZoneInfo("Europe/Prague")
# The time zone is attached to the time itself, so the scheduler fires at 21:00 Prague
# wall-clock time in both summer (CEST) and winter (CET) time.
ASK_AT = time(21, 0, tzinfo=PRAGUE)
# Catches evenings missed at 21:00 because the laptop was asleep or the bot wasn't running.
CATCH_UP_EVERY = timedelta(minutes=10)

QUESTIONS = {
    "caffeine": "Caffeine after 5pm today?",
    "tag": "Anything unusual tonight?",
}
CHOICES = {
    "caffeine": {"yes": "Yes", "no": "No"},
    "tag": {"alcohol": "Alcohol", "illness": "Illness", "travel": "Travel", "other": "Other", "none": "Nothing unusual"},
}
CALLBACK_PATTERN = re.compile(r"(caffeine|tag):(\d{4}-\d{2}-\d{2}):([a-z]+)")

log = logging.getLogger("bot")


# --- Pure helpers (no Telegram calls) -------------------------------------------------

def evening_due(now):
    """Return the evening (ISO date) whose question is due at `now`, or None before 21:00.

    Comparing Prague wall-clock times keeps this right across clock changes. After
    midnight the date rolls over and nothing is due until 21:00, so a missed question
    is never sent late and dated to the wrong evening.
    """
    local = now.astimezone(PRAGUE)
    return local.date().isoformat() if local.time() >= ASK_AT.replace(tzinfo=None) else None


def parse_callback(data):
    """Return (kind, evening, value) from a button's callback data, or None if invalid."""
    match = CALLBACK_PATTERN.fullmatch(data or "")
    if not match:
        return None
    kind, evening, value = match.groups()
    try:
        date.fromisoformat(evening)
    except ValueError:
        return None
    return (kind, evening, value) if value in CHOICES[kind] else None


def question_text(kind, evening, answer=None):
    text = f"{QUESTIONS[kind]}\n(evening of {date.fromisoformat(evening):%a %-d %b})"
    if answer:
        text += f"\n\nSaved: {CHOICES[kind][answer]}. Tap another button to change it."
    return text


def keyboard(kind, evening, selected=None):
    """Buttons for one question; the selected answer gets a check mark."""
    buttons = [
        InlineKeyboardButton(("✓ " if value == selected else "") + label, callback_data=f"{kind}:{evening}:{value}")
        for value, label in CHOICES[kind].items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


# --- Database -------------------------------------------------------------------------

def was_asked(conn, evening):
    return conn.execute("SELECT 1 FROM daily_log WHERE evening_date = ?", (evening,)).fetchone() is not None


def mark_asked(conn, evening):
    """Create the evening's row, explicitly 'unanswered' until a button is tapped."""
    conn.execute("INSERT OR IGNORE INTO daily_log (evening_date) VALUES (?)", (evening,))


def save_answer(conn, kind, evening, value):
    """Save an answer and return the previous one ('unanswered' / None if there was none)."""
    column = "caffeine_after_5pm" if kind == "caffeine" else "tag"
    row = conn.execute(f"SELECT {column} FROM daily_log WHERE evening_date = ?", (evening,)).fetchone()
    previous = row[0] if row else ("unanswered" if kind == "caffeine" else None)
    conn.execute(
        f"INSERT INTO daily_log (evening_date, {column}) VALUES (?, ?) "
        f"ON CONFLICT (evening_date) DO UPDATE SET {column} = excluded.{column}",
        (evening, value),
    )
    return previous


# --- Telegram handlers ----------------------------------------------------------------

async def ignore_strangers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler: drop anything not from my own chat."""
    allowed = context.bot_data["chat_id"]
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or user.id != allowed or chat.id != allowed:
        log.warning("Ignored an update from user %s in chat %s", user and user.id, chat and chat.id)
        raise ApplicationHandlerStop


async def send_question(context: ContextTypes.DEFAULT_TYPE, kind, evening):
    await context.bot.send_message(
        context.bot_data["chat_id"], question_text(kind, evening), reply_markup=keyboard(kind, evening)
    )


async def ask_evening(context: ContextTypes.DEFAULT_TYPE, evening):
    """Send the caffeine question for `evening` unless it was already sent."""
    with closing(connect()) as conn:
        if was_asked(conn, evening):
            return
    await send_question(context, "caffeine", evening)
    # Recorded only after a successful send, so a failed send is retried by the catch-up check.
    with closing(connect()) as conn, conn:
        mark_asked(conn, evening)
    log.info("Asked about the evening of %s", evening)


async def ask_if_due(context: ContextTypes.DEFAULT_TYPE):
    evening = evening_due(datetime.now(PRAGUE))
    if evening:
        await ask_evening(context, evening)


async def ask_now(context: ContextTypes.DEFAULT_TYPE):
    await ask_evening(context, datetime.now(PRAGUE).date().isoformat())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parsed = parse_callback(query.data)
    if parsed is None:
        await query.answer("Unknown button.")
        return
    kind, evening, value = parsed

    with closing(connect()) as conn, conn:
        previous = save_answer(conn, kind, evening, value)
    log.info("Saved %s=%s for the evening of %s (was %s)", kind, value, evening, previous)

    await query.answer(f"Saved: {CHOICES[kind][value]}")
    try:
        await query.edit_message_text(question_text(kind, evening, value), reply_markup=keyboard(kind, evening, value))
    except BadRequest as e:
        if "not modified" not in str(e).lower():  # tapping the already-selected button
            raise

    # Ask the follow-up only the first time; a corrected caffeine answer doesn't re-ask it.
    if kind == "caffeine" and previous == "unanswered":
        await send_question(context, "tag", evening)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! I'll ask about caffeine every evening at 21:00 Prague time.")


# --- Startup --------------------------------------------------------------------------

def build_application(token, chat_id):
    app = Application.builder().token(token).build()
    app.bot_data["chat_id"] = chat_id
    app.add_handler(TypeHandler(Update, ignore_strangers), group=-1)
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_daily(ask_if_due, ASK_AT, name="evening question")
    app.job_queue.run_repeating(ask_if_due, interval=CATCH_UP_EVERY, first=5, name="catch-up check")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ask-now", action="store_true", help="send today's question right away")
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request logs include the bot token

    load_dotenv(Path(__file__).resolve().parent / ".env")
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in .env first.")
    if not chat_id or not chat_id.lstrip("-").isdigit():
        raise SystemExit("Put your TELEGRAM_CHAT_ID in .env first (run `python find_chat_id.py`).")

    app = build_application(token, int(chat_id))
    if args.ask_now:
        app.job_queue.run_once(ask_now, 1, name="ask now")
    log.info("Bot running. Next question at 21:00 Prague time. Press Ctrl-C to stop.")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
