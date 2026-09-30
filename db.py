"""Local SQLite storage: schema and connection helper."""
import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent / "sleep_caffeine.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oura_nights (
    -- Oura's label for the night: the date you woke up (see DECISIONS.md #5).
    day               TEXT PRIMARY KEY CHECK (date(day) IS day),
    sleep_score       INTEGER,
    lowest_heart_rate INTEGER,       -- bpm, from the main (long_sleep) period
    bedtime_start     TEXT,          -- ISO 8601 with UTC offset, as Oura reports it
    bedtime_end       TEXT,
    fetched_at        TEXT NOT NULL  -- UTC time this row was last written by the sync
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


def connect(path=DB_FILE):
    """Open the database, creating tables and the view if they don't exist yet."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
