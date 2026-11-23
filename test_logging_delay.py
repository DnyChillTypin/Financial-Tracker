"""Offline regression tests for delayed logs, timestamp columns, tasks and migration."""
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

for module in ('flask', 'requests', 'gspread', 'gspread.exceptions',
               'oauth2client', 'oauth2client.service_account', 'dotenv'):
    sys.modules.setdefault(module, MagicMock())

import main

# Health columns (0-based list indexes into a row)
WEIGHT, WEIGHT_T, EX, EX_T, JERK, SLEEP, WAKE, NOTE, NOTE_T = range(1, 10)


class Sheet:
    def __init__(self, header=None, width=7, title="Sheet1"):
        self.width = width
        self.title = title
        self.id = 0
        self.rows = [header or (["Date"] + [""] * (width - 1))]
        self.note_map = {}
        self.spreadsheet = MagicMock()
        self.spreadsheet.fetch_sheet_metadata.side_effect = self._metadata

    @property
    def col_count(self):
        return max(len(row) for row in self.rows)

    def add_cols(self, count):
        for row in self.rows:
            row.extend([""] * count)

    def get_all_values(self):
        width = self.col_count
        return [row[:] + [""] * (width - len(row)) for row in self.rows]

    def update(self, address, values):
        match = re.match(r'([A-Z])(\d+)', address)
        col, row = ord(match[1]) - 65, int(match[2]) - 1
        while len(self.rows) < row + len(values):
            self.rows.append([""] * self.width)
        for offset, cells in enumerate(values):
            target = self.rows[row + offset]
            target.extend([""] * (col + len(cells) - len(target)))
            target[col:col + len(cells)] = [str(v) for v in cells]

    def batch_update(self, data):
        for item in data:
            self.update(item["range"], item["values"])

    def update_cell(self, row, col, value):
        self.update(f'{chr(64 + col)}{row}', [[value]])

    def insert_rows(self, values, row):
        self.rows[row - 1:row - 1] = [cells[:] for cells in values]

    def insert_row(self, values, row):
        self.insert_rows([values], row)

    def row_values(self, row):
        return self.rows[row - 1][:]

    def delete_rows(self, row):
        del self.rows[row - 1]

    def duplicate(self, new_sheet_name=None):
        self.backup_name = new_sheet_name

    def _metadata(self, params=None):
        row_data = []
        for r in range(len(self.rows)):
            row_data.append({"values": [
                {"note": self.note_map[(r + 1, c + 1)]} if (r + 1, c + 1) in self.note_map else {}
                for c in range(self.col_count)]})
        return {"sheets": [{"data": [{"rowData": row_data}]}]}


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 4, 2, 0, tzinfo=main.VIETNAM_TZ)
        self.finance = Sheet()
        self.health = Sheet(main.HEALTH_HEADER, main.HEALTH_WIDTH)
        self.tasks = Sheet(main.TASKS_HEADER, main.TASKS_WIDTH)
        main._logged_stack.clear()
        main._last_removed = None
        for target, value in (('get_finance_sheet', self.finance),
                              ('get_health_sheet', self.health),
                              ('get_tasks_sheet', self.tasks)):
            patcher = patch.object(main, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(main, 'send_message', return_value=200)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(main, 'datetime', wraps=datetime)
        clock = patcher.start()
        clock.now.return_value = self.now
        self.clock = clock
        self.addCleanup(patcher.stop)

    def log(self, text):
        main.parse_and_handle(text, 'sender')

    def reply(self):
        return self.send.call_args.args[1]

    def assert_delayed(self, cell, timestamp):
        self.assertEqual(cell, f'{timestamp} (delayed)')


class TestDelayedLogging(BaseCase):
    def test_spending_crosses_midnight_and_excludes_suffix(self):
        self.log('s 100 notes -4')
        self.assertEqual(self.finance.rows[1][:4],
                         ['03-10-2026', '2026-10-03 22:00:00 (delayed)', '100.0', 'notes'])
        self.assertIn('2026-10-03 22:00:00', self.reply())

    def test_all_health_commands_and_fractional_hours(self):
        stamp = '2026-10-04 00:30:00 (delayed)'
        commands = [('we 70.5', WEIGHT, '70.5', WEIGHT_T),
                    ('ex 30 pushups', EX, '30 pushups', EX_T),
                    ('j', JERK, stamp, None), ('s', SLEEP, stamp, None),
                    ('w', WAKE, stamp, None), ('n felt tired', NOTE, 'felt tired', NOTE_T)]
        for offset, (command, column, expected, time_col) in enumerate(commands, 1):
            with self.subTest(command=command):
                self.log(command + ' -1.5')
                row = self.health.rows[offset]
                self.assertEqual(row[column], expected)
                if time_col:
                    self.assertEqual(row[time_col], stamp)

    def test_income_pairs_with_historical_spending_and_totals(self):
        self.log('s 10 yesterday -4')
        self.log('s 20 today')
        self.log('a 100 salary -3')
        self.assertEqual(self.finance.rows[2][4:],
                         ['2026-10-03 23:00:00 (delayed)', '100.0', 'salary'])
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
        self.assertEqual(self.finance.rows[4][1], '2026-10-04 02:00:00')
        main.handle_remove_last()
        self.assertEqual(main._logged_stack[0]['row_index'], 4)
        self.assertEqual(self.finance.rows[3][2], '20.0')

    def test_health_backfill_stays_in_its_day(self):
        self.log('ex 10 pushups')
        self.log('ex 20 pushups -4')
        self.log('we 70 -3')
        self.assertEqual(self.health.rows[1][0], '03-10-2026')
        self.assertEqual(self.health.rows[1][EX], '20 pushups')
        self.assertEqual(self.health.rows[2][WEIGHT], '70.0')
        self.assertEqual(self.health.rows[4][0], '04-10-2026')
        self.assertEqual(self.health.rows[4][EX], '10 pushups')

    def test_totals_read_new_health_columns(self):
        self.log('ex 10 pushups')
        self.log('ex 5 pushups')
        self.log('j')
        totals = main.get_total_data(self.now.date(), self.now.date())
        self.assertEqual(totals[4], 1)
        self.assertEqual(totals[5], {'pushups': 15})

    def test_historical_sleep_does_not_overwrite_later_sleep(self):
        self.log('s')
        self.log('s -3')
        self.assert_delayed(self.health.rows[1][SLEEP], '2026-10-03 23:00:00')
        self.assertEqual(self.health.rows[3][SLEEP], '2026-10-04 02:00:00')
        self.assertEqual(main.get_last_sleep_wake_event(self.health)[1], self.now)

    def test_duplicate_uses_effective_time(self):
        self.log('s -10')
        self.log('s -8')
        self.assert_delayed(self.health.rows[1][SLEEP], '2026-10-03 16:00:00')
        self.assert_delayed(self.health.rows[2][SLEEP], '2026-10-03 18:00:00')
        self.assertEqual(len(self.health.rows), 3)
        self.assertIn('Warning: Missing Wake Up', self.reply())

    def test_duplicate_within_four_hours_overwrites_cell(self):
        self.clock.now.return_value = self.now - timedelta(hours=1)
        self.log('s')
        self.clock.now.return_value = self.now
        self.log('s')
        self.assertEqual(len(self.health.rows), 2)
        self.assertEqual(self.health.rows[1][SLEEP], '2026-10-04 02:00:00')
        self.assertIn('overwrote previous 01:00:00', self.reply())

    def test_duplicate_crossing_midnight_moves_date(self):
        self.log('s -3')
        self.log('s -1')
        self.assertTrue(self.health.rows[1][SLEEP])
        self.assertEqual(self.health.rows[3][0], '04-10-2026')
        self.assert_delayed(self.health.rows[3][SLEEP], '2026-10-04 01:00:00')

    def test_latest_event_uses_timestamp_when_rows_are_out_of_order(self):
        self.log('s')
        self.log('s -1')
        self.assertEqual(main.get_last_sleep_wake_event(self.health)[1], self.now)

    def test_suffix_only_at_end_and_only_logging_commands(self):
        self.log('n indoor -4 degrees')
        self.assertEqual(self.health.rows[1][NOTE], 'indoor -4 degrees')
        self.assertEqual(main._parse_logging_delay('total m -4'), ('total m -4', None))
        self.assertEqual(main._parse_logging_delay('task fix bug -4'), ('task fix bug -4', None))

    def test_zero_delay_and_year_boundary(self):
        self.log('a 2 -0')
        self.assert_delayed(self.finance.rows[1][4], '2026-10-04 02:00:00')
        text, occurred_at = main._parse_logging_delay('j -7000')
        self.assertEqual(text, 'j')
        self.assertEqual(occurred_at, self.now - timedelta(hours=7000))

    def test_overflow_is_rejected_without_writing(self):
        self.log('s 100 -' + '9' * 400)
        self.assertEqual(len(self.finance.rows), 1)
        self.assertIn('Delay is too large', self.reply())

    def test_invalid_delay_is_rejected_without_writing(self):
        for suffix in ('-1.2.3', '-nan', '-inf', '-4h'):
            with self.subTest(suffix=suffix):
                self.log('j ' + suffix)
                self.assertEqual(len(self.health.rows), 1)
                self.assertIn('Invalid delay', self.reply())

    def test_remove_undo_preserves_delayed_timestamps(self):
        for command, sheet, col in [('s 100 notes -4', self.finance, 1),
                                    ('ex 10 pushups -4', self.health, EX_T)]:
            with self.subTest(command=command):
                self.log(command)
                main.handle_remove_last()
                main.handle_undo()
                self.assert_delayed(sheet.rows[1][col], '2026-10-03 22:00:00')

    def test_clear_and_undo_paired_finance_preserves_timestamps(self):
        self.log('s 100')
        self.log('a 200')
        main.handle_remove_last()
        self.assertEqual(self.finance.rows[1][4:], ['', '', ''])
        main.handle_undo()
        self.assertEqual(self.finance.rows[1][1], '2026-10-04 02:00:00')
        self.assertEqual(self.finance.rows[1][4], '2026-10-04 02:00:00')

    def test_full_word_finance_commands(self):
        self.log('spend 100 lunch -4')
        self.log('income 500 salary -3')
        self.assertEqual(self.finance.rows[1][1:],
                         ['2026-10-03 22:00:00 (delayed)', '100.0', 'lunch', '', '', ''])
        self.assertEqual(self.finance.rows[2][1:],
                         ['', '', '', '2026-10-03 23:00:00 (delayed)', '500.0', 'salary'])

    def test_full_word_health_commands(self):
        stamp = '2026-10-04 01:00:00 (delayed)'
        commands = [('weight 70.5', WEIGHT, '70.5', WEIGHT_T),
                    ('exercise 30 pushups', EX, '30 pushups', EX_T),
                    ('jerk', JERK, stamp, None),
                    ('note felt good', NOTE, 'felt good', NOTE_T)]
        for offset, (command, column, expected, time_col) in enumerate(commands, 1):
            with self.subTest(command=command):
                self.log(command + ' -1')
                self.assertEqual(self.health.rows[offset][column], expected)
                if time_col:
                    self.assertEqual(self.health.rows[offset][time_col], stamp)

    def test_sleep_and_wake_full_words_accept_spaced_delay(self):
        self.log('sleep - 10')
        self.log('wake up - 2')
        self.assert_delayed(self.health.rows[1][SLEEP], '2026-10-03 16:00:00')
        self.assert_delayed(self.health.rows[3][WAKE], '2026-10-04 00:00:00')

    def test_additional_aliases(self):
        self.log('spent 10 food')
        self.log('add 20 refund')
        self.log('workout 10 pullups')
        self.assertEqual(self.finance.rows[1][2], '10.0')
        self.assertEqual(self.finance.rows[1][5], '20.0')
        self.assertEqual(self.health.rows[1][EX], '10 pullups')

    def test_wakeup_spellings(self):
        for command in ('wake', 'wakeup', 'wake-up'):
            with self.subTest(command=command):
                main._logged_stack.clear()
                self.health.rows = [main.HEALTH_HEADER[:]]
                self.log(command + ' -2')
                self.assert_delayed(self.health.rows[1][WAKE], '2026-10-04 00:00:00')

    def test_sleep_rejects_unrecognized_arguments(self):
        self.log('sleep sometime')
        self.assertEqual(len(self.health.rows), 1)
        self.assertIn('Invalid format', self.reply())

    def test_delayed_finance_rows_are_inserted_chronologically(self):
        self.log('spend 30 latest')
        self.log('spend 10 earliest -1')
        self.log('income 20 middle -0.5')

        self.assertEqual([row[0] for row in self.finance.rows],
                         ['Date', '04-10-2026', '', ''])
        self.assertEqual(self.finance.rows[1][2:4], ['10.0', 'earliest'])
        self.assertEqual(self.finance.rows[2][5:7], ['20.0', 'middle'])
        self.assertEqual(self.finance.rows[3][2:4], ['30.0', 'latest'])
        self.assert_delayed(self.finance.rows[1][1], '2026-10-04 01:00:00')
        self.assert_delayed(self.finance.rows[2][4], '2026-10-04 01:30:00')
        self.assertEqual(self.finance.rows[3][1], '2026-10-04 02:00:00')

    def test_delayed_health_rows_are_inserted_chronologically(self):
        self.log('exercise latest')
        self.log('weight 70 -1')
        self.log('note middle -0.5')

        self.assertEqual([row[0] for row in self.health.rows],
                         ['Date', '04-10-2026', '', ''])
        self.assertEqual(self.health.rows[1][WEIGHT], '70.0')
        self.assertEqual(self.health.rows[2][NOTE], 'middle')
        self.assertEqual(self.health.rows[3][EX], 'latest')
        self.assert_delayed(self.health.rows[1][WEIGHT_T], '2026-10-04 01:00:00')
        self.assert_delayed(self.health.rows[2][NOTE_T], '2026-10-04 01:30:00')
        self.assertEqual(self.health.rows[3][EX_T], '2026-10-04 02:00:00')

    def test_delayed_log_always_gets_a_new_row(self):
        self.log('spend 10 first -1')
        self.log('income 20 second -1')
        self.log('weight 70 -1')
        self.log('exercise run -1')

        self.assertEqual(len(self.finance.rows), 3)
        self.assertEqual(len(self.health.rows), 3)
        self.assertFalse(self.finance.rows[1][4])
        self.assertFalse(self.finance.rows[2][1])
        self.assertFalse(self.health.rows[1][EX])
        self.assertFalse(self.health.rows[2][WEIGHT])

    def test_later_normal_logs_do_not_reuse_delayed_rows(self):
        self.log('spend 10 delayed -1')
        self.log('income 20 current')
        self.log('weight 70 -1')
        self.log('exercise current')

        self.assertEqual(len(self.finance.rows), 3)
        self.assertEqual(self.finance.rows[1][4:], ['', '', ''])
        self.assertEqual(self.finance.rows[2][4:], ['2026-10-04 02:00:00', '20.0', 'current'])
        self.assertEqual(len(self.health.rows), 3)
        self.assertEqual(self.health.rows[1][EX], '')
        self.assertEqual(self.health.rows[2][EX], 'current')

    def test_delayed_row_is_a_boundary_for_health_backfill(self):
        self.clock.now.return_value = self.now - timedelta(hours=1)
        self.log('exercise old')
        self.clock.now.return_value = self.now
        self.log('weight 70 -0.5')
        self.log('note current')

        self.assertEqual(len(self.health.rows), 4)
        self.assertEqual(self.health.rows[1][EX], 'old')
        self.assertEqual(self.health.rows[2][WEIGHT], '70.0')
        self.assertEqual(self.health.rows[3][NOTE], 'current')


class TestTasks(BaseCase):
    def add_at(self, hours_ago, command):
        self.clock.now.return_value = self.now - timedelta(hours=hours_ago)
        self.log(command)
        self.clock.now.return_value = self.now

    def test_add_stores_task_priority_and_time(self):
        self.log('task buy milk h')
        self.assertEqual(self.tasks.rows[1], ['buy milk', 'high', '2026-10-04 02:00:00', ''])
        self.assertIn('Task added (high): buy milk', self.reply())

    def test_priority_words_and_default(self):
        self.log('task a low')
        self.log('task b medium')
        self.log('task c')
        self.assertEqual([row[1] for row in self.tasks.rows[1:]], ['low', 'medium', 'medium'])

    def test_list_is_grouped_by_priority_then_added_time(self):
        self.add_at(5, 'task old medium m')
        self.add_at(4, 'task low one l')
        self.add_at(3, 'task first high h')
        self.add_at(2, 'task new medium m')
        self.add_at(1, 'task second high h')
        self.log('task')
        self.assertEqual(self.reply(),
                         '📝 Tasks (5 open)\n'
                         '\n🔴 High\n1. first high (03/10)\n2. second high (04/10)\n'
                         '\n🟡 Medium\n3. old medium (03/10)\n4. new medium (04/10)\n'
                         '\n🟢 Low\n5. low one (03/10)')

    def test_complete_and_pass_use_displayed_numbers(self):
        self.add_at(2, 'task medium thing m')
        self.add_at(1, 'task urgent h')
        self.log('task 2 c')    # #2 is the medium task, since high comes first
        self.assertEqual(self.tasks.rows[1][3], '2026-10-04 02:00:00')
        self.assertIn('Completed: medium thing', self.reply())
        self.log('task 1 p')
        self.assertEqual(self.tasks.rows[2][3], 'PASSED 2026-10-04 02:00:00')
        self.assertIn('No open tasks', self.reply())

    def test_finished_tasks_are_hidden_and_renumbered(self):
        self.add_at(3, 'task one h')
        self.add_at(2, 'task two h')
        self.add_at(1, 'task three h')
        self.log('task 1 c')
        self.log('task')
        self.assertIn('1. two', self.reply())
        self.assertIn('2. three', self.reply())
        self.assertNotIn('one', self.reply())

    def test_invalid_number(self):
        self.log('task one h')
        self.log('task 5 c')
        self.assertIn('No open task #5', self.reply())
        self.assertEqual(self.tasks.rows[1][3], '')

    def test_rm_and_undo_task(self):
        self.log('task one h')
        self.assertIn('Removed: one | high', main.handle_remove_last())
        self.assertEqual(len(self.tasks.rows), 1)
        main.handle_undo()
        self.assertEqual(self.tasks.rows[1][0], 'one')

    def test_reminder_sends_list(self):
        self.log('task one h')
        main.send_task_reminder('owner')
        self.assertEqual(self.send.call_args.args[0], 'owner')
        self.assertIn('⏰ 02:00 — Tasks (1 open)', self.reply())
        self.assertIn('1. one', self.reply())


class TestMigration(unittest.TestCase):
    def test_finance_x_becomes_timestamp_and_notes_are_cleared(self):
        sheet = Sheet()
        sheet.rows += [['03-10-2026', 'x', '10', 'food', '', '', ''],
                       ['', '', '', '', 'x', '50', 'gift'],
                       ['', 'x', '5', '', '', '', '']]
        sheet.note_map = {(2, 2): 'Logged: 2026-10-03 08:00:00',
                          (2, 4): 'Logged: 2026-10-03 08:00:00',
                          (3, 5): 'Logged: 2026-10-03 21:00:00\nDelayed: true'}
        self.assertTrue(main.migrate_finance_sheet(sheet))
        self.assertEqual(sheet.rows[1][1], '2026-10-03 08:00:00')
        self.assertEqual(sheet.rows[2][4], '2026-10-03 21:00:00 (delayed)')
        self.assertEqual(sheet.rows[3][1], '2026-10-03')  # no note: date only
        self.assertEqual(sheet.rows[1][2:4], ['10', 'food'])
        self.assertTrue(sheet.backup_name.startswith('Sheet1 backup'))
        sheet.spreadsheet.batch_update.assert_called_once()
        self.assertFalse(main.migrate_finance_sheet(sheet))

    def test_health_is_rewritten_into_timestamp_columns(self):
        sheet = Sheet(["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
                      title="health")
        sheet.rows += [['03-10-2026', '70', '', '', '', 'x', ''],
                       ['', '', '20 pushups', 'x', 'x', '', 'tired']]
        sheet.note_map = {(2, 2): 'Logged: 2026-10-03 07:00:00',
                          (2, 6): 'Logged: 2026-10-03 06:30:00',
                          (3, 3): 'Logged: 2026-10-03 18:00:00',
                          (3, 4): 'Logged: 2026-10-03 22:00:00\nDelayed: true',
                          (3, 5): 'Logged: 2026-10-03 23:30:00',
                          (3, 7): 'Logged: 2026-10-03 23:31:00'}
        self.assertTrue(main.migrate_health_sheet(sheet))
        self.assertEqual(sheet.rows[0], main.HEALTH_HEADER)
        self.assertEqual(sheet.rows[1], ['03-10-2026', '70', '2026-10-03 07:00:00', '', '',
                                         '', '', '2026-10-03 06:30:00', '', ''])
        self.assertEqual(sheet.rows[2], ['', '', '', '20 pushups', '2026-10-03 18:00:00',
                                         '2026-10-03 22:00:00 (delayed)', '2026-10-03 23:30:00',
                                         '', 'tired', '2026-10-03 23:31:00'])
        self.assertFalse(main.migrate_health_sheet(sheet))

    def test_migrated_health_is_readable(self):
        sheet = Sheet(["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"],
                      title="health")
        sheet.rows += [['03-10-2026', '', '', '', 'x', '', '']]
        sheet.note_map = {(2, 5): 'Logged: 2026-10-03 23:30:00'}
        main.migrate_health_sheet(sheet)
        event = main.get_last_sleep_wake_event(sheet)
        self.assertEqual(event[0], 'sleep')
        self.assertEqual(event[1].hour, 23)


if __name__ == '__main__':
    unittest.main()
