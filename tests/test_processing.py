"""The results pipeline and the SMS safeguards."""

import time
import unittest
from unittest import mock

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from raceresults import db, events, medals, processing, settings, sms, webscorer, workers
from raceresults.state import state
from tests.helpers import MEDAL_ROWS, DbTestCase, race, racer

PHONE = "07400123456"


def ok_response(*_args, **_kwargs):
    response = mock.Mock(status_code=201)
    response.json.return_value = {"sid": "SM" + "0" * 32}
    return response


class PipelineBase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.config_id = medals.create_config("Test medals", MEDAL_ROWS)
        settings.update({"webscorer_api_id": "1", "twilio_account_sid": "ACtest",
                         "twilio_auth_token": "token", "twilio_messaging_service_sid": "MGtest"})
        self.event_id = events.create_event({"name": "Test", "race_id": "123", "medal_config_id": self.config_id,
                                             "medal_colours": True})

    def fetch(self, *runners):
        processing.process_event(self.event_id, fetched=webscorer.extract(race({"Long": list(runners)})))

    def rows(self):
        return {r["bib"]: r for r in processing.results(self.event_id)}

    def log(self, status=None):
        entries = sms.log_entries(self.event_id)
        return [e for e in entries if status is None or e["status"] == status]

    def sms_on(self):
        sms.set_enabled(True, self.event_id)

    def drain(self, post=ok_response):
        with mock.patch("raceresults.sms.requests.post", side_effect=post) as fake:
            while sms.send_next():
                pass
        return fake


class Results(PipelineBase):
    def test_new_finisher_medal_and_update(self):
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", ""))
        self.assertFalse(self.rows()["1"]["finished"])
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0"))
        row = self.rows()["1"]
        self.assertTrue(row["finished"])
        self.assertEqual(row["medal"], "Gold")
        self.assertEqual(row["updates"], 0)
        self.assertIsNotNone(row["first_seen"])
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "4:30:00.0"))
        row = self.rows()["1"]
        self.assertEqual(row["medal"], "Silver")
        self.assertEqual(row["updates"], 1)  # the terminal's *** UPDATED ***

    def test_adjusted_time_counts(self):
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", AdjustedTime="5:10:00.0"))
        row = self.rows()["1"]
        self.assertEqual(row["time"], "5:10:00.0")
        self.assertEqual(row["finish_time"], "3:50:00.0")
        self.assertEqual(row["medal"], "Bronze")

    def test_edit_recalculates_and_survives_refetch(self):
        runner = racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0")
        self.fetch(runner)
        processing.save_edits(self.event_id, "1", {"time": "5:30:00", "name": "Alex  Edited"})
        row = self.rows()["1"]
        self.assertEqual((row["time"], row["medal"], row["name"]), ("5:30:00", "Bronze", "Alex Edited"))
        self.assertEqual(row["orig"]["time"], "3:50:00.0")
        self.fetch(runner)  # Webscorer still says 3:50, the edit wins
        self.assertEqual(self.rows()["1"]["medal"], "Bronze")
        processing.revert_edits(self.event_id, "1")
        self.assertEqual(self.rows()["1"]["medal"], "Gold")

    def test_edit_back_to_original_removes_override(self):
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0"))
        processing.save_edits(self.event_id, "1", {"category": "Vet 40"})
        processing.save_edits(self.event_id, "1", {"category": "Senior"})
        self.assertEqual(self.rows()["1"]["edited"], [])

    def test_bad_time_edit_rejected(self):
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0"))
        with self.assertRaises(processing.EditError):
            processing.save_edits(self.event_id, "1", {"time": "about 4 hours"})

    def test_racer_missing_from_fetch_is_hidden_not_deleted(self):
        a = racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0")
        b = racer(2, "Sam", "Sample", "Senior", "Female", "4:40:00.0")
        self.fetch(a, b)
        first_seen = self.rows()["2"]["first_seen"]
        self.fetch(a)
        self.assertNotIn("2", self.rows())
        self.fetch(a, b)
        self.assertEqual(self.rows()["2"]["first_seen"], first_seen)

    def test_zero_racer_response_ignored(self):
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0"))
        with mock.patch("raceresults.webscorer.fetch_race", return_value=race({"Long": []})):
            workers.poll_once(self.event_id)
        self.assertIn("1", self.rows())
        self.assertIn("no racers", state.fetch[self.event_id]["error"])


class Texts(PipelineBase):
    def test_sent_once(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        fake = self.drain()
        self.assertEqual(fake.call_count, 1)
        body = fake.call_args.kwargs["data"]
        self.assertEqual(body["To"], "+447400123456")
        self.assertEqual(body["Body"], "1 - Long - Senior - Open - 3:50:00.0 - Gold")
        self.assertEqual(body["MessagingServiceSid"], "MGtest")
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        self.assertEqual(self.drain().call_count, 0)
        self.assertEqual(self.rows()["1"]["sms"], "sent")

    def test_corrected_result_sends_one_correction(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        self.drain()
        processing.save_edits(self.event_id, "1", {"time": "4:30:00"})
        processing.process_event(self.event_id)
        fake = self.drain()
        self.assertEqual(fake.call_count, 1)
        self.assertTrue(fake.call_args.kwargs["data"]["Body"].endswith("4:30:00 - Silver"))
        processing.process_event(self.event_id)
        self.assertEqual(self.drain().call_count, 0)

    def test_queued_text_superseded_by_correction(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "4:30:00.0", PHONE))
        self.assertEqual(len(self.log("pending")), 1)
        self.assertEqual(len(self.log("superseded")), 1)
        self.assertEqual(self.drain().call_count, 1)

    def test_invalid_number_logged_once_then_retried_when_corrected(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", "12345"))
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", "12345"))
        failed = self.log("failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("Invalid phone", failed[0]["error"])
        self.assertEqual(self.drain().call_count, 0)
        processing.save_edits(self.event_id, "1", {"phone": PHONE})
        self.assertEqual(self.drain().call_count, 1)

    def test_medal_needed_when_medal_colours_on(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Vet 70", "Female/Male", "3:50:00.0", PHONE))  # no standard
        self.assertEqual(self.log(), [])
        events.update_event(self.event_id, {"medal_colours": False})
        processing.process_event(self.event_id)
        fake = self.drain()
        self.assertEqual(fake.call_args.kwargs["data"]["Body"], "1 - Long - Vet 70 - Open - 3:50:00.0")

    def test_info_message_once_per_racer(self):
        events.update_event(self.event_id, {"info_on": True, "info_message": "Collect your medal at the tent"})
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        fake = self.drain()
        self.assertEqual([c.kwargs["data"]["Body"] for c in fake.call_args_list][1], "Collect your medal at the tent")
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "4:30:00.0", PHONE))
        self.assertEqual(self.drain().call_count, 1)  # correction only, no second info text

    def test_nothing_queued_while_off_and_switching_off_cancels(self):
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        self.assertEqual(self.log(), [])
        self.assertEqual(sms.preview(self.event_id)["texts"], 1)
        self.sms_on()
        self.assertEqual(len(self.log("pending")), 1)
        sms.set_enabled(False, self.event_id)
        self.assertEqual(len(self.log("cancelled")), 1)
        self.sms_on()  # re-evaluated: queued again
        self.assertEqual(len(self.log("pending")), 1)

    def test_switching_event_turns_texts_off(self):
        self.sms_on()
        other = events.create_event({"name": "Other", "race_id": "456"})
        events.activate(other)
        self.assertFalse(state.sms_enabled)

    def test_interrupted_send_never_repeated(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        db.execute("UPDATE sms_log SET status = 'sending'")  # as if the power went mid-send
        processing.process_event(self.event_id)
        self.assertEqual(self.drain().call_count, 0)

    def test_connection_failure_retried_but_timeout_not(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        refused = requests.ConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "refused")))
        self.drain(post=mock.Mock(side_effect=refused))
        entry = self.log()[0]
        self.assertEqual(entry["status"], "pending")  # never reached Twilio: safe to retry
        db.execute("UPDATE sms_log SET next_attempt_at = 0")
        self.drain(post=mock.Mock(side_effect=requests.ReadTimeout()))
        entry = self.log()[0]
        self.assertEqual(entry["status"], "failed")  # may have been sent: don't retry automatically
        self.assertIn("may have been sent", entry["error"])

    def test_twilio_rejection_not_retried(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        rejected = mock.Mock(status_code=400)
        rejected.json.return_value = {"code": 21610, "message": "Attempt to send to unsubscribed recipient"}
        self.drain(post=mock.Mock(return_value=rejected))
        processing.process_event(self.event_id)
        self.assertEqual(self.drain().call_count, 0)
        self.assertIn("21610", self.log("failed")[0]["error"])

    def test_manual_resend(self):
        self.sms_on()
        self.fetch(racer(1, "Alex", "Example", "Senior", "Female/Male", "3:50:00.0", PHONE))
        self.drain(post=mock.Mock(side_effect=requests.ReadTimeout()))
        sms.resend(self.log("failed")[0]["id"])
        self.assertEqual(self.drain().call_count, 1)


if __name__ == "__main__":
    unittest.main()


class EditOptions(PipelineBase):
    def test_from_medals_then_from_results(self):
        self.fetch(racer(1, "Alex", "Example", "Vet 70", "Female/Male", "3:50:00.0"))
        options = processing.racer_detail(self.event_id, "1")["options"]
        self.assertEqual(options["source"], "medals")
        self.assertEqual(options["category"], ["Senior", "Vet 40"])  # from the medal configuration, in its order
        events.update_event(self.event_id, {"medal_config_id": None})
        options = processing.racer_detail(self.event_id, "1")["options"]
        self.assertEqual((options["source"], options["category"], options["gender"]), ("results", ["Vet 70"], ["Open"]))
