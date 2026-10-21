"""Timezone regressions, including systems without an IANA database."""

from contextlib import ExitStack
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zoneinfo

from src.app import create_app
from src.data_retention.document import CourseDocumentBuilder
from src.storage.sqlite import CourseStore


class TimezoneTests(unittest.TestCase):
    def setUp(self):
        # Force the tzdata fallback even on macOS/Linux, which usually have
        # a system timezone database that would hide a missing dependency.
        self.original_tzpath = zoneinfo.TZPATH
        zoneinfo.reset_tzpath(())
        zoneinfo.ZoneInfo.clear_cache()
        self.addCleanup(self.restore_tzpath)

    def restore_tzpath(self):
        zoneinfo.reset_tzpath(self.original_tzpath)
        zoneinfo.ZoneInfo.clear_cache()

    def test_first_run_dashboard_without_system_timezones(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as connections:
            original_connect = CourseStore.connect

            def connect(store):
                connection = original_connect(store)
                # SQLite's context manager commits but does not close. Close
                # every handle before removing the temporary DB on Windows.
                connections.callback(connection.close)
                return connection

            with patch.object(CourseStore, "connect", connect), patch(
                "src.app.routes.api_key_present", return_value=False
            ):
                app = create_app(db_path=Path(directory) / "moodle.sqlite")
                app.testing = True
                response = app.test_client().get("/api/dashboard")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["courses"], [])
            self.assertEqual(payload["state"]["screen"], "apikey")
            self.assertEqual(
                payload["today"],
                dt.datetime.fromtimestamp(
                    payload["now"], zoneinfo.ZoneInfo("Europe/Zurich")
                ).date().isoformat(),
            )

    def test_course_dates_keep_daylight_saving_without_system_timezones(self):
        builder = CourseDocumentBuilder(session=None)
        for month, expected_time in ((1, "13:00"), (7, "14:00")):
            with self.subTest(month=month):
                instant = dt.datetime(2026, month, 15, 12, tzinfo=dt.timezone.utc)
                self.assertEqual(
                    builder._split(int(instant.timestamp())),
                    (instant.date().isoformat(), expected_time),
                )


if __name__ == "__main__":
    unittest.main()
