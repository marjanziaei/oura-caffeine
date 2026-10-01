"""Local SQLite storage: schema and connection helper."""
import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent / "sleep_caffeine.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oura_nights (
    -- Oura's label for the night: the date you woke up (see DECISIONS.md #5).
    day               TEXT PRIMARY KEY CHECK (date(day) IS day),
    sleep_score       INTEGER,
    lowest_heart_rate INTEGER,       -- bpm, lowest across the main (long_sleep) periods
    bedtime_start     TEXT,          -- ISO 8601 with UTC offset, as Oura reports it
    bedtime_end       TEXT,
    fetched_at        TEXT NOT NULL, -- UTC time this row was last written by the sync
    long_sleep_count  INTEGER,       -- main sleep parts combined into this night (>1 = broken night)
    -- Minutes asleep in other (non-long_sleep) periods, split by when they happened:
    pre_sleep_nap_minutes  INTEGER,  -- before the night, e.g. evening dozing (can affect the night)
    post_wake_nap_minutes  INTEGER,  -- after waking (can't affect this night)
    adjacent_sleep_minutes INTEGER   -- within 30 min of the night: falling asleep / waking, not naps
);

CREATE TABLE IF NOT EXISTS daily_log (
    -- The evening the answer is about.
    evening_date       TEXT PRIMARY KEY CHECK (date(evening_date) IS evening_date),
    caffeine_after_5pm TEXT NOT NULL DEFAULT 'unanswered'
                       CHECK (caffeine_after_5pm IN ('yes', 'no', 'unanswered')),
    tag                TEXT CHECK (tag IN ('alcohol', 'illness', 'travel', 'other'))
);

-- Each night paired with the answer about the evening before it.
-- Evening D -> the night Oura labels D + 1. A missing answer shows as 'unanswered'.
CREATE VIEW IF NOT EXISTS nights_with_caffeine AS
SELECT
    date(n.day, '-1 day')                        AS evening_date,
    COALESCE(l.caffeine_after_5pm, 'unanswered') AS caffeine_after_5pm,
    l.tag,
    n.day                                        AS sleep_day,
    n.sleep_score,
    n.lowest_heart_rate,
    n.bedtime_start,
    n.bedtime_end
FROM oura_nights n
LEFT JOIN daily_log l ON l.evening_date = date(n.day, '-1 day');
"""


# Columns added after a table was first created. CREATE TABLE IF NOT EXISTS leaves
# existing tables alone, so connect() adds any of these that are missing.
ADDED_COLUMNS = {
    "oura_nights": {
        "long_sleep_count": "INTEGER",
        "pre_sleep_nap_minutes": "INTEGER",
        "post_wake_nap_minutes": "INTEGER",
        "adjacent_sleep_minutes": "INTEGER",
    },
}


def connect(path=DB_FILE):
    """Open the database, creating or upgrading tables and the view as needed."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    conn.commit()
    return conn
