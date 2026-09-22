"""SQLite storage. One connection per thread; WAL so the web app and workers don't block each other."""

import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medal_configs (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS medal_standards (
    id        INTEGER PRIMARY KEY,
    config_id INTEGER NOT NULL REFERENCES medal_configs(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    distance  TEXT NOT NULL,
    category  TEXT NOT NULL,
    gender    TEXT NOT NULL,
    gold      TEXT NOT NULL DEFAULT '',
    silver    TEXT NOT NULL DEFAULT '',
    bronze    TEXT NOT NULL DEFAULT '',
    finisher  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS medal_standards_config ON medal_standards(config_id, position);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    event_date      TEXT NOT NULL DEFAULT '',
    race_id         TEXT NOT NULL,
    medal_config_id INTEGER REFERENCES medal_configs(id) ON DELETE SET NULL,
    page_filename   TEXT NOT NULL DEFAULT '',  -- the event's results page on the website, e.g. landsend2026.html
    medal_colours   INTEGER NOT NULL DEFAULT 0, -- work out medal colours (and only text finishers who have one)
    info_on         INTEGER NOT NULL DEFAULT 0, -- send the information text after each finisher's result text
    info_message    TEXT NOT NULL DEFAULT '',
    organiser_logo  TEXT,               -- file name in data/logos
    polling         INTEGER NOT NULL DEFAULT 1,
    race_info       TEXT,               -- JSON: Webscorer RaceInfo (name, date, organiser...)
    group_order     TEXT,               -- JSON: [[distance, gender, category], ...] in Webscorer order
    adjusted_field  TEXT,               -- racer key holding the adjusted time, if Webscorer sends one
    data_updated_at REAL,               -- last time fetched data changed
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS racers (
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    racer_key     TEXT NOT NULL,
    raw           TEXT NOT NULL,        -- JSON: trimmed Webscorer record (the offline cache)
    present       INTEGER NOT NULL DEFAULT 1,
    bib           TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    distance      TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    gender        TEXT NOT NULL DEFAULT '',
    finish_time   TEXT NOT NULL DEFAULT '',   -- Webscorer "Time"
    adjusted_time TEXT NOT NULL DEFAULT '',   -- Webscorer adjusted time, if any
    result_time   TEXT NOT NULL DEFAULT '',   -- the time that counts: edit > adjusted > finish
    result_secs   REAL,                        -- NULL when not finished
    medal         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    purchases     TEXT NOT NULL DEFAULT '',
    edited        TEXT NOT NULL DEFAULT '[]',  -- JSON list of edited fields
    fingerprint   TEXT,
    first_seen_at REAL,
    changed_at    REAL,
    update_count  INTEGER NOT NULL DEFAULT 0,
    finish_clock  REAL,                        -- StartTime + Time, used to order same-moment arrivals
    PRIMARY KEY (event_id, racer_key)
);

CREATE TABLE IF NOT EXISTS edits (
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    racer_key TEXT NOT NULL,
    field     TEXT NOT NULL,
    value     TEXT NOT NULL,
    edited_at REAL NOT NULL,
    PRIMARY KEY (event_id, racer_key, field)
);

CREATE TABLE IF NOT EXISTS sms_log (
    id              INTEGER PRIMARY KEY,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    racer_key       TEXT NOT NULL,
    bib             TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,             -- result | info
    message         TEXT NOT NULL,
    raw_number      TEXT NOT NULL DEFAULT '',  -- number as entered (Info1 or edit)
    to_number       TEXT NOT NULL DEFAULT '',  -- E.164 number actually used
    status          TEXT NOT NULL,             -- pending|sending|sent|failed|cancelled|superseded
    error           TEXT NOT NULL DEFAULT '',
    twilio_sid      TEXT NOT NULL DEFAULT '',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sms_log_racer ON sms_log(event_id, racer_key, kind);
CREATE INDEX IF NOT EXISTS sms_log_status ON sms_log(status, next_attempt_at);
"""

_local = threading.local()
_db_path = None


def init(path):
    """Open (creating if needed) the database at `path`, upgrading an older one in place."""
    global _db_path
    _db_path = str(path)
    c = conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    _migrate(c)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate(c):
    """Bring a database made by an earlier version up to date (CREATE TABLE IF NOT EXISTS won't add columns)."""
    event_columns = {row["name"] for row in c.execute("PRAGMA table_info(events)")}
    if "page_filename" not in event_columns:  # version 2: results page file name moved from settings to events
        c.execute("ALTER TABLE events ADD COLUMN page_filename TEXT NOT NULL DEFAULT ''")
    if "medal_colours" not in event_columns:  # version 3: medal colours and the info text are per event
        c.execute("ALTER TABLE events ADD COLUMN medal_colours INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE events ADD COLUMN info_on INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE events ADD COLUMN info_message TEXT NOT NULL DEFAULT ''")
        old = {row["key"]: row["value"] for row in
               c.execute("SELECT key, value FROM settings WHERE key IN ('medal_colours', 'info_on', 'info_message')")}
        c.execute("UPDATE events SET medal_colours = ?, info_on = ?, info_message = ?",
                  (1 if old.get("medal_colours") == "true" else 0, 1 if old.get("info_on") == "true" else 0,
                   json.loads(old["info_message"]) if old.get("info_message") else ""))
        c.execute("DELETE FROM settings WHERE key IN ('medal_colours', 'info_on', 'info_message')")


def conn():
    """This thread's connection (autocommit; use transaction() to group writes)."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _db_path:
        if _db_path is None:
            raise RuntimeError("Database not initialised")
        c = sqlite3.connect(_db_path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn, _local.path = c, _db_path
    return c


@contextmanager
def transaction():
    """BEGIN IMMEDIATE ... COMMIT. Nested use joins the outer transaction."""
    c = conn()
    if c.in_transaction:
        yield c
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def query(sql, params=()):
    return conn().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return conn().execute(sql, params).fetchone()


def execute(sql, params=()):
    return conn().execute(sql, params)


def checkpoint():
    """Flush the WAL into the main file (before power-off)."""
    conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
