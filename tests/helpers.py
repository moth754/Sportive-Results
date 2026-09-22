"""Shared test helpers: a throwaway database and synthetic Webscorer data (no real people)."""

import copy
import tempfile
import unittest
from pathlib import Path

from raceresults import db, publish, settings
from raceresults.state import state


def racer(bib, first, last, category, gender, time, phone="", start="08:00:00.0", **extra):
    record = {
        "Place": "1", "Bib": str(bib), "Name": f"{first} {last}", "FirstName": first, "LastName": last,
        "TeamName": None, "Category": category, "Age": 40, "Gender": gender, "Info1": phone,
        "Email": f"{first.lower()}@example.com", "Time": time, "Difference": "-", "PercentBack": "-",
        "PercentWinning": "100%", "PercentAverage": "-", "PercentMedian": "-", "StartTime": start,
    }
    record.update(extra)
    return record


def race(distances, name="Test Race 2026"):
    """Build Webscorer-shaped JSON: Overall, per-gender and per-category groupings for each distance."""
    results = []
    for distance, racers in distances.items():
        racers = copy.deepcopy(racers)
        for r in racers:
            r["Distance"] = distance
        results.append({"Grouping": {"Distance": distance, "Overall": True}, "Racers": racers})
        genders = list(dict.fromkeys(r["Gender"] for r in racers))
        for gender in genders:
            results.append({"Grouping": {"Distance": distance, "Gender": gender},
                            "Racers": [r for r in racers if r["Gender"] == gender]})
        for gender in genders:
            for category in dict.fromkeys(r["Category"] for r in racers if r["Gender"] == gender):
                results.append({"Grouping": {"Distance": distance, "Gender": gender, "Category": category},
                                "Racers": [r for r in racers if r["Gender"] == gender and r["Category"] == category]})
    return {"RaceInfo": {"RaceId": 1, "Name": name, "Date": "Sep 12, 2026", "OrganizerName": "Test Org"},
            "Results": results}


MEDAL_ROWS = [
    {"distance": "Long", "category": "Senior", "gender": "Open",
     "gold": "4:00:00", "silver": "5:00:00", "bronze": "6:00:00", "finisher": "24:00:00"},
    {"distance": "Long", "category": "Senior", "gender": "Female",
     "gold": "4:30:00", "silver": "5:30:00", "bronze": "6:30:00", "finisher": "24:00:00"},
    {"distance": "Long", "category": "Vet 40", "gender": "Open",
     "gold": "4:10:00", "silver": "5:10:00", "bronze": "6:10:00", "finisher": "24:00:00"},
]


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        db.init(self.data_dir / "test.db")
        settings.reset_cache()
        publish.DATA_DIR = self.data_dir
        publish._uploaded.clear()
        state.sms_enabled = False

    def tearDown(self):
        state.sms_enabled = False
        db.conn().close()
        db._local.conn = None
        settings.reset_cache()
        self._tmp.cleanup()
