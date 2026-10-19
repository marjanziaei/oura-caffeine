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
    -- NULL = not answered yet; 'none' = answered "Nothing unusual".
    tag                TEXT CHECK (tag IN ('alcohol', 'illness', 'travel', 'other', 'none')),
    -- 1 = entered by hand after the fact, not via the bot on the evening (see DECISIONS.md #8).
    backfilled         INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1))
);
"""


# Each night paired with the answer about the evening before it (the one place the pairing rule
# lives, see DECISIONS.md #5). Evening D -> the night Oura labels D + 1. A missing answer shows
# as 'unanswered'. backfilled and pre_sleep_nap_minutes were added for the analysis (#12).
VIEW_NAME = "nights_with_caffeine"
VIEW_SQL = """
CREATE VIEW nights_with_caffeine AS
SELECT
    date(n.day, '-1 day')                        AS evening_date,
    COALESCE(l.caffeine_after_5pm, 'unanswered') AS caffeine_after_5pm,
    l.tag,
    l.backfilled,
    n.day                                        AS sleep_day,
    n.sleep_score,
    n.lowest_heart_rate,
    n.pre_sleep_nap_minutes,
    n.bedtime_start,
    n.bedtime_end
FROM oura_nights n
LEFT JOIN daily_log l ON l.evening_date = date(n.day, '-1 day');
"""

# One row per weekly message that was sent (or tried): which path wrote it, so I can count later.
# Holds no message text and no sleep data. Example: SELECT source, count(*) FROM weekly_messages GROUP BY source;
WEEKLY_LOG_SQL = """
CREATE TABLE IF NOT EXISTS weekly_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at   TEXT NOT NULL,                                  -- UTC time the run finished
    source    TEXT NOT NULL CHECK (source IN ('ai', 'template')),
    attempts  INTEGER NOT NULL,                               -- AI tries made (1 or 2)
    label     TEXT NOT NULL,                                  -- confidence label of the main result
    problems  TEXT NOT NULL,                                  -- JSON: what the checker rejected, per attempt
    sent      INTEGER NOT NULL CHECK (sent IN (0, 1))         -- 1 = Telegram accepted it
);
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
    "daily_log": {
        "backfilled": "INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1))",
    },
}


def connect(path=DB_FILE):
    """Open the database, creating or upgrading tables and the view as needed."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(WEEKLY_LOG_SQL)
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    ensure_view(conn)
    conn.commit()
    return conn


def ensure_view(conn):
    """Create the view, or replace it if an older version (missing columns) is in place.

    Done in one transaction so a reader never sees the view missing. It holds no data.
    """
    wanted = {"evening_date", "caffeine_after_5pm", "tag", "backfilled", "sleep_day", "sleep_score",
              "lowest_heart_rate", "pre_sleep_nap_minutes", "bedtime_start", "bedtime_end"}
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({VIEW_NAME})")}
    if existing == wanted:
        return
    conn.commit()
    conn.execute("BEGIN")
    conn.execute(f"DROP VIEW IF EXISTS {VIEW_NAME}")
    conn.execute(VIEW_SQL)
    conn.commit()
