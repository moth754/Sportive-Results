"""Settings (secrets, legacy import) and the web app's guards."""

import io
import unittest
from unittest import mock

from raceresults import settings
from raceresults.app import create_app
from raceresults.state import state
from tests.helpers import DbTestCase

LEGACY = """Parameter,Value
APIID,11111
RACEID,123456
TextSend,True
MedalColours,True
TSID,ACtest
Ttoken,secret-token
TFrom,447400123456
MessagingServiceSID,MGtest
WEBSCORERTOKEN,abcd1234
RefreshTime,30
InfoOn,False
InfoMessage,Collect your medal, and a drink, at the tent"""


class Settings(DbTestCase):
    def test_legacy_import(self):
        imported, event_values, race_id = settings.import_legacy_config(LEGACY)
        s = settings.get_all()
        self.assertEqual(race_id, "123456")
        self.assertEqual(s["webscorer_api_id"], "11111")
        self.assertEqual(s["refresh_seconds"], 30)
        self.assertEqual(event_values, {"medal_colours": "True", "info_on": "False",
                                        "info_message": "Collect your medal, and a drink, at the tent"})
        self.assertFalse(state.sms_enabled)  # TextSend is ignored: texts always start off
        self.assertNotIn("TextSend", imported)

    def test_secrets_never_leave(self):
        settings.update({"webscorer_token": "abcd1234", "twilio_auth_token": "secret-token"})
        view = settings.public_view()
        self.assertNotIn("abcd1234", str(view))
        self.assertNotIn("secret-token", str(view))
        self.assertTrue(view["webscorer_token_set"])

    def test_blank_secret_keeps_value_and_clear_removes(self):
        settings.update({"ftp_password": "pw1"})
        settings.update({"ftp_password": ""})
        self.assertEqual(settings.get("ftp_password"), "pw1")
        settings.update({}, clear=["ftp_password"])
        self.assertEqual(settings.get("ftp_password"), "")

    def test_validation(self):
        for values in ({"refresh_seconds": 1}, {"ftp_protocol": "gopher"}, {"phone_region": "ZZ"}):
            with self.assertRaises(settings.SettingsError, msg=values):
                settings.update(values)

    def test_public_url_gets_a_scheme(self):
        settings.update({"public_url": "example.com/results/"})
        self.assertEqual(settings.get("public_url"), "https://example.com/results/")


class WebApp(DbTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.data_dir, start_workers=False)
        self.client = self.app.test_client()
        self.headers = {"X-Requested-With": "raceresults"}

    def test_state_changes_need_header(self):
        self.assertEqual(self.client.post("/api/sms/enable", json={"on": False}).status_code, 403)
        self.assertEqual(self.client.post("/api/sms/enable", json={"on": False}, headers=self.headers).status_code, 200)

    def test_texts_cannot_start_without_twilio(self):
        self.client.post("/api/events", json={"name": "E", "race_id": "1"}, headers=self.headers)
        response = self.client.post("/api/sms/enable", json={"on": True}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(state.sms_enabled)

    def test_password(self):
        self.client.post("/api/settings/password", json={"password": "letmein"}, headers=self.headers)
        other = self.app.test_client()
        self.assertEqual(other.get("/api/status").status_code, 401)
        self.assertEqual(other.get("/").status_code, 302)
        other.post("/login", data={"password": "letmein"})
        self.assertEqual(other.get("/api/status").status_code, 200)

    def test_settings_api_masks_secrets(self):
        self.client.put("/api/settings", json={"values": {"webscorer_token": "abcd1234"}}, headers=self.headers)
        body = self.client.get("/api/settings").get_data(as_text=True)
        self.assertNotIn("abcd1234", body)

    def test_legacy_import_applies_medal_and_info_to_active_event(self):
        self.client.post("/api/events", json={"name": "E", "race_id": "1"}, headers=self.headers)
        response = self.client.post("/api/settings/import-legacy", headers=self.headers,
                                    data={"file": (io.BytesIO(LEGACY.encode()), "Config.csv")})
        self.assertEqual(response.get_json()["applied_to"], "E")
        event = self.client.get("/api/events").get_json()["events"][0]
        self.assertTrue(event["medal_colours"])
        self.assertEqual(event["info_message"], "Collect your medal, and a drink, at the tent")

    def test_medal_csv_import_via_api(self):
        csv_bytes = b"Distance,Category,Gender,Gold,Silver,Bronze,Finisher\nLong,Senior,Open,4:00:00,5:00:00,6:00:00,24:00:00\n"
        response = self.client.post("/api/medal-configs/import", headers=self.headers,
                                    data={"file": (io.BytesIO(csv_bytes), "Seaside_Medals.csv")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["name"], "Seaside Medals")

    def test_shutdown_refused_without_permission(self):
        with mock.patch("raceresults.system.can_power_off", return_value=False), \
                mock.patch("raceresults.system.power_off") as power_off:
            response = self.client.post("/api/shutdown", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        power_off.assert_not_called()


if __name__ == "__main__":
    unittest.main()
