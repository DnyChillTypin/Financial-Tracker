import sys
from unittest.mock import MagicMock

# Mock third-party dependencies before importing main
mock_modules = [
    'flask',
    'requests',
    'gspread',
    'gspread.exceptions',
    'oauth2client',
    'oauth2client.service_account',
    'dotenv'
]
for mod in mock_modules:
    sys.modules[mod] = MagicMock()

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

VIETNAM_TZ = timezone(timedelta(hours=7))

# Import main now that external modules are mocked
import main

HEADER = main.HEALTH_HEADER


def health_row(date="", sleep="", wake=""):
    """Build a health row with sleep (col G) / wake up (col H) timestamps."""
    row = [""] * main.HEALTH_WIDTH
    row[0] = date
    row[main.HEALTH_SLEEP_COL - 1] = sleep
    row[main.HEALTH_WAKE_COL - 1] = wake
    return row


class TestSleepWakeLogic(unittest.TestCase):

    def test_parse_time_cell(self):
        dt = main._parse_time_cell("2026-09-18 07:30:15")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 9, 18))
        self.assertEqual((dt.hour, dt.minute, dt.second), (7, 30, 15))
        self.assertEqual(dt.tzinfo, VIETNAM_TZ)

        # Delayed marker is ignored when parsing
        self.assertEqual(main._parse_time_cell("2026-09-18 23:15:00 (delayed)").hour, 23)

        # Empty and invalid cases
        self.assertIsNone(main._parse_time_cell(""))
        self.assertIsNone(main._parse_time_cell("x"))
        self.assertIsNone(main._parse_time_cell("2026-09-18"))

    def test_parse_note_timestamp(self):
        # Still used to migrate old cell notes
        dt = main._parse_note_timestamp("Logged: 2026-09-18 23:15:00\nDelayed: true")
        self.assertEqual(dt.hour, 23)
        self.assertIsNone(main._parse_note_timestamp(""))
        self.assertIsNone(main._parse_note_timestamp(None))
        self.assertIsNone(main._parse_note_timestamp("invalid note format"))

    def test_get_last_sleep_wake_event_empty(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [HEADER]
        self.assertIsNone(main.get_last_sleep_wake_event(mock_sheet))

    def test_get_last_sleep_wake_event_sleep_only(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [
            HEADER, health_row("18-09-2026", sleep="2026-09-18 23:00:00")]

        event_type, event_dt, row_idx, col_idx = main.get_last_sleep_wake_event(mock_sheet)
        self.assertEqual(event_type, "sleep")
        self.assertEqual(row_idx, 2)
        self.assertEqual(col_idx, main.HEALTH_SLEEP_COL)
        self.assertEqual(event_dt.hour, 23)

    def test_get_last_sleep_wake_event_alternating_wake_after_sleep(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [
            HEADER,
            health_row("17-09-2026", sleep="2026-09-17 23:00:00"),
            health_row("18-09-2026", wake="2026-09-18 07:00:00"),
        ]

        event_type, event_dt, row_idx, col_idx = main.get_last_sleep_wake_event(mock_sheet)
        self.assertEqual(event_type, "wake")
        self.assertEqual(row_idx, 3)
        self.assertEqual(col_idx, main.HEALTH_WAKE_COL)
        self.assertEqual(event_dt.day, 18)
        self.assertEqual(event_dt.hour, 7)

    def test_get_last_sleep_wake_event_same_row_comparison(self):
        mock_sheet = MagicMock()
        # Wake was logged at 07:00, Sleep was logged at 23:00 on the same row
        mock_sheet.get_all_values.return_value = [
            HEADER,
            health_row("18-09-2026", sleep="2026-09-18 23:00:00", wake="2026-09-18 07:00:00"),
        ]

        event_type, event_dt, row_idx, col_idx = main.get_last_sleep_wake_event(mock_sheet)
        self.assertEqual(event_type, "sleep")
        self.assertEqual(row_idx, 2)
        self.assertEqual(event_dt.hour, 23)

    @patch('main.send_message')
    @patch('main.get_health_sheet')
    def test_handle_sleep_wake_duplicate_within_4_hours(self, mock_get_sheet, mock_send):
        mock_sheet = MagicMock()
        mock_get_sheet.return_value = mock_sheet
        # Previous sleep was logged 1 hour ago
        now = datetime(2026, 9, 18, 12, 0, tzinfo=VIETNAM_TZ)
        prev_time = now - timedelta(hours=1)
        mock_sheet.get_all_values.return_value = [
            HEADER, health_row("18-09-2026", sleep=prev_time.strftime("%Y-%m-%d %H:%M:%S"))]

        main.handle_sleep_wake_log("sleep", "test_sender", occurred_at=now)

        # Must overwrite the timestamp cell instead of appending a new row
        mock_sheet.update.assert_called_with("G2", [["2026-09-18 12:00:00"]])
        self.assertIn("time updated", mock_send.call_args[0][1])
        self.assertIn("overwrote", mock_send.call_args[0][1])

    @patch('main.send_message')
    @patch('main.handle_health_entry')
    @patch('main.get_health_sheet')
    def test_handle_sleep_wake_duplicate_after_4_hours(self, mock_get_sheet, mock_handle_health, mock_send):
        mock_sheet = MagicMock()
        mock_get_sheet.return_value = mock_sheet
        # Previous sleep was logged 6 hours ago
        now = datetime.now(VIETNAM_TZ)
        prev_time = now - timedelta(hours=6)
        mock_sheet.get_all_values.return_value = [
            HEADER, health_row(prev_time.strftime("%d-%m-%Y"),
                               sleep=prev_time.strftime("%Y-%m-%d %H:%M:%S"))]

        main.handle_sleep_wake_log("sleep", "test_sender")

        # Must log as new entry AND warn about missing Wake Up
        mock_handle_health.assert_called_once()
        self.assertIn("Warning: Missing Wake Up", mock_send.call_args[0][1])

    @patch('main.send_message')
    @patch('main.handle_health_entry')
    @patch('main.get_health_sheet')
    def test_handle_sleep_wake_alternating_normal(self, mock_get_sheet, mock_handle_health, mock_send):
        mock_sheet = MagicMock()
        mock_get_sheet.return_value = mock_sheet
        # Previous was sleep, now logging wake up
        mock_sheet.get_all_values.return_value = [
            HEADER, health_row("18-09-2026", sleep="2026-09-18 01:00:00")]

        main.handle_sleep_wake_log("wake", "test_sender")

        # Must log normally without warning
        mock_handle_health.assert_called_once()
        self.assertIn("Wake up logged:", mock_send.call_args[0][1])
        self.assertNotIn("Warning", mock_send.call_args[0][1])

if __name__ == '__main__':
    unittest.main()
