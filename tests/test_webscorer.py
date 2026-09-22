import unittest

from raceresults import webscorer
from tests.helpers import race, racer


class Extraction(unittest.TestCase):
    def test_overall_groupings_flattened(self):
        data = race({
            "Long": [racer(1, "Alex", "Example", "Senior", "Female/Male", "4:00:00.0", "07700900123"),
                     racer(2, "Sam", "Sample", "Senior", "Female", "DNS")],
            "Short": [racer(3, "Jo", "Test", "Vet 40", "Female", "1:30:00.5", Info2="T-shirt")],
        })
        racers, meta = webscorer.extract(data)
        self.assertEqual([r["bib"] for r in racers], ["1", "2", "3"])  # per-gender groupings don't duplicate
        first = racers[0]
        self.assertEqual(first["name"], "Alex Example")
        self.assertEqual(first["gender"], "Open")  # Female/Male -> Open, as before
        self.assertEqual(first["distance"], "Long")
        self.assertEqual(first["phone"], "07700900123")
        self.assertEqual(racers[2]["purchases"], "T-shirt")
        self.assertNotIn("Email", str(racers))  # email isn't kept
        self.assertEqual(meta["race_info"]["Name"], "Test Race 2026")
        self.assertIn(["Long", "Open", "Senior"], meta["group_order"])
        self.assertIsNone(meta["adjusted_field"])

    def test_name_fallback_and_duplicate_bibs(self):
        a = racer(5, "", "", "Senior", "Female", "1:00:00", Name="Only Name")
        b = racer(5, "Dup", "Licate", "Senior", "Female", "2:00:00")
        racers, _ = webscorer.extract(race({"Long": [a, b]}))
        self.assertEqual(len(racers), 1)
        self.assertEqual(racers[0]["name"], "Only Name")

    def test_no_bib_uses_name(self):
        racers, _ = webscorer.extract(race({"Long": [racer("-", "Pat", "Doe", "Senior", "Female", "1:00:00")]}))
        self.assertTrue(racers[0]["key"].startswith("~pat doe|"))

    def test_adjusted_time_detected(self):
        for key in ("AdjustedTime", "Adjusted time", "AdjTime"):
            data = race({"Long": [racer(1, "A", "B", "Senior", "Female", "4:00:00", **{key: "4:10:00"})]})
            racers, meta = webscorer.extract(data)
            self.assertEqual(meta["adjusted_field"], key)
            self.assertEqual(racers[0]["adjusted"], "4:10:00")

    def test_adjusted_field_ignores_look_alikes(self):
        self.assertIsNone(webscorer.find_adjusted_field(["Time", "Difference", "PercentBack"]))
        self.assertEqual(webscorer.find_adjusted_field(["AdjustedTimeDifference", "AdjustedTime"]), "AdjustedTime")

    def test_parse_date(self):
        self.assertEqual(webscorer.parse_date("Aug 23, 2026"), "2026-08-23")
        self.assertEqual(webscorer.parse_date("nonsense"), "")


if __name__ == "__main__":
    unittest.main()
