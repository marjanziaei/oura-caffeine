"""Fetch the last 30 days of Oura sleep data into the local database.

Safe to run repeatedly: each night is one row keyed by its date, so a re-run
updates rows in place instead of adding duplicates. Never writes to daily_log.

Run:  python sync_oura.py
"""
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from db import DB_FILE, connect
from oura_client import api_get

DAYS = 30

UPSERT = """
INSERT INTO oura_nights (day, sleep_score, lowest_heart_rate, bedtime_start, bedtime_end, fetched_at)
VALUES (:day, :sleep_score, :lowest_heart_rate, :bedtime_start, :bedtime_end, :fetched_at)
ON CONFLICT (day) DO UPDATE SET
    sleep_score       = excluded.sleep_score,
    lowest_heart_rate = excluded.lowest_heart_rate,
    bedtime_start     = excluded.bedtime_start,
    bedtime_end       = excluded.bedtime_end,
    fetched_at        = excluded.fetched_at
"""

_WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE}


def deny_daily_log_writes(action, table, *_):
    """SQLite authorizer: the sync's connection may read daily_log but never change it."""
    if table == "daily_log" and action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def main_sleep_by_day(periods):
    """Return {day: period} for the main (long_sleep) period of each day, longest if several."""
    best = {}
    for period in periods:
        if period.get("type") != "long_sleep":
            continue
        length = datetime.fromisoformat(period["bedtime_end"]) - datetime.fromisoformat(period["bedtime_start"])
        if period["day"] not in best or length > best[period["day"]][0]:
            best[period["day"]] = (length, period)
    return {day: period for day, (_, period) in best.items()}


def main():
    today = date.today()
    first_day = (today - timedelta(days=DAYS - 1)).isoformat()
    # Pad by a day on each side: the sleep endpoint can omit the night labelled with
    # start_date itself. Days outside the window are dropped below.
    params = {
        "start_date": (today - timedelta(days=DAYS)).isoformat(),
        "end_date": (today + timedelta(days=1)).isoformat(),
    }

    scores = {doc["day"]: doc["score"] for doc in api_get("daily_sleep", params)}
    main_sleeps = main_sleep_by_day(api_get("sleep", params))

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for day in sorted(set(scores) | set(main_sleeps)):
        if day < first_day:
            continue
        sleep = main_sleeps.get(day, {})
        rows.append({
            "day": day,
            "sleep_score": scores.get(day),
            "lowest_heart_rate": sleep.get("lowest_heart_rate"),
            "bedtime_start": sleep.get("bedtime_start"),
            "bedtime_end": sleep.get("bedtime_end"),
            "fetched_at": fetched_at,
        })

    with closing(connect()) as conn:
        conn.set_authorizer(deny_daily_log_writes)
        with conn:  # one transaction: all rows are saved, or none are
            conn.executemany(UPSERT, rows)

    if rows:
        print(f"Synced {len(rows)} nights ({rows[0]['day']} to {rows[-1]['day']}) into {DB_FILE.name}.")
    else:
        print("Oura returned no sleep data for the last 30 days.")


if __name__ == "__main__":
    main()
