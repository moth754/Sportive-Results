"""Events: results page file names and the upgrade from version 1 databases."""

import sqlite3
import unittest

from raceresults import db, events, settings
from tests.helpers import DbTestCase


class PageFileNames(DbTestCase):
    def test_default_from_name_and_uniqueness(self):
        first = events.create_event({"name": "Lands End 2026", "race_id": "1"})
        self.assertEqual(events.get_event(first)["page_filename"], "landsend2026.html")
        second = events.create_event({"name": "Lands End 2026", "race_id": "2"})  # same name, second page
        self.assertEqual(events.get_event(second)["page_filename"], "landsend2026-2.html")
        with self.assertRaises(events.EventError) as ctx:
            events.create_event({"name": "Other", "race_id": "3", "page_filename": "LandsEnd2026.html"})
        self.assertIn("already uses", str(ctx.exception))

    def test_typed_names_are_cleaned_and_checked(self):
        event_id = events.create_event({"name": "Moor to Sea", "race_id": "1", "page_filename": " moor2026 "})
        self.assertEqual(events.get_event(event_id)["page_filename"], "moor2026.html")
        for bad in ("../x.html", "a b.html", "results.php", "/index.html"):
            with self.assertRaises(events.EventError, msg=bad):
                events.update_event(event_id, {"page_filename": bad})
        events.update_event(event_id, {"name": "Moor to Sea 2027"})  # renaming keeps the web address
        self.assertEqual(events.get_event(event_id)["page_filename"], "moor2026.html")
        events.update_event(event_id, {"page_filename": ""})  # blank: a fresh default from the name
        self.assertEqual(events.get_event(event_id)["page_filename"], "moortosea2027.html")

    def test_medal_colours_default_and_info(self):
        with_config = events.create_event({"name": "A", "race_id": "1", "medal_config_id": None})
        self.assertFalse(events.get_public(with_config)["medal_colours"])
        events.update_event(with_config, {"medal_colours": "true", "info_on": True, "info_message": " Well done "})
        row = events.get_public(with_config)
        self.assertEqual((row["medal_colours"], row["info_on"], row["info_message"]), (True, True, "Well done"))

    def test_page_url(self):
        settings.update({"public_url": "https://example.com/results/"})
        event_id = events.create_event({"name": "Devon Grit 2026", "race_id": "1"})
        self.assertEqual(events.get_public(event_id)["page_url"], "https://example.com/results/devongrit2026.html")
        settings.update({"public_url": ""})
        self.assertEqual(events.get_public(event_id)["page_url"], "")


class UpgradeFromVersion1(unittest.TestCase):
    def test_column_added_and_names_filled(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT NOT NULL, event_date TEXT NOT NULL DEFAULT '',
                    race_id TEXT NOT NULL, medal_config_id INTEGER, organiser_logo TEXT, polling INTEGER NOT NULL DEFAULT 1,
                    race_info TEXT, group_order TEXT, adjusted_field TEXT, data_updated_at REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                INSERT INTO events(name, race_id, created_at, updated_at) VALUES ('Lands End 100 2026', '447118', 0, 0);
                INSERT INTO settings VALUES ('ftp_filename', '"index.html"');
                INSERT INTO settings VALUES ('medal_colours', 'true'), ('info_on', 'true'),
                    ('info_message', '"Mind the gap"');
                PRAGMA user_version = 1;
            """)
            old.close()
            db.init(path)
            settings.reset_cache()
            events.fill_missing_page_filenames()
            self.assertEqual(db.query_one("PRAGMA user_version")[0], db.SCHEMA_VERSION)
            self.assertEqual(db.query_one("SELECT page_filename FROM events")[0], "landsend1002026.html")
            self.assertIsNone(db.query_one("SELECT 1 FROM settings WHERE key = 'ftp_filename'"))
            row = db.query_one("SELECT medal_colours, info_on, info_message FROM events")
            self.assertEqual(tuple(row), (1, 1, "Mind the gap"))  # old global values carried onto the event
            self.assertIsNone(db.query_one("SELECT 1 FROM settings WHERE key = 'medal_colours'"))
            db.conn().close()
            db._local.conn = None


if __name__ == "__main__":
    unittest.main()
