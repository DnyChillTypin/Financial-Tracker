"""Offline regression tests for delayed logs and date placement."""
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

for module in ('flask', 'requests', 'gspread', 'gspread.exceptions',
               'oauth2client', 'oauth2client.service_account', 'dotenv'):
    sys.modules.setdefault(module, MagicMock())

import main


class Sheet:
    def __init__(self):
        self.rows = [["Date"] + [""] * 6]
        self.notes = {}

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def update(self, address, values):
        match = re.match(r'([A-G])(\d+)', address)
        col, row = ord(match[1]) - 65, int(match[2]) - 1
        while len(self.rows) < row + len(values):
            self.rows.append([""] * 7)
        for offset, cells in enumerate(values):
            self.rows[row + offset][col:col + len(cells)] = [str(v) for v in cells]

    def update_cell(self, row, col, value):
        self.update(f'{chr(64 + col)}{row}', [[value]])

    def update_note(self, address, value):
        self.notes[address] = value

    def get_note(self, address):
        return self.notes.get(address, "")

    def insert_rows(self, values, row):
        self.rows[row - 1:row - 1] = [cells[:] for cells in values]
        self.notes = {f'{a[0]}{int(a[1:]) + (len(values) if int(a[1:]) >= row else 0)}': v
                      for a, v in self.notes.items()}

    def row_values(self, row):
        return self.rows[row - 1][:]

    def delete_rows(self, row):
        del self.rows[row - 1]
        self.notes = {f'{a[0]}{int(a[1:]) - (1 if int(a[1:]) > row else 0)}': v
                      for a, v in self.notes.items() if int(a[1:]) != row}

    def insert_row(self, values, row):
        self.insert_rows([values], row)


class TestDelayedLogging(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 4, 2, 0, tzinfo=main.VIETNAM_TZ)
        self.finance, self.health = Sheet(), Sheet()
        main._logged_stack.clear()
        main._last_removed = None
        for target, value in (('get_finance_sheet', self.finance),
                              ('get_health_sheet', self.health)):
            patcher = patch.object(main, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(main, 'send_message')
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(main, 'datetime', wraps=datetime)
        clock = patcher.start()
        clock.now.return_value = self.now
        self.clock = clock
        self.addCleanup(patcher.stop)

    def log(self, text):
        main.parse_and_handle(text, 'sender')

    def assert_delayed_note(self, note, timestamp):
        self.assertEqual(note, f'Logged: {timestamp}\nDelayed: true')

    def test_spending_crosses_midnight_and_excludes_suffix(self):
        self.log('s 100 notes -4')
        self.assertEqual(self.finance.rows[1][:4], ['03-10-2026', 'x', '100.0', 'notes'])
        self.assert_delayed_note(
            self.finance.notes['B2'], '2026-10-03 22:00:00')
        self.assertEqual(self.finance.notes['D2'], self.finance.notes['B2'])
        self.assertIn('2026-10-03 22:00:00', self.send.call_args.args[1])

    def test_all_health_commands_and_fractional_hours(self):
        commands = [('we 70.5', 2, '70.5'),
                    ('ex 30 pushups', 3, '30 pushups'),
                    ('j', 4, 'x'), ('s', 5, 'x'),
                    ('w', 6, 'x'), ('n felt tired', 7, 'felt tired')]
        for offset, (command, column, expected) in enumerate(commands, 1):
            with self.subTest(command=command):
                self.log(command + ' -1.5')
                self.assertEqual(self.health.rows[offset][column - 1], expected)
                self.assert_delayed_note(
                    self.health.notes[f'{chr(64 + column)}{offset + 1}'],
                    '2026-10-04 00:30:00')

    def test_income_pairs_with_historical_spending_and_totals(self):
        self.log('s 10 yesterday -4')
        self.log('s 20 today')
        self.log('a 100 salary -3')
        self.assertEqual(self.finance.rows[2][4:], ['x', '100.0', 'salary'])
        self.assertEqual(self.finance.rows[1][4:], ['', '', ''])
        yesterday = (self.now - timedelta(days=1)).date()
        totals = main.get_total_data(yesterday, yesterday)
        self.assertEqual((totals[0], totals[2]), (10, 100))
        totals = main.get_total_data(self.now.date(), self.now.date())
        self.assertEqual((totals[0], totals[2]), (20, 0))

    def test_new_historical_date_and_full_block_insertion_shift_history(self):
        self.log('s 20 today')
        self.log('s 10 yesterday -4')
        self.log('s 5 yesterday again -3')
        self.assertEqual([row[0] for row in self.finance.rows],
                         ['Date', '03-10-2026', '', '', '04-10-2026'])
        self.assertEqual([entry['row_index'] for entry in main._logged_stack], [5, 2, 3])
        self.assertEqual(self.finance.notes['B5'], 'Logged: 2026-10-04 02:00:00')
        main.handle_remove_last()
        self.assertEqual(main._logged_stack[0]['row_index'], 4)
        self.assertEqual(self.finance.rows[3][2], '20.0')

    def test_health_backfill_stays_in_its_day(self):
        self.log('ex 10 pushups')
        self.log('ex 20 pushups -4')
        self.log('we 70 -3')
        self.assertEqual(self.health.rows[1][:3], ['03-10-2026', '', '20 pushups'])
        self.assertEqual(self.health.rows[2][:3], ['', '70.0', ''])
        self.assertEqual(self.health.rows[4][:3], ['04-10-2026', '', '10 pushups'])

    def test_historical_sleep_does_not_overwrite_later_sleep(self):
        self.log('s')
        self.log('s -3')
        self.assert_delayed_note(
            self.health.notes['E2'], '2026-10-03 23:00:00')
        self.assertEqual(self.health.notes['E4'], 'Logged: 2026-10-04 02:00:00')
        self.assertEqual(main.get_last_sleep_wake_event(self.health)[1], self.now)

    def test_duplicate_uses_effective_time(self):
        self.log('s -10')
        self.log('s -8')
        self.assert_delayed_note(
            self.health.notes['E2'], '2026-10-03 16:00:00')
        self.assert_delayed_note(
            self.health.notes['E3'], '2026-10-03 18:00:00')
        self.assertEqual(len(self.health.rows), 3)
        self.assertIn('Warning: Missing Wake Up', self.send.call_args.args[1])

    def test_duplicate_crossing_midnight_moves_date(self):
        self.log('s -3')
        self.log('s -1')
        self.assertEqual(self.health.rows[1][4], 'x')
        self.assertEqual(self.health.rows[3][0], '04-10-2026')
        self.assert_delayed_note(
            self.health.notes['E4'], '2026-10-04 01:00:00')

    def test_latest_event_uses_timestamp_when_rows_are_out_of_order(self):
        self.log('s')
        self.log('s -1')
        self.assertEqual(main.get_last_sleep_wake_event(self.health)[1], self.now)

    def test_suffix_only_at_end_and_only_logging_commands(self):
        self.log('n indoor -4 degrees')
        self.assertEqual(self.health.rows[1][6], 'indoor -4 degrees')
        self.assertEqual(main._parse_logging_delay('total m -4'), ('total m -4', None))

    def test_zero_delay_and_year_boundary(self):
        self.log('a 2 -0')
        self.assert_delayed_note(
            self.finance.notes['E2'], '2026-10-04 02:00:00')
        text, occurred_at = main._parse_logging_delay('j -7000')
        self.assertEqual(text, 'j')
        self.assertEqual(occurred_at, self.now - timedelta(hours=7000))

    def test_overflow_is_rejected_without_writing(self):
        self.log('s 100 -' + '9' * 400)
        self.assertEqual(len(self.finance.rows), 1)
        self.assertIn('Delay is too large', self.send.call_args.args[1])

    def test_invalid_delay_is_rejected_without_writing(self):
        for suffix in ('-1.2.3', '-nan', '-inf', '-4h'):
            with self.subTest(suffix=suffix):
                self.log('j ' + suffix)
                self.assertEqual(len(self.health.rows), 1)
                self.assertIn('Invalid delay', self.send.call_args.args[1])

    def test_remove_undo_preserves_delayed_notes(self):
        for command, sheet, address in [('s 100 notes -4', self.finance, 'B2'),
                                        ('ex 10 pushups -4', self.health, 'C2')]:
            with self.subTest(command=command):
                self.log(command)
                main.handle_remove_last()
                main.handle_undo()
                self.assert_delayed_note(
                    sheet.notes[address], '2026-10-03 22:00:00')

    def test_clear_and_undo_paired_finance_preserves_timestamps(self):
        self.log('s 100')
        self.log('a 200')
        main.handle_remove_last()
        self.assertEqual(self.finance.notes['E2'], '')
        main.handle_undo()
        self.assertEqual(self.finance.notes['B2'], 'Logged: 2026-10-04 02:00:00')
        self.assertEqual(self.finance.notes['E2'], 'Logged: 2026-10-04 02:00:00')

    def test_full_word_finance_commands(self):
        self.log('spend 100 lunch -4')
        self.log('income 500 salary -3')
        self.assertEqual(self.finance.rows[1][1:],
                         ['x', '100.0', 'lunch', '', '', ''])
        self.assertEqual(self.finance.rows[2][1:],
                         ['', '', '', 'x', '500.0', 'salary'])
        self.assert_delayed_note(
            self.finance.notes['B2'], '2026-10-03 22:00:00')
        self.assert_delayed_note(
            self.finance.notes['E3'], '2026-10-03 23:00:00')

    def test_full_word_health_commands(self):
        commands = [('weight 70.5', 2, '70.5'),
                    ('exercise 30 pushups', 3, '30 pushups'),
                    ('jerk', 4, 'x'),
                    ('note felt good', 7, 'felt good')]
        for offset, (command, column, expected) in enumerate(commands, 1):
            with self.subTest(command=command):
                self.log(command + ' -1')
                self.assertEqual(self.health.rows[offset][column - 1], expected)
                self.assert_delayed_note(
                    self.health.notes[f'{chr(64 + column)}{offset + 1}'],
                    '2026-10-04 01:00:00')

    def test_sleep_and_wake_full_words_accept_spaced_delay(self):
        self.log('sleep - 10')
        self.log('wake up - 2')
        self.assert_delayed_note(
            self.health.notes['E2'], '2026-10-03 16:00:00')
        self.assert_delayed_note(
            self.health.notes['F4'], '2026-10-04 00:00:00')

    def test_additional_aliases(self):
        self.log('spent 10 food')
        self.log('add 20 refund')
        self.log('workout 10 pullups')
        self.assertEqual(self.finance.rows[1][2], '10.0')
        self.assertEqual(self.finance.rows[1][5], '20.0')
        self.assertEqual(self.health.rows[1][2], '10 pullups')

    def test_wakeup_spellings(self):
        for command in ('wake', 'wakeup', 'wake-up'):
            with self.subTest(command=command):
                main._logged_stack.clear()
                self.health.rows = [["Date"] + [""] * 6]
                self.health.notes.clear()
                self.log(command + ' -2')
                self.assert_delayed_note(
                    self.health.notes['F2'], '2026-10-04 00:00:00')

    def test_sleep_rejects_unrecognized_arguments(self):
        self.log('sleep sometime')
        self.assertEqual(len(self.health.rows), 1)
        self.assertIn('Invalid format', self.send.call_args.args[1])

    def test_delayed_finance_rows_are_inserted_chronologically(self):
        self.log('spend 30 latest')
        self.log('spend 10 earliest -1')
        self.log('income 20 middle -0.5')

        self.assertEqual([row[0] for row in self.finance.rows],
                         ['Date', '04-10-2026', '', ''])
        self.assertEqual(self.finance.rows[1][2:4], ['10.0', 'earliest'])
        self.assertEqual(self.finance.rows[2][5:7], ['20.0', 'middle'])
        self.assertEqual(self.finance.rows[3][2:4], ['30.0', 'latest'])
        self.assert_delayed_note(
            self.finance.notes['B2'], '2026-10-04 01:00:00')
        self.assert_delayed_note(
            self.finance.notes['E3'], '2026-10-04 01:30:00')
        self.assertEqual(self.finance.notes['B4'], 'Logged: 2026-10-04 02:00:00')

    def test_delayed_health_rows_are_inserted_chronologically(self):
        self.log('exercise latest')
        self.log('weight 70 -1')
        self.log('note middle -0.5')

        self.assertEqual([row[0] for row in self.health.rows],
                         ['Date', '04-10-2026', '', ''])
        self.assertEqual(self.health.rows[1][1], '70.0')
        self.assertEqual(self.health.rows[2][6], 'middle')
        self.assertEqual(self.health.rows[3][2], 'latest')
        self.assert_delayed_note(
            self.health.notes['B2'], '2026-10-04 01:00:00')
        self.assert_delayed_note(
            self.health.notes['G3'], '2026-10-04 01:30:00')
        self.assertEqual(self.health.notes['C4'], 'Logged: 2026-10-04 02:00:00')

    def test_delayed_log_always_gets_a_new_row(self):
        self.log('spend 10 first -1')
        self.log('income 20 second -1')
        self.log('weight 70 -1')
        self.log('exercise run -1')

        self.assertEqual(len(self.finance.rows), 3)
        self.assertEqual(len(self.health.rows), 3)
        self.assertFalse(self.finance.rows[1][4])
        self.assertFalse(self.finance.rows[2][1])
        self.assertFalse(self.health.rows[1][2])
        self.assertFalse(self.health.rows[2][1])

    def test_later_normal_logs_do_not_reuse_delayed_rows(self):
        self.log('spend 10 delayed -1')
        self.log('income 20 current')
        self.log('weight 70 -1')
        self.log('exercise current')

        self.assertEqual(len(self.finance.rows), 3)
        self.assertEqual(self.finance.rows[1][4:], ['', '', ''])
        self.assertEqual(self.finance.rows[2][4:], ['x', '20.0', 'current'])
        self.assertEqual(len(self.health.rows), 3)
        self.assertEqual(self.health.rows[1][2], '')
        self.assertEqual(self.health.rows[2][2], 'current')

    def test_delayed_row_is_a_boundary_for_health_backfill(self):
        self.clock.now.return_value = self.now - timedelta(hours=1)
        self.log('exercise old')
        self.clock.now.return_value = self.now
        self.log('weight 70 -0.5')
        self.log('note current')

        self.assertEqual(len(self.health.rows), 4)
        self.assertEqual(self.health.rows[1][2], 'old')
        self.assertEqual(self.health.rows[2][1], '70.0')
        self.assertEqual(self.health.rows[3][6], 'current')


if __name__ == '__main__':
    unittest.main()
