import unittest

from raceresults.phone import normalise

MOBILE = "+447400123456"  # libphonenumber's example UK mobile; tests never send real texts


class PhoneNumbers(unittest.TestCase):
    def test_uk_forms(self):
        for raw in ("07400 123456", "07400123456", "7400123456", "447400123456", "+44 7400 123456",
                    "+44 (0)7400 123456", "0044 7400 123456", "7400123456.0", "07400-123-456"):
            self.assertEqual(normalise(raw, "GB"), (MOBILE, None), raw)

    def test_rejected(self):
        for raw in ("", None, "abc", "12345", "07400 1234564567", "+44 121 234 5678"):
            number, reason = normalise(raw, "GB")
            self.assertIsNone(number, raw)
            self.assertTrue(reason)

    def test_landline_reason(self):
        self.assertIn("Landline", normalise("0121 234 5678", "GB")[1])

    def test_other_region(self):
        self.assertEqual(normalise("+353 85 123 4567", "GB")[0], "+353851234567")


if __name__ == "__main__":
    unittest.main()
