import unittest
from unittest import mock

from raceresults import system


class DiskUsageTests(unittest.TestCase):
    def test_percent_matches_df_and_ignores_reserved_blocks(self):
        usage = mock.Mock(total=1000, used=600, free=200)  # 200 reserved for root
        with mock.patch("raceresults.system.shutil.disk_usage", return_value=usage):
            result = system.disk_usage("/data")
        self.assertEqual(result, {"percent": 75.0, "used": 600, "total": 1000})

    def test_unreadable_path_reports_zero(self):
        with mock.patch("raceresults.system.shutil.disk_usage", side_effect=OSError):
            self.assertEqual(system.disk_usage("/missing"), {"percent": 0.0, "used": 0, "total": 0})

    def test_sampler_reports_disk_of_data_folder(self):
        sampler = system.Sampler("/data")
        with mock.patch("raceresults.system.disk_usage", return_value={"percent": 12.5, "used": 5, "total": 40}) as du:
            sampler.sample()
        du.assert_called_with("/data")
        self.assertEqual(system.state.stats["disk"], 12.5)
        self.assertEqual(system.state.stats["disk_used"], 5)
        self.assertEqual(system.state.stats["disk_total"], 40)


if __name__ == "__main__":
    unittest.main()
