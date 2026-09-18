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

class TestSleepWakeLogic(unittest.TestCase):

    def test_parse_note_timestamp(self):
        note = "Logged: 2026-09-18 07:30:15"
        dt = main._parse_note_timestamp(note)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 9)
        self.assertEqual(dt.day, 18)
        self.assertEqual(dt.hour, 7)
        self.assertEqual(dt.minute, 30)
        self.assertEqual(dt.second, 15)
        self.assertEqual(dt.tzinfo, VIETNAM_TZ)

        # Empty and invalid cases
        self.assertIsNone(main._parse_note_timestamp(""))
        self.assertIsNone(main._parse_note_timestamp(None))
        self.assertIsNone(main._parse_note_timestamp("invalid note format"))

        # Multiline note
        multiline = "Logged: 2026-09-18 23:15:00\nSome other info"
        dt2 = main._parse_note_timestamp(multiline)
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.hour, 23)

    def test_get_last_sleep_wake_event_empty(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"]
        ]
        result = main.get_last_sleep_wake_event(mock_sheet)
        self.assertIsNone(result)

    def test_get_last_sleep_wake_event_sleep_only(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
            ["18-09-2026", "", "", "", "x", "", ""]
        ]
        mock_sheet.get_note.return_value = "Logged: 2026-09-18 23:00:00"

        event_type, event_dt, row_idx, col_idx = main.get_last_sleep_wake_event(mock_sheet)
        self.assertEqual(event_type, "sleep")
        self.assertEqual(row_idx, 2)
        self.assertEqual(col_idx, 5)
        self.assertEqual(event_dt.hour, 23)

    def test_get_last_sleep_wake_event_alternating_wake_after_sleep(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
            ["17-09-2026", "", "", "", "x", "", ""],
            ["18-09-2026", "", "", "", "", "x", ""]
        ]
        def mock_get_note(a1):
            if a1 == "E2":
                return "Logged: 2026-09-17 23:00:00"
            if a1 == "F3":
                return "Logged: 2026-09-18 07:00:00"
            return ""
        mock_sheet.get_note.side_effect = mock_get_note

        event_type, event_dt, row_idx, col_idx = main.get_last_sleep_wake_event(mock_sheet)
        self.assertEqual(event_type, "wake")
        self.assertEqual(row_idx, 3)
        self.assertEqual(col_idx, 6)
        self.assertEqual(event_dt.day, 18)
        self.assertEqual(event_dt.hour, 7)

    def test_get_last_sleep_wake_event_same_row_comparison(self):
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
            ["18-09-2026", "", "", "", "x", "x", ""]
        ]
        # Wake was logged at 07:00, Sleep was logged at 23:00 on the same row
        def mock_get_note(a1):
            if a1 == "E2":
                return "Logged: 2026-09-18 23:00:00"
            if a1 == "F2":
                return "Logged: 2026-09-18 07:00:00"
            return ""
        mock_sheet.get_note.side_effect = mock_get_note

        event_type, event_dt, row_idx, col_idx = main.get_last_sleep_wake_event(mock_sheet)
        self.assertEqual(event_type, "sleep")
        self.assertEqual(row_idx, 2)
        self.assertEqual(event_dt.hour, 23)

    @patch('main.send_message')
    @patch('main.get_health_sheet')
    def test_handle_sleep_wake_duplicate_within_4_hours(self, mock_get_sheet, mock_send):
        mock_sheet = MagicMock()
        mock_get_sheet.return_value = mock_sheet
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
            ["18-09-2026", "", "", "", "x", "", ""]
        ]
        # Previous sleep was logged 1 hour ago
        now = datetime.now(VIETNAM_TZ)
        prev_time = now - timedelta(hours=1)
        mock_sheet.get_note.return_value = f"Logged: {prev_time.strftime('%Y-%m-%d %H:%M:%S')}"

        main.handle_sleep_wake_log("sleep", "test_sender")

        # Must overwrite cell note instead of appending a new row
        mock_sheet.update_cell.assert_called_with(2, 5, "x")
        mock_sheet.update_note.assert_called()
        self.assertIn("time updated", mock_send.call_args[0][1])
        self.assertIn("overwrote", mock_send.call_args[0][1])

    @patch('main.send_message')
    @patch('main.handle_health_entry')
    @patch('main.get_health_sheet')
    def test_handle_sleep_wake_duplicate_after_4_hours(self, mock_get_sheet, mock_handle_health, mock_send):
        mock_sheet = MagicMock()
        mock_get_sheet.return_value = mock_sheet
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
            ["18-09-2026", "", "", "", "x", "", ""]
        ]
        # Previous sleep was logged 6 hours ago
        now = datetime.now(VIETNAM_TZ)
        prev_time = now - timedelta(hours=6)
        mock_sheet.get_note.return_value = f"Logged: {prev_time.strftime('%Y-%m-%d %H:%M:%S')}"

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
        mock_sheet.get_all_values.return_value = [
            ["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
            ["18-09-2026", "", "", "", "x", "", ""]
        ]
        # Previous was sleep, now logging wake up
        mock_sheet.get_note.return_value = "Logged: 2026-09-18 01:00:00"

        main.handle_sleep_wake_log("wake", "test_sender")

        # Must log normally without warning
        mock_handle_health.assert_called_once()
        self.assertIn("Wake up logged:", mock_send.call_args[0][1])
        self.assertNotIn("Warning", mock_send.call_args[0][1])

if __name__ == '__main__':
    unittest.main()
