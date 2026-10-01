"""Fetch the last 30 days of Oura sleep data into the local database.

Safe to run repeatedly: each night is one row keyed by its date, so a re-run
updates rows in place instead of adding duplicates. Never writes to daily_log.

Run:  python sync_oura.py              (last 30 days)
      python sync_oura.py --days 90    (further back, e.g. to fill in older rows)
"""
import argparse
import logging
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from db import DB_FILE, connect
from oura_client import api_get

DAYS = 30

# Short periods ending or starting this close to the night count as falling asleep or
# waking up, not as naps (see DECISIONS.md #6).
ADJACENT_MINUTES = 30

log = logging.getLogger("sync_oura")

UPSERT = """
INSERT INTO oura_nights (day, sleep_score, lowest_heart_rate, bedtime_start, bedtime_end, fetched_at,
                         long_sleep_count, pre_sleep_nap_minutes, post_wake_nap_minutes, adjacent_sleep_minutes)
VALUES (:day, :sleep_score, :lowest_heart_rate, :bedtime_start, :bedtime_end, :fetched_at,
        :long_sleep_count, :pre_sleep_nap_minutes, :post_wake_nap_minutes, :adjacent_sleep_minutes)
ON CONFLICT (day) DO UPDATE SET
    sleep_score            = excluded.sleep_score,
    lowest_heart_rate      = excluded.lowest_heart_rate,
    bedtime_start          = excluded.bedtime_start,
    bedtime_end            = excluded.bedtime_end,
    fetched_at             = excluded.fetched_at,
    long_sleep_count       = excluded.long_sleep_count,
    pre_sleep_nap_minutes  = excluded.pre_sleep_nap_minutes,
    post_wake_nap_minutes  = excluded.post_wake_nap_minutes,
    adjacent_sleep_minutes = excluded.adjacent_sleep_minutes
"""

_WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE}


def deny_daily_log_writes(action, table, *_):
    """SQLite authorizer: the sync's connection may read daily_log but never change it."""
    if table == "daily_log" and action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def summarize_days(periods):
    """Group sleep periods by Oura day and return {day: summary} (see summarize_day)."""
    by_day = defaultdict(list)
    for period in periods:
        if period.get("type") != "deleted":
            by_day[period["day"]].append(period)
    return {day: summarize_day(day, day_periods) for day, day_periods in by_day.items()}


def summarize_day(day, periods):
    """Combine a day's long_sleep periods into one night and total up its other sleep.

    Several long_sleep periods (a broken night) are combined: earliest start, latest
    end, lowest heart rate across the parts. Other periods within ADJACENT_MINUTES of
    the night are falling asleep or waking up; the rest are naps, split into before
    the night (which can affect it) and after waking (which can't).
    """
    parse = datetime.fromisoformat
    mains = sorted((p for p in periods if p["type"] == "long_sleep"), key=lambda p: parse(p["bedtime_start"]))
    summary = {"long_sleep_count": len(mains), "bedtime_start": None, "bedtime_end": None, "lowest_heart_rate": None}

    if len(mains) > 1:
        parts = ", ".join(f"{p['bedtime_start'][11:16]}-{p['bedtime_end'][11:16]}" for p in mains)
        log.warning("%s has %d long_sleep periods (%s); combining them into one night.", day, len(mains), parts)
    if mains:
        summary["bedtime_start"] = mains[0]["bedtime_start"]
        summary["bedtime_end"] = max(mains, key=lambda p: parse(p["bedtime_end"]))["bedtime_end"]
        heart_rates = [p["lowest_heart_rate"] for p in mains if p.get("lowest_heart_rate") is not None]
        summary["lowest_heart_rate"] = min(heart_rates) if heart_rates else None
        night_start, night_end = parse(summary["bedtime_start"]), parse(summary["bedtime_end"])

    others = [p for p in periods if p["type"] != "long_sleep"]
    if not mains:
        # Without a night there is no "before" or "after", so leave the split unknown.
        if others:
            log.warning("%s has no long_sleep period; its %d other periods are not counted.", day, len(others))
        summary.update(pre_sleep_nap_minutes=None, post_wake_nap_minutes=None, adjacent_sleep_minutes=None)
        return summary

    seconds = {"pre_sleep_nap_minutes": 0, "post_wake_nap_minutes": 0, "adjacent_sleep_minutes": 0}
    for period in others:
        start, end = parse(period["bedtime_start"]), parse(period["bedtime_end"])
        # Positive gap: time between this period and the night. Negative: they overlap.
        gap = max(night_start - end, start - night_end)
        if gap <= timedelta(minutes=ADJACENT_MINUTES):
            column = "adjacent_sleep_minutes"
        elif end <= night_start:
            column = "pre_sleep_nap_minutes"
        else:
            column = "post_wake_nap_minutes"
        seconds[column] += period.get("total_sleep_duration") or 0
    summary.update({column: round(total / 60) for column, total in seconds.items()})
    return summary


def main(days=DAYS):
    logging.basicConfig(format="%(levelname)s: %(message)s")
    today = date.today()
    first_day = (today - timedelta(days=days - 1)).isoformat()
    # Pad by a day on each side: the sleep endpoint can omit the night labelled with
    # start_date itself. Days outside the window are dropped below.
    params = {
        "start_date": (today - timedelta(days=days)).isoformat(),
        "end_date": (today + timedelta(days=1)).isoformat(),
    }

    scores = {doc["day"]: doc["score"] for doc in api_get("daily_sleep", params)}
    nights = summarize_days(api_get("sleep", params))

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    empty = dict.fromkeys(["long_sleep_count", "bedtime_start", "bedtime_end", "lowest_heart_rate",
                           "pre_sleep_nap_minutes", "post_wake_nap_minutes", "adjacent_sleep_minutes"])
    for day in sorted(set(scores) | set(nights)):
        if day < first_day:
            continue
        rows.append({"day": day, "sleep_score": scores.get(day), "fetched_at": fetched_at, **nights.get(day, empty)})

    with closing(connect()) as conn:
        conn.set_authorizer(deny_daily_log_writes)
        with conn:  # one transaction: all rows are saved, or none are
            conn.executemany(UPSERT, rows)

    if rows:
        print(f"Synced {len(rows)} nights ({rows[0]['day']} to {rows[-1]['day']}) into {DB_FILE.name}.")
    else:
        print(f"Oura returned no sleep data for the last {days} days.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=DAYS, help=f"how many days back to sync (default {DAYS})")
    main(parser.parse_args().days)
