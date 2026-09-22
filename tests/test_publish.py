"""The public results page: sections, positions and the privacy whitelist."""

import unittest
from unittest import mock

from raceresults import events, processing, publish, settings, webscorer
from tests.helpers import DbTestCase, race, racer


class Sections(unittest.TestCase):
    def test_structure_and_ties(self):
        rows = [
            {"name": "A", "distance": "Long", "gender": "Open", "category": "Senior", "time": "3:00:00", "secs": 10800},
            {"name": "B", "distance": "Long", "gender": "Female", "category": "Senior", "time": "3:10:00", "secs": 11400},
            {"name": "C", "distance": "Long", "gender": "Open", "category": "Vet 40", "time": "3:10:00", "secs": 11400},
            {"name": "D", "distance": "Long", "gender": "Open", "category": "Senior", "time": "3:20:00", "secs": 12000},
            {"name": "E", "distance": "Short", "gender": "Open", "category": "Senior", "time": "1:00:00", "secs": 3600},
        ]
        order = [["Short", "", ""], ["Long", "", ""], ["Long", "Female", ""], ["Long", "Open", ""],
                 ["Long", "Female", "Senior"], ["Long", "Open", "Senior"], ["Long", "Open", "Vet 40"]]
        out = publish.build_sections(rows, order)
        self.assertEqual([d["name"] for d in out], ["Short", "Long"])  # Webscorer's order
        long = out[1]
        self.assertEqual([s["title"] for s in long["sections"]],
                         ["Overall", "Female", "Open", "Senior · Female", "Senior · Open", "Vet 40 · Open"])
        overall = long["sections"][0]
        self.assertEqual([r["pos"] for r in overall["rows"]], [1, 2, 2, 4])  # tie shares 2nd
        self.assertEqual((overall["winner"], overall["winning_time"]), ("A", "3:00:00"))
        self.assertEqual(set(overall["rows"][0]), {"pos", "name", "category", "gender", "time", "medal"})


class Page(DbTestCase):
    def setUp(self):
        super().setUp()
        settings.update({"ftp_host": "ftp.example.org", "timing_company_name": "Test Timing"})
        self.event_id = events.create_event({"name": "Seaside Sprint", "race_id": "99", "event_date": "2026-09-12",
                                             "page_filename": "seaside2026.html"})
        data = race({"Long": [
            racer(9876, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", "07400123456", Info2="Hoodie"),
            racer(9877, "Sam", "Sample", "Senior", "Female", "DNS", "07400123457"),
        ]})
        processing.process_event(self.event_id, fetched=webscorer.extract(data))

    def test_only_public_fields(self):
        html = publish.render_bundle(self.event_id)["html"].decode()
        self.assertIn("Alex Example", html)
        self.assertIn("3:50:00.0", html)
        self.assertIn("Seaside Sprint", html)
        self.assertIn("Saturday 12 September 2026", html)
        for private in ("9876", "07400123456", "7400123456", "example.com", "Hoodie", "Sam Sample"):
            self.assertNotIn(private, html, private)  # no bibs, phones, emails, purchases or non-finishers

    def test_sections_start_closed_and_medals_only_when_on(self):
        html = publish.render_bundle(self.event_id)["html"].decode()
        self.assertNotIn("<details id=\"long-overall\" open", html)
        self.assertNotIn(" open>", html)
        self.assertNotIn(">Medal<", html)  # medal colours are off for this event
        from raceresults import medals
        config_id = medals.create_config("Test", [{"distance": "Long", "category": "Senior", "gender": "Open",
                                                   "gold": "4:00:00", "silver": "5:00:00", "bronze": "", "finisher": ""}])
        events.update_event(self.event_id, {"medal_config_id": config_id, "medal_colours": True})
        processing.process_event(self.event_id)
        html = publish.render_bundle(self.event_id)["html"].decode()
        self.assertIn(">Medal<", html)
        self.assertIn('class="pill gold">Gold<', html)

    def test_names_are_escaped(self):
        processing.save_edits(self.event_id, "9876", {"name": "<script>alert(1)</script>"})
        html = publish.render_bundle(self.event_id)["html"].decode()
        self.assertNotIn("<script>alert(1)", html)

    def test_uploads_only_when_changed(self):
        with mock.patch("raceresults.publish.upload") as upload:
            self.assertTrue(publish.publish_once()[0])
            self.assertEqual(upload.call_count, 1)
            self.assertEqual(list(upload.call_args.args[1]), ["seaside2026.html"])  # the event's own page name
            publish.publish_once()
            self.assertEqual(upload.call_count, 1)  # nothing changed
            processing.save_edits(self.event_id, "9876", {"time": "3:40:00"})
            publish.publish_once()
            self.assertEqual(upload.call_count, 2)
            publish.publish_once(force=True)
            self.assertEqual(upload.call_count, 3)

    def test_each_event_has_its_own_page(self):
        other = events.create_event({"name": "Hill Climb 2026", "race_id": "100"})
        with mock.patch("raceresults.publish.upload") as upload:
            publish.publish_once()
            events.activate(other)
            publish.publish_once()
        self.assertEqual([list(c.args[1]) for c in upload.call_args_list], [["seaside2026.html"], ["hillclimb2026.html"]])

    def test_upload_failure_reported(self):
        with mock.patch("raceresults.publish.upload", side_effect=publish.UploadError("530 Login incorrect")):
            ok, message = publish.publish_once()
        self.assertFalse(ok)
        self.assertIn("530", message)


if __name__ == "__main__":
    unittest.main()
