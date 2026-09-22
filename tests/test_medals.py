import unittest

from raceresults import medals
from tests.helpers import MEDAL_ROWS, DbTestCase


class TimeParsing(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(medals.time_to_seconds("4:26:32.8"), 4 * 3600 + 26 * 60 + 32.8)
        self.assertEqual(medals.time_to_seconds("26:45.0"), 26 * 60 + 45)
        self.assertEqual(medals.time_to_seconds("08:35:00"), 8 * 3600 + 35 * 60)
        self.assertEqual(medals.time_to_seconds(" 24:00:00 "), 24 * 3600)

    def test_not_times(self):
        for value in ("DNS", "DNF", "-", "", None, "abc", "1:2:3:4", ":30"):
            self.assertIsNone(medals.time_to_seconds(value), value)

    def test_typed_result_times(self):
        self.assertTrue(medals.valid_result_time("4:26:32.8"))
        self.assertTrue(medals.valid_result_time("26:45"))
        self.assertFalse(medals.valid_result_time("4:75:00"))
        self.assertFalse(medals.valid_result_time("four hours"))


class Matching(unittest.TestCase):
    def setUp(self):
        self.table = medals.MedalTable(MEDAL_ROWS)

    def test_levels_in_order(self):
        m = lambda t: self.table.medal_for("Long", "Senior", "Open", medals.time_to_seconds(t))
        self.assertEqual(m("3:59:59.9"), "Gold")
        self.assertEqual(m("4:00:00"), "Gold")  # on the limit counts
        self.assertEqual(m("4:00:00.1"), "Silver")
        self.assertEqual(m("5:59:00"), "Bronze")
        self.assertEqual(m("7:00:00"), "Finisher")
        self.assertEqual(m("25:00:00"), "")

    def test_case_and_spaces_ignored(self):
        self.assertEqual(self.table.medal_for(" long ", "SENIOR", "open", 100), "Gold")
        self.assertEqual(self.table.medal_for("Long", "Vet  40", "Open", 100), "Gold")

    def test_no_standard_or_time(self):
        self.assertEqual(self.table.medal_for("Short", "Senior", "Open", 100), "")
        self.assertEqual(self.table.medal_for("Long", "Senior", "Open", None), "")

    def test_blank_level_not_awarded(self):
        table = medals.MedalTable([{**MEDAL_ROWS[0], "bronze": "", "finisher": ""}])
        self.assertEqual(table.medal_for("Long", "Senior", "Open", medals.time_to_seconds("4:30:00")), "Silver")
        self.assertEqual(table.medal_for("Long", "Senior", "Open", medals.time_to_seconds("5:30:00")), "")


class CsvImport(unittest.TestCase):
    GOOD = ("﻿Distance,Category,Gender,Gold,Silver,Bronze,Finisher\n"
            "Long,Senior,Open,4:00:00,5:00:00,6:00:00,24:00:00\n"
            "\n"
            "Long,Vet 40,Female,08:35:00,9:10:00,,24:00:00\n")

    def test_parse(self):
        rows = medals.parse_csv(medals.decode_upload(self.GOOD.encode("utf-8")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["gold"], "08:35:00")
        self.assertEqual(rows[1]["bronze"], "")

    def test_problems_reported_with_line_numbers(self):
        bad = "Distance,Category,Gender,Gold,Silver,Bronze,Finisher\nLong,Senior,Open,4 hours,5:00:00,6:00:00,\nLong,Senior,Open,4:00:00,,,\n"
        with self.assertRaises(medals.MedalError) as ctx:
            medals.parse_csv(bad)
        problems = "\n".join(ctx.exception.problems)
        self.assertIn("Line 2: Gold '4 hours'", problems)
        self.assertIn("Line 3: duplicates line 2", problems)

    def test_missing_headings(self):
        with self.assertRaises(medals.MedalError):
            medals.parse_csv("a,b,c\n1,2,3\n")

    def test_round_trip(self):
        rows = medals.parse_csv(self.GOOD)
        self.assertEqual(medals.parse_csv(medals.to_csv(rows)), rows)


class SavedConfigs(DbTestCase):
    def test_create_update_duplicate_delete(self):
        config_id = medals.create_config("Test medals", MEDAL_ROWS)
        self.assertEqual(len(medals.get_config(config_id)["rows"]), 3)
        with self.assertRaises(medals.MedalError):
            medals.create_config("test MEDALS", MEDAL_ROWS)  # names are unique, ignoring case
        medals.update_config(config_id, name="Renamed", rows=MEDAL_ROWS[:1])
        self.assertEqual(medals.get_config(config_id)["name"], "Renamed")
        copy_id = medals.duplicate_config(config_id, "Copy")
        self.assertEqual(medals.get_config(copy_id)["rows"], medals.get_config(config_id)["rows"])
        medals.delete_config(copy_id)
        self.assertIsNone(medals.get_config(copy_id))

    def test_invalid_rows_rejected(self):
        with self.assertRaises(medals.MedalError):
            medals.create_config("Bad", [{**MEDAL_ROWS[0], "gold": "soon"}])


if __name__ == "__main__":
    unittest.main()
