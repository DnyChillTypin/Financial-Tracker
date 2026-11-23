import hmac
import os
import re
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, request
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# Vietnam timezone (UTC+7)
VIETNAM_TZ = timezone(timedelta(hours=7))

load_dotenv()

app = Flask(__name__)

# ──────────────────────────────────────────────
# MESSAGE DEDUPLICATION
# Prevents duplicate processing when Render cold-starts
# and Facebook retries the webhook before getting a 200.
# ──────────────────────────────────────────────
_processed_messages = {}  # mid -> timestamp
MESSAGE_TTL = 300  # keep message IDs for 5 minutes

def _is_duplicate(mid):
    """Return True if this message was already processed. Also cleans stale entries."""
    now = time.time()
    # Prune old entries
    stale = [k for k, v in _processed_messages.items() if now - v > MESSAGE_TTL]
    for k in stale:
        del _processed_messages[k]
    # Check duplicate
    if mid in _processed_messages:
        return True
    _processed_messages[mid] = now
    return False

PAGE_ACCESS_TOKEN = os.getenv('PAGE_ACCESS_TOKEN')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE')
SPREADSHEET_NAME = os.getenv('SPREADSHEET_NAME')
# Secret required by the /cron/tasks endpoint (called by an external scheduler).
CRON_SECRET = os.getenv('CRON_SECRET')
# Messenger PSID that receives the scheduled task list. Send `myid` to the bot to get it.
REMINDER_RECIPIENT_ID = os.getenv('REMINDER_RECIPIENT_ID')

# ──────────────────────────────────────────────
# SHEET LAYOUTS
# Every tracked value has its timestamp in a plain cell (no cell notes).
# Timestamps are "YYYY-MM-DD HH:MM:SS", with " (delayed)" appended for -x logs.
# ──────────────────────────────────────────────

# Finance: [Date] [time spent] [amount spent] [note spent] [time added] [amount added] [note added]
FINANCE_WIDTH = 7
FINANCE_SPENT_COLS = (2, 3, 4)
FINANCE_ADDED_COLS = (5, 6, 7)
FINANCE_TIME_COLS = (2, 5)

# Health: [Date] [Weight] [Weight Time] [Exercises] [Exercise Time] [Jerk] [Sleep] [Wake Up] [Notes] [Note Time]
HEALTH_HEADER = ["Date", "Weight", "Weight Time", "Exercises", "Exercise Time",
                 "Jerk", "Sleep", "Wake Up", "Notes", "Note Time"]
HEALTH_WIDTH = len(HEALTH_HEADER)
# field -> (value col, time col). Jerk/Sleep/Wake Up are pure events: the
# timestamp itself is the cell's content, so they have no separate time col.
HEALTH_FIELDS = {
    "weight": (2, 3),
    "exercise": (4, 5),
    "jerk": (6, None),
    "sleep": (7, None),
    "wake": (8, None),
    "note": (9, 10),
}
HEALTH_EXERCISE_COL = 4
HEALTH_JERK_COL = 6
HEALTH_SLEEP_COL = 7
HEALTH_WAKE_COL = 8
HEALTH_TIME_COLS = (3, 5, 6, 7, 8, 10)

# Tasks: [Task] [Priority] [Added] [Finished]
TASKS_HEADER = ["Task", "Priority", "Added", "Finished"]
TASKS_WIDTH = len(TASKS_HEADER)

SHEET_WIDTHS = {"finance": FINANCE_WIDTH, "health": HEALTH_WIDTH, "tasks": TASKS_WIDTH}
TIME_COLS = {"finance": FINANCE_TIME_COLS, "health": HEALTH_TIME_COLS}

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DELAYED_MARK = " (delayed)"

# ──────────────────────────────────────────────
# GOOGLE SHEETS SETUP
# ──────────────────────────────────────────────

# Sheets already checked for the old cell-note layout in this process.
_migration_checked = set()

def get_spreadsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME)

def get_finance_sheet():
    sheet = get_spreadsheet().sheet1
    if "finance" not in _migration_checked:
        migrate_finance_sheet(sheet)
        _migration_checked.add("finance")
    return sheet

def get_health_sheet():
    spreadsheet = get_spreadsheet()
    try:
        sheet = spreadsheet.worksheet("health")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="health", rows=1000, cols=HEALTH_WIDTH)
        sheet.update(f'A1:{_col(HEALTH_WIDTH)}1', [HEALTH_HEADER])
        _migration_checked.add("health")
        return sheet
    if "health" not in _migration_checked:
        migrate_health_sheet(sheet)
        _migration_checked.add("health")
    return sheet

def get_tasks_sheet():
    spreadsheet = get_spreadsheet()
    try:
        return spreadsheet.worksheet("tasks")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="tasks", rows=1000, cols=TASKS_WIDTH)
        sheet.update(f'A1:{_col(TASKS_WIDTH)}1', [TASKS_HEADER])
        return sheet

def _get_sheet(sheet_name):
    return {"finance": get_finance_sheet, "health": get_health_sheet,
            "tasks": get_tasks_sheet}[sheet_name]()

# ──────────────────────────────────────────────
# ONE-TIME MIGRATION: cell notes -> timestamp cells
# Runs automatically the first time each sheet is opened in a process.
# Idempotent: a migrated sheet is detected and left alone. A backup copy of
# the sheet (notes included) is made before anything is rewritten.
# ──────────────────────────────────────────────

def _fetch_notes(sheet):
    """Return {(row, col): note} for every cell note in the sheet (1-based)."""
    meta = sheet.spreadsheet.fetch_sheet_metadata(params={
        "ranges": f"'{sheet.title}'",
        "includeGridData": "true",
        "fields": "sheets(data(startRow,startColumn,rowData(values(note))))",
    })
    notes = {}
    for data in (meta.get("sheets") or [{}])[0].get("data", []):
        start_row, start_col = data.get("startRow", 0), data.get("startColumn", 0)
        for r, row_data in enumerate(data.get("rowData", [])):
            for c, cell in enumerate(row_data.get("values", [])):
                if cell.get("note"):
                    notes[(start_row + r + 1, start_col + c + 1)] = cell["note"]
    return notes

def _note_to_time_cell(note, fallback_date):
    """Convert an old 'Logged: ...' note into a timestamp cell value."""
    occurred_at = _parse_note_timestamp(note)
    if occurred_at:
        return _time_cell(occurred_at, delayed="Delayed: true" in note)
    # No usable note: keep the day at least so the entry is still marked.
    return fallback_date.strftime("%Y-%m-%d") if fallback_date else "?"

def _backup_and_clear_notes(sheet):
    """Copy the sheet (notes included); return a callable that clears its notes."""
    stamp = datetime.now(VIETNAM_TZ).strftime("%Y%m%d-%H%M%S")
    sheet.duplicate(new_sheet_name=f"{sheet.title} backup {stamp}")
    return lambda:sheet.spreadsheet.batch_update({"requests": [{
        "updateCells": {"range": {"sheetId": sheet.id}, "fields": "note"}}]})

def migrate_finance_sheet(sheet):
    """Replace the 'x' in time spent / time added with the timestamp from its note."""
    values = sheet.get_all_values()
    if not any(len(row) >= col and row[col - 1] == "x"
               for row in values[1:] for col in FINANCE_TIME_COLS):
        return False
    clear_notes = _backup_and_clear_notes(sheet)
    notes = _fetch_notes(sheet)
    current_date = None
    columns = {col: [] for col in FINANCE_TIME_COLS}
    for index, row in enumerate(values, 1):
        row = row + [""] * (FINANCE_WIDTH - len(row))
        if index > 1 and row[0]:
            current_date = _parse_date_str(row[0]) or current_date
        for col in FINANCE_TIME_COLS:
            cell = row[col - 1]
            if index > 1 and cell == "x":
                cell = _note_to_time_cell(notes.get((index, col), ""), current_date)
            columns[col].append([cell])
    sheet.batch_update([
        {"range": f"{_col(col)}1:{_col(col)}{len(values)}", "values": cells}
        for col, cells in columns.items()])
    clear_notes()
    return True

def migrate_health_sheet(sheet):
    """Rewrite the old 7-column health layout into the timestamp-column layout."""
    values = sheet.get_all_values()
    if not values:
        sheet.update(f'A1:{_col(HEALTH_WIDTH)}1', [HEALTH_HEADER])
        return True
    # Old header: C = Exercises, D = Jerk. New header: C = Weight Time, D = Exercises.
    header = [h.strip().lower() for h in values[0] + [""] * 4]
    if header[2] != "exercises" and header[3] != "jerk":
        return False
    clear_notes = _backup_and_clear_notes(sheet)
    notes = _fetch_notes(sheet)
    rows = [HEALTH_HEADER]
    current_date = None
    for index, row in enumerate(values[1:], 2):
        date, weight, exercise, jerk, sleep, wake, note = (row + [""] * 7)[:7]
        if date:
            current_date = _parse_date_str(date) or current_date

        def stamp(old_col, value):
            if not value:
                return ""
            return _note_to_time_cell(notes.get((index, old_col), ""), current_date)

        rows.append([date, weight, stamp(2, weight), exercise, stamp(3, exercise),
                     stamp(4, jerk), stamp(5, sleep), stamp(6, wake),
                     note, stamp(7, note)])
    if sheet.col_count < HEALTH_WIDTH:
        sheet.add_cols(HEALTH_WIDTH - sheet.col_count)
    sheet.update(f'A1:{_col(HEALTH_WIDTH)}{len(rows)}', rows)
    clear_notes()
    return True

# ──────────────────────────────────────────────
# SHARED HELPERS
# ──────────────────────────────────────────────

# Stack of logged entries so rm/remove can walk backwards through history.
# Each entry: {"sheet": "finance"|"health", "row_index": int, "cols": [int]}
# Set by handle_finance_spent, handle_finance_added, handle_health_entry.
_logged_stack = []

def get_today_date_str():
    return datetime.now(VIETNAM_TZ).strftime("%d-%m-%Y")

def _col(col_index):
    """1-based column index -> letter (A-Z)."""
    return chr(64 + col_index)

def _set_cell(sheet, row_index, col_index, value):
    """Write one cell RAW (update_cell is USER_ENTERED, which turns timestamps into dates)."""
    sheet.update(f'{_col(col_index)}{row_index}', [[value]])

def _time_cell(occurred_at, delayed=False):
    """Format a timestamp cell value."""
    return occurred_at.strftime(TIMESTAMP_FORMAT) + (DELAYED_MARK if delayed else "")

def _parse_time_cell(value):
    """Parse a timestamp cell value into an aware datetime, or None."""
    try:
        dt = datetime.strptime(str(value).strip()[:19], TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return dt.replace(tzinfo=VIETNAM_TZ)

def _insert_entry_rows(sheet, sheet_name, row_index, rows):
    """Insert explicitly positioned rows and keep removal history aligned."""
    width = len(rows[0])
    sheet.insert_rows([[""] * width for _ in rows], row=row_index)
    sheet.update(f'A{row_index}:{_col(width)}{row_index + len(rows) - 1}', rows)
    for entry in _logged_stack:
        if entry["sheet"] == sheet_name and entry["row_index"] >= row_index:
            entry["row_index"] += len(rows)
    if (_last_removed and _last_removed["sheet"] == sheet_name
            and _last_removed["row_index"] >= row_index):
        _last_removed["row_index"] += len(rows)

def _row_occurred_at(sheet_name, row):
    """Return the earliest timestamp stored in a row's time cells."""
    timestamps = [_parse_time_cell(row[col - 1])
                  for col in TIME_COLS[sheet_name] if len(row) >= col]
    timestamps = [t for t in timestamps if t]
    return min(timestamps) if timestamps else None

def _row_is_delayed(sheet_name, row):
    """Return whether a row was reserved for a delayed log."""
    return any(len(row) >= col and row[col - 1].endswith(DELAYED_MARK)
               for col in TIME_COLS[sheet_name])

def _entry_row(sheet, sheet_name, occurred_at, cols, scan_all=False,
               force_new_row=False):
    """Find or create the correct row in the event's chronological date block."""
    values = sheet.get_all_values()
    width = SHEET_WIDTHS[sheet_name]
    blank = [""] * (width - 1)
    target_date = occurred_at.date()
    date_str = occurred_at.strftime("%d-%m-%Y")
    current_date = None
    matching = []
    later_row = None
    for index, row in enumerate(values[1:], 2):
        if row and row[0]:
            current_date = _parse_date_str(row[0])
            if current_date and current_date > target_date and later_row is None:
                later_row = index
        if current_date == target_date and any(row):
            matching.append(index)

    if force_new_row and matching:
        insert_at = matching[-1] + 1
        for index in matching:
            row_timestamp = _row_occurred_at(sheet_name, values[index - 1])
            if row_timestamp and occurred_at < row_timestamp:
                insert_at = index
                break

        is_first_row = insert_at == matching[0]
        new_row = [date_str if is_first_row else ""] + blank
        _insert_entry_rows(sheet, sheet_name, insert_at, [new_row])
        if is_first_row:
            # Col A belongs only to the first row of a date block.
            sheet.update_cell(insert_at + 1, 1, "")
        return insert_at

    if not force_new_row:
        candidates = reversed(matching) if scan_all else matching[-1:]
        for index in candidates:
            row = values[index - 1]
            if _row_is_delayed(sheet_name, row):
                # A delayed row is a chronological boundary. Do not place a
                # newer normal event into any row that appears before it.
                break
            if all(len(row) < col or not row[col - 1] for col in cols):
                return index

    if matching:
        index = matching[-1] + 1
        rows = [[""] + blank]
    elif later_row is not None:
        index = later_row
        rows = [[date_str] + blank, [""] + blank]
    else:
        index = len(values) + 1
        # Keep the separator even though Sheets omits trailing empty rows.
        if len(values) > 1:
            index += 1
        sheet.update(f'A{index}:{_col(width)}{index}', [[date_str] + blank])
        return index

    if index <= len(values):
        _insert_entry_rows(sheet, sheet_name, index, rows)
    else:
        sheet.update(f'A{index}:{_col(width)}{index}', rows)
    return index

def _write_finance_entry(amount, note, occurred_at, cols, force_new_row=False):
    sheet = get_finance_sheet()
    occurred_at = occurred_at or datetime.now(VIETNAM_TZ)
    cols = list(cols)
    row_index = _entry_row(
        sheet, "finance", occurred_at, cols, force_new_row=force_new_row)
    first, _, last = cols
    # The time column holds the timestamp itself.
    sheet.update(f'{_col(first)}{row_index}:{_col(last)}{row_index}',
                 [[_time_cell(occurred_at, delayed=force_new_row), amount, note]])
    _logged_stack.append({"sheet": "finance", "row_index": row_index, "cols": cols})

# ──────────────────────────────────────────────
# FINANCE HELPERS
# Finance columns:
# [Date] [time spent] [amount spent] [note spent] [time added] [amount added] [note added]
# ──────────────────────────────────────────────

def get_last_finance_date(sheet):
    """Return the last date string in col A, or None."""
    all_values = sheet.get_all_values()
    for row in reversed(all_values):
        if row[0] and row[0] != "Date":
            return row[0]
    return None

def finance_new_day_separator(sheet):
    today_str = get_today_date_str()
    last_date = get_last_finance_date(sheet)
    if last_date and last_date != today_str:
        sheet.append_row([""] * FINANCE_WIDTH)

def get_finance_date_col(sheet):
    """Return today's date if it's the first entry of the day, else empty string."""
    today_str = get_today_date_str()
    last_date = get_last_finance_date(sheet)
    return today_str if last_date != today_str else ""

def get_last_finance_row_index(sheet):
    """Return the 1-based index of the last non-empty row, or 0 if sheet is empty."""
    all_values = sheet.get_all_values()
    for i in range(len(all_values) - 1, -1, -1):
        if any(cell for cell in all_values[i]):
            return i + 1
    return 0

def handle_finance_spent(amount, note, occurred_at=None, force_new_row=False):
    _write_finance_entry(
        amount, note, occurred_at, FINANCE_SPENT_COLS, force_new_row=force_new_row)


def handle_finance_added(amount, note, occurred_at=None, force_new_row=False):
    _write_finance_entry(
        amount, note, occurred_at, FINANCE_ADDED_COLS, force_new_row=force_new_row)

# ──────────────────────────────────────────────
# TOTAL HELPERS
# ──────────────────────────────────────────────

def _parse_date_str(date_str):
    """Parse dd-mm-yyyy string to a date object."""
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").date()
    except ValueError:
        return None


def _date_in_range(date_str, start_date, end_date):
    """Check if a dd-mm-yyyy string falls within [start_date, end_date]."""
    d = _parse_date_str(date_str)
    if d is None:
        return False
    return start_date <= d <= end_date


def get_total_data(start_date, end_date):
    """
    Collect spent entries (finance) and jerk count (health) for a date range.
    Returns (total_spent, spent_entries, jerk_count).
    """
    # -- Finance spent & added --
    finance_sheet = get_finance_sheet()
    fin_values = finance_sheet.get_all_values()
    total_spent = 0.0
    spent_entries = []  # list of (date_str, amount, note)
    total_added = 0.0
    added_entries = []  # list of (date_str, amount, note)
    current_date = None

    for row in fin_values:
        if row[0] and row[0] != "Date":
            current_date = row[0]
        if current_date is None or not _date_in_range(current_date, start_date, end_date):
            continue
        
        # Spent is col 3 (index 2)
        if len(row) > 2 and row[2]:
            try:
                amt = float(row[2])
                total_spent += amt
                note = row[3] if len(row) > 3 and row[3] else ""
                spent_entries.append((current_date, amt, note))
            except ValueError:
                pass
                
        # Added is col 6 (index 5)
        if len(row) > 5 and row[5]:
            try:
                amt = float(row[5])
                total_added += amt
                note = row[6] if len(row) > 6 and row[6] else ""
                added_entries.append((current_date, amt, note))
            except ValueError:
                pass

    # -- Health jerk count --
    try:
        health_sheet = get_health_sheet()
        health_values = health_sheet.get_all_values()
    except Exception:
        health_values = []

    jerk_count = 0
    exercises = {}
    current_date = None
    for row in health_values:
        if row[0] and row[0] != "Date":
            current_date = row[0]
        if current_date is None or not _date_in_range(current_date, start_date, end_date):
            continue

        # Exercise is col D (index 3)
        if len(row) >= HEALTH_EXERCISE_COL and row[HEALTH_EXERCISE_COL - 1]:
            ex_str = row[HEALTH_EXERCISE_COL - 1]
            parts = ex_str.strip().split(maxsplit=1)
            if len(parts) == 2:
                try:
                    count = float(parts[0])
                    if count.is_integer(): count = int(count)
                    name = parts[1].lower().replace("-", " ")
                except ValueError:
                    count = 1
                    name = ex_str.strip().lower().replace("-", " ")
            else:
                count = 1
                name = ex_str.strip().lower().replace("-", " ")
            
            if name in exercises:
                exercises[name] += count
            else:
                exercises[name] = count

        # Jerk is col F (index 5)
        if len(row) >= HEALTH_JERK_COL and row[HEALTH_JERK_COL - 1]:
            jerk_count += 1

    return total_spent, spent_entries, total_added, added_entries, jerk_count, exercises


def parse_total_args(rest):
    """
    Parse the arguments after 'total'.
    Returns (start_date, end_date, label) or (None, None, error_msg).

    Formats:
      (empty)         → today
      d dd/mm         → specific day (current year)
      w dd/mm         → week containing that date
      m dd/mm         → month of that date
      y yyyy          → entire year
      dd/mm/yy        → exact date
    """
    now = datetime.now(VIETNAM_TZ)
    today = now.date()

    if not rest:
        return today, today, "today"

    parts = rest.split(" ", 1)
    mode = parts[0].lower()

    # Direct date: total dd/mm/yy or dd/mm/yyyy
    if "/" in mode or ("-" in mode and mode not in ("d", "w", "m", "y")):
        arg = mode.replace("/", "-")
        segs = arg.split("-")
        try:
            if len(segs) == 2:
                day, month = int(segs[0]), int(segs[1])
                d = datetime(now.year, month, day).date()
                return d, d, d.strftime("%d/%m/%Y")
            elif len(segs) == 3:
                day, month, year = int(segs[0]), int(segs[1]), int(segs[2])
                if year < 100:
                    year += 2000
                d = datetime(year, month, day).date()
                return d, d, d.strftime("%d/%m/%Y")
        except ValueError:
            pass
        return None, None, "❌ Invalid date. Use: dd/mm or dd/mm/yy"

    # Modes: d, w, m, y
    date_arg = parts[1].strip() if len(parts) > 1 else ""

    if mode == "d":
        if not date_arg:
            return today, today, "today"
        arg = date_arg.replace("/", "-")
        segs = arg.split("-")
        try:
            day, month = int(segs[0]), int(segs[1])
            d = datetime(now.year, month, day).date()
            return d, d, d.strftime("%d/%m/%Y")
        except (ValueError, IndexError):
            return None, None, "❌ Invalid date. Use: total d dd/mm"

    elif mode == "w":
        if not date_arg:
            # Current week (Monday–Sunday)
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            return start, end, f"week of {start.strftime('%d/%m')} – {end.strftime('%d/%m')}"
        arg = date_arg.replace("/", "-")
        segs = arg.split("-")
        try:
            day, month = int(segs[0]), int(segs[1])
            d = datetime(now.year, month, day).date()
            start = d - timedelta(days=d.weekday())
            end = start + timedelta(days=6)
            return start, end, f"week of {start.strftime('%d/%m')} – {end.strftime('%d/%m')}"
        except (ValueError, IndexError):
            return None, None, "❌ Invalid date. Use: total w dd/mm"

    elif mode == "m":
        if not date_arg:
            # Current month
            start = today.replace(day=1)
            if today.month == 12:
                end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
            return start, end, today.strftime("%B %Y")
        arg = date_arg.replace("/", "-")
        segs = arg.split("-")
        try:
            month = int(segs[0])
            start = datetime(now.year, month, 1).date()
            if month == 12:
                end = datetime(now.year + 1, 1, 1).date() - timedelta(days=1)
            else:
                end = datetime(now.year, month + 1, 1).date() - timedelta(days=1)
            return start, end, start.strftime("%B %Y")
        except (ValueError, IndexError):
            return None, None, "❌ Invalid month. Use: total m [1-12]"

    elif mode == "y":
        if not date_arg:
            year = now.year
        else:
            try:
                year = int(date_arg)
                if year < 100:
                    year += 2000
            except ValueError:
                return None, None, "❌ Invalid year. Use: total y [yyyy]"
        start = datetime(year, 1, 1).date()
        end = datetime(year, 12, 31).date()
        return start, end, str(year)

    else:
        return None, None, (
            "❌ Unknown mode. Use:\n"
            "  total           → today\n"
            "  total d dd/mm   → specific day\n"
            "  total w dd/mm   → that week\n"
            "  total m [1-12]  → that month\n"
            "  total y [yyyy]  → that year\n"
            "  total dd/mm/yy  → exact date"
        )


def format_total_message(label, total_spent, spent_entries, total_added, added_entries, jerk_count, exercises):
    """Build a summary message showing spent + added + jerk + exercises."""
    lines = [f"📊 Summary — {label}"]
    lines.append("─" * 28)

    if spent_entries:
        lines.append(f"\n💸 Total Spent: {total_spent:.3f}K")
        for date_str, amt, note in spent_entries:
            entry = f"   • {amt:.3f}K"
            if note:
                entry += f" — {note}"
            lines.append(entry)
    else:
        lines.append("\n💸 Total Spent: 0.000K")

    if added_entries:
        lines.append(f"\n💰 Total Added: {total_added:.3f}K")
        for date_str, amt, note in added_entries:
            entry = f"   • {amt:.3f}K"
            if note:
                entry += f" — {note}"
            lines.append(entry)
    else:
        lines.append("\n💰 Total Added: 0.000K")

    if exercises:
        lines.append("\n💪 Exercises:")
        for name, count in exercises.items():
            lines.append(f"   • {count} {name}")

    lines.append(f"\n🫣 Jerk count: {jerk_count}")
    lines.append("─" * 28)

    return "\n".join(lines)

# ──────────────────────────────────────────────
# HEALTH HELPERS
# Health columns:
# [Date] [Weight] [Weight Time] [Exercises] [Exercise Time] [Jerk] [Sleep] [Wake Up] [Notes] [Note Time]
# ──────────────────────────────────────────────

def get_last_health_date(sheet):
    """Return the last date string in col A (skipping header), or None."""
    all_values = sheet.get_all_values()
    for row in reversed(all_values):
        if row[0] and row[0] != "Date":
            return row[0]
    return None

def _next_empty_row(sheet):
    """Return the 1-based index of the first completely empty row at the bottom."""
    all_values = sheet.get_all_values()
    return len(all_values) + 1

def ensure_health_today(sheet):
    """If today's date row doesn't exist yet, add separator + date row."""
    today_str = get_today_date_str()
    last_date = get_last_health_date(sheet)
    if last_date != today_str:
        if last_date is not None:
            # Blank separator row — write explicitly to column A to avoid
            # Google Sheets API table-detection placing it in the wrong column.
            sep_row = _next_empty_row(sheet)
            sheet.update(f'A{sep_row}', [[""]])
        date_row = _next_empty_row(sheet)
        sheet.update(f'A{date_row}:{_col(HEALTH_WIDTH)}{date_row}',
                     [[today_str] + [""] * (HEALTH_WIDTH - 1)])

def handle_health_entry(field, value=None, occurred_at=None, force_new_row=False):
    """
    field: one of HEALTH_FIELDS — weight, exercise, jerk, sleep, wake, note.

    Value fields (weight, exercise, note) write the value plus its timestamp in
    the adjacent time column. Event fields (jerk, sleep, wake) write the
    timestamp itself as the cell content.

    Tries to fill an existing today's row where the target columns are empty.
    Only appends a new row if no such row is available.
    """
    sheet = get_health_sheet()
    occurred_at = occurred_at or datetime.now(VIETNAM_TZ)
    time_value = _time_cell(occurred_at, delayed=force_new_row)
    value_col, time_col = HEALTH_FIELDS[field]
    cols = [value_col] + ([time_col] if time_col else [])

    row_index = _entry_row(
        sheet, "health", occurred_at, cols, scan_all=True,
        force_new_row=force_new_row)
    if time_col:
        sheet.update(f'{_col(value_col)}{row_index}:{_col(time_col)}{row_index}',
                     [[value, time_value]])
    else:
        _set_cell(sheet, row_index, value_col, time_value)
    _logged_stack.append({"sheet": "health", "row_index": row_index, "cols": cols})


def _parse_note_timestamp(note_text):
    """Extract datetime from an old-layout 'Logged: YYYY-MM-DD HH:MM:SS' note (migration only)."""
    if not note_text:
        return None
    first_line = note_text.strip().split("\n")[0]
    cleaned = first_line.replace("Logged:", "").strip()
    try:
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=VIETNAM_TZ)
    except ValueError:
        return None

def _get_row_date(all_values, row_idx):
    """row_idx is 1-based. Finds the date for row_idx by looking backwards in Col A."""
    for i in range(row_idx - 1, 0, -1):
        if i < len(all_values) and all_values[i][0] and all_values[i][0] != "Date":
            d = _parse_date_str(all_values[i][0])
            if d:
                return datetime(d.year, d.month, d.day, tzinfo=VIETNAM_TZ)
    return None

def get_last_sleep_wake_event(sheet, before=None):
    """
    Find the most recent sleep or wake up log in the health sheet.
    Returns (event_type, event_dt, row_index, col_index) or None.
    event_type is 'sleep' (col G) or 'wake' (col H).
    """
    all_values = sheet.get_all_values()
    latest = None
    row_date = None
    for row_index, row in enumerate(all_values[1:], 2):
        if row and row[0]:
            row_date = _get_row_date(all_values, row_index)
        if not row_date or (before and row_date.date() > before.date()):
            continue
        for col, event_type in ((HEALTH_SLEEP_COL, "sleep"), (HEALTH_WAKE_COL, "wake")):
            if len(row) < col or not row[col - 1].strip():
                continue
            event_dt = _parse_time_cell(row[col - 1]) or row_date
            # >= so that, on a tie, the later row wins.
            if (before is None or event_dt <= before) and (latest is None or event_dt >= latest[1]):
                latest = (event_type, event_dt, row_index, col)
    return latest


def handle_sleep_wake_log(action_type, sender_id, occurred_at=None,
                          force_new_row=False):
    """
    Handle logging of sleep or wake up with alternation check and 4-hour duplicate handling.
    action_type: 'sleep' (col G) or 'wake' (col H)
    """
    sheet = get_health_sheet()
    now = occurred_at or datetime.now(VIETNAM_TZ)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    emoji = "😴" if action_type == 'sleep' else "☀️"
    label = "Sleep" if action_type == 'sleep' else "Wake up"
    opposite_label = "Wake Up" if action_type == 'sleep' else "Sleep"

    last_event_info = get_last_sleep_wake_event(sheet, before=now)

    if last_event_info:
        last_type, last_dt, last_row, last_col = last_event_info

        # Check if consecutive same-type log (sleep after sleep, or wake after wake)
        if last_type == action_type:
            diff_hours = None
            if last_dt:
                diff_seconds = (now - last_dt).total_seconds()
                diff_hours = diff_seconds / 3600.0

            # Case 1: Within 4 hours -> overwrite previous timestamp
            if (not force_new_row and diff_hours is not None
                    and 0 <= diff_hours <= 4.0):
                # An updated event crossing midnight belongs in its new date block.
                row_date = _get_row_date(sheet.get_all_values(), last_row)
                if row_date and row_date.date() != now.date():
                    _set_cell(sheet, last_row, last_col, "")
                    _logged_stack[:] = [e for e in _logged_stack if not (
                        e["sheet"] == "health" and e["row_index"] == last_row
                        and last_col in e["cols"])]
                    handle_health_entry(action_type, occurred_at=now)
                    send_message(sender_id, f"{emoji} {label} time updated: {timestamp} "
                                 f"(overwrote previous {last_dt:%d-%m-%Y %H:%M:%S})")
                    return
                _set_cell(sheet, last_row, last_col, _time_cell(now))

                # Format previous time for display
                if last_dt.date() == now.date():
                    prev_str = last_dt.strftime("%H:%M:%S")
                else:
                    prev_str = last_dt.strftime("%d-%m-%Y %H:%M:%S")

                # Ensure stack has this entry for rm/undo
                if not any(e.get("sheet") == "health" and e.get("row_index") == last_row and last_col in e.get("cols", []) for e in _logged_stack):
                    _logged_stack.append({"sheet": "health", "row_index": last_row, "cols": [last_col]})

                send_message(sender_id, f"{emoji} {label} time updated: {timestamp} (overwrote previous {prev_str})")
                return

            # Case 2: After > 4 hours -> log as new entry with warning
            handle_health_entry(
                action_type, occurred_at=now,
                force_new_row=force_new_row)
            send_message(
                sender_id,
                f"⚠️ Warning: Missing {opposite_label} log before this {label}.\n"
                f"{emoji} {label} logged: {timestamp}"
            )
            return

    # Normal alternating log or first-ever log
    handle_health_entry(
        action_type, occurred_at=now,
        force_new_row=force_new_row)
    send_message(sender_id, f"{emoji} {label} logged: {timestamp}")

# ──────────────────────────────────────────────
# REMOVE LAST ENTRY + UNDO
# ──────────────────────────────────────────────

_last_removed = None  # stores data needed to undo the last rm

def handle_remove_last():
    """
    Remove the most recent logged entry by popping from _logged_stack.
    - Health entries: always delete the entire row (removes cell notes too,
      since there is only 1 entry per row on the health sheet).
    - Finance entries: clear only the specific cells; delete the row if it
      becomes empty.
    Saves state into _last_removed so handle_undo() can restore it.
    Can be called repeatedly to keep removing entries backwards.
    """
    global _last_removed

    if not _logged_stack:
        return "❌ Nothing to remove — no recent entry tracked."

    info = _logged_stack.pop()

    sheet = _get_sheet(info["sheet"])
    row_idx = info["row_index"]
    cols = info["cols"]

    # Read current row values for preview and undo backup
    row_data = sheet.row_values(row_idx)
    # Pad row_data to the sheet's full width
    while len(row_data) < SHEET_WIDTHS[info["sheet"]]:
        row_data.append("")

    # Build preview of what's being cleared
    preview_vals = [row_data[c - 1] for c in cols if row_data[c - 1].strip()]
    preview = " | ".join(preview_vals) if preview_vals else "(empty)"

    if info["sheet"] in ("health", "tasks"):
        # Health / tasks: always delete the entire row (values + timestamps)
        sheet.delete_rows(row_idx)
        _last_removed = {
            "sheet": info["sheet"],
            "action": "delete_row",
            "row_index": row_idx,
            "row_data": row_data,
            "cols": cols,
        }
        # Adjust row indices in the stack for entries below the deleted row
        for entry in _logged_stack:
            if entry["sheet"] == info["sheet"] and entry["row_index"] > row_idx:
                entry["row_index"] -= 1
    else:
        # Finance: clear only the specific cells (amount, note and its time cell)
        for col in cols:
            _set_cell(sheet, row_idx, col, "")

        # Check if the row still has any data beyond the date column
        updated_row = sheet.row_values(row_idx)
        has_remaining = any(c.strip() for c in updated_row[1:]) if len(updated_row) > 1 else False

        if not has_remaining:
            # Row is now empty → delete it entirely
            sheet.delete_rows(row_idx)
            _last_removed = {
                "sheet": "finance",
                "action": "delete_row",
                "row_index": row_idx,
                "row_data": row_data,
                "cols": cols,
            }
            for entry in _logged_stack:
                if entry["sheet"] == "finance" and entry["row_index"] > row_idx:
                    entry["row_index"] -= 1
        else:
            _last_removed = {
                "sheet": "finance",
                "action": "clear_cols",
                "row_index": row_idx,
                "cols_cleared": cols,
                "cleared_values": [row_data[c - 1] for c in cols],
            }

    return f"🗑️ Removed: {preview}\nType 'undo' to restore."


def handle_undo():
    """
    Restore the entry that was cleared by the last rm/remove command.
    Can only be used after an rm — not on its own.
    Pushes the restored entry back onto the stack.
    """
    global _last_removed

    if _last_removed is None:
        return "❌ Nothing to undo."

    entry = _last_removed
    _last_removed = None  # one level of undo only

    sheet = _get_sheet(entry["sheet"])

    if entry["action"] == "clear_cols":
        for col, val in zip(entry["cols_cleared"], entry["cleared_values"]):
            _set_cell(sheet, entry["row_index"], col, val)
        # Push the restored entry back onto the stack
        _logged_stack.append({
            "sheet": entry["sheet"],
            "row_index": entry["row_index"],
            "cols": entry["cols_cleared"],
        })
    elif entry["action"] == "delete_row":
        # Re-insert the full row at its original position
        sheet.insert_row(entry["row_data"], entry["row_index"])
        # Adjust row indices in the stack for entries on the same sheet
        for e in _logged_stack:
            if e["sheet"] == entry["sheet"] and e["row_index"] >= entry["row_index"]:
                e["row_index"] += 1
        # Push the restored entry back onto the stack
        _logged_stack.append({
            "sheet": entry["sheet"],
            "row_index": entry["row_index"],
            "cols": entry["cols"],
        })

    return f"↩️ Restored last {entry['sheet']} entry."

# ──────────────────────────────────────────────
# TASK TRACKER
# Tasks columns: [Task] [Priority] [Added] [Finished]
# Finished holds the completion timestamp, or "PASSED <timestamp>" when dropped.
# Open tasks are numbered high → medium → low, then oldest first; the numbers
# shown in the list are the ones `task <n> c` / `task <n> p` refer to.
# ──────────────────────────────────────────────

TASK_PRIORITIES = ("high", "medium", "low")
TASK_PRIORITY_ALIASES = {
    "h": "high", "high": "high",
    "m": "medium", "med": "medium", "medium": "medium",
    "l": "low", "low": "low",
}
TASK_PRIORITY_LABELS = {"high": "🔴 High", "medium": "🟡 Medium", "low": "🟢 Low"}
TASK_DONE_ALIASES = {"c": "c", "done": "c", "complete": "c", "completed": "c",
                     "p": "p", "pass": "p", "passed": "p"}
REMINDER_MIN_INTERVAL = 600  # ignore repeat cron hits within 10 minutes
_last_reminder_at = 0.0

def get_open_tasks(sheet):
    """Return open tasks in display order: [(row_index, task, priority, added)]."""
    tasks = []
    for row_index, row in enumerate(sheet.get_all_values()[1:], 2):
        row = row + [""] * (TASKS_WIDTH - len(row))
        task, priority, added, finished = row[:TASKS_WIDTH]
        if task and not finished:
            priority = TASK_PRIORITY_ALIASES.get(priority.lower(), "medium")
            tasks.append((row_index, task, priority, added))
    tasks.sort(key=lambda t: (TASK_PRIORITIES.index(t[2]), t[3], t[0]))
    return tasks

def format_task_list(tasks, title="📝 Tasks"):
    if not tasks:
        return f"{title}\n\n✅ No open tasks."
    lines = [f"{title} ({len(tasks)} open)"]
    number = 0
    for priority in TASK_PRIORITIES:
        group = [t for t in tasks if t[2] == priority]
        if not group:
            continue
        lines.append("")
        lines.append(TASK_PRIORITY_LABELS[priority])
        for _, task, _, added in group:
            number += 1
            added_dt = _parse_time_cell(added)
            date = f" ({added_dt:%d/%m})" if added_dt else ""
            lines.append(f"{number}. {task}{date}")
    return "\n".join(lines)

def handle_task_add(text, priority):
    sheet = get_tasks_sheet()
    row_index = len(sheet.get_all_values()) + 1
    added = _time_cell(datetime.now(VIETNAM_TZ))
    sheet.update(f'A{row_index}:{_col(TASKS_WIDTH)}{row_index}',
                 [[text, priority, added, ""]])
    _logged_stack.append({"sheet": "tasks", "row_index": row_index, "cols": [1, 2]})
    return sheet

def handle_task_finish(number, action):
    """Mark open task #number completed ('c') or passed ('p'). Returns a reply."""
    sheet = get_tasks_sheet()
    tasks = get_open_tasks(sheet)
    if not 1 <= number <= len(tasks):
        return f"❌ No open task #{number}.\n\n" + format_task_list(tasks)
    row_index, task, _, _ = tasks[number - 1]
    timestamp = _time_cell(datetime.now(VIETNAM_TZ))
    finished = timestamp if action == "c" else f"PASSED {timestamp}"
    _set_cell(sheet, row_index, TASKS_WIDTH, finished)
    verb = "✅ Completed" if action == "c" else "⏭️ Passed"
    remaining = [t for t in tasks if t[0] != row_index]
    return f"{verb}: {task}\n\n" + format_task_list(remaining)

def handle_task_command(rest, sender_id):
    """
    task                      → show open tasks
    task <text> <l/m/h>       → add a task (priority defaults to medium)
    task <n> c / task <n> p   → complete / pass task number n
    """
    if not rest:
        send_message(sender_id, format_task_list(get_open_tasks(get_tasks_sheet())))
        return

    parts = rest.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].lower() in TASK_DONE_ALIASES:
        send_message(sender_id, handle_task_finish(
            int(parts[0]), TASK_DONE_ALIASES[parts[1].lower()]))
        return

    priority = "medium"
    if len(parts) > 1 and parts[-1].lower() in TASK_PRIORITY_ALIASES:
        priority = TASK_PRIORITY_ALIASES[parts[-1].lower()]
        parts = parts[:-1]
    text = " ".join(parts)
    sheet = handle_task_add(text, priority)
    send_message(sender_id, f"➕ Task added ({priority}): {text}\n\n"
                 + format_task_list(get_open_tasks(sheet)))

def send_task_reminder(recipient_id):
    """Send the open task list (used by the 7pm / 10pm schedule)."""
    tasks = get_open_tasks(get_tasks_sheet())
    hour = datetime.now(VIETNAM_TZ).strftime("%H:%M")
    return send_message(recipient_id, format_task_list(tasks, title=f"⏰ {hour} — Tasks"))

# ──────────────────────────────────────────────
# POSTBACK HANDLER (for persistent menu / ice breakers)
# ──────────────────────────────────────────────

def handle_postback(payload, sender_id):
    """Handle postback payloads from persistent menu and ice breakers."""
    timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")

    if payload == "HEALTH_SLEEP":
        handle_sleep_wake_log("sleep", sender_id)
    elif payload == "HEALTH_WAKEUP":
        handle_sleep_wake_log("wake", sender_id)
    elif payload == "HEALTH_JERK":
        handle_health_entry("jerk")
        send_message(sender_id, f"✅ Jerk logged: {timestamp}")
    elif payload == "GET_STARTED":
        send_message(sender_id,
            "👋 Welcome! Here are the commands:\n\n"
            "💰 Finance:\n"
            "  spend [amount] [note] (or s) — log spending\n"
            "  income [amount] [note] (or a/add) — log income\n"
            "  total               — today's summary\n"
            "  total d [dd/mm]     — day summary\n"
            "  total w [dd/mm]     — week summary\n"
            "  total m [1-12]      — month summary\n"
            "  total y [yyyy]      — year summary\n"
            "  total [dd/mm/yy]    — exact date summary\n"
            "  rm / remove         — remove last entry\n"
            "  undo                 — restore removed entry\n\n"
            "🏃 Health:\n"
            "  weight [number] (or we) — weight\n"
            "  exercise [text] (or ex/workout) — exercise\n"
            "  note [text] (or n) — notes\n"
            "  sleep (or s) — log sleep\n"
            "  wake / wake up / wakeup (or w) — log wake up\n"
            "  jerk (or j) — log jerk\n\n"
            "📝 Tasks:\n"
            "  task [text] [l/m/h] — add a task\n"
            "  task                — list open tasks\n"
            "  task [n] c / task [n] p — complete / pass task n\n\n"
            "⏱ Add -hours to any log for an earlier time (Vietnam time).\n"
            "  spend 100 lunch -4 | exercise 30 pushups -1.5 | sleep -8\n\n"
            "💡 Use the ≡ menu for quick sleep/wake/jerk buttons!"
        )
    else:
        send_message(sender_id, "❓ Unknown action.")

# ──────────────────────────────────────────────
# MESSAGE PARSING
# ──────────────────────────────────────────────

LOGGING_COMMAND_ALIASES = {
    "add": "a",
    "income": "a",
    "spend": "spend",
    "spent": "spend",
    "weight": "we",
    "exercise": "ex",
    "workout": "ex",
    "note": "n",
    "sleep": "sleep",
    "wake": "w",
    "wakeup": "w",
    "wake-up": "w",
    "jerk": "j",
}

def _parse_logging_delay(message_text):
    """Remove a trailing -hours suffix from logging commands only."""
    text = message_text.strip()
    parts = text.split(maxsplit=1)
    keyword = parts[0].lower() if parts else ""
    logging_keywords = {"a", "s", "we", "ex", "n", "j", "w", "😴", "☀️"}
    if keyword not in logging_keywords and keyword not in LOGGING_COMMAND_ALIASES:
        return text, None
    match = re.search(r"\s+-\s*([0-9]+(?:\.[0-9]+)?|\.[0-9]+)$", text)
    if not match:
        if re.search(r"\s+-(?:[0-9.]\S*|inf|infinity|nan)$", text, re.IGNORECASE):
            raise ValueError("❌ Invalid delay. Use hours, e.g. -4 or -1.5.")
        return text, None
    try:
        occurred_at = datetime.now(VIETNAM_TZ) - timedelta(hours=float(match[1]))
    except (ValueError, OverflowError):
        raise ValueError("❌ Delay is too large. Use hours, e.g. -4 or -1.5.") from None
    return text[:match.start()].rstrip(), occurred_at

def parse_and_handle(message_text, sender_id):
    try:
        message_text, occurred_at = _parse_logging_delay(message_text)
    except ValueError as exc:
        send_message(sender_id, str(exc))
        return
    force_new_row = occurred_at is not None
    delayed_label = (f" (at {occurred_at:%Y-%m-%d %H:%M:%S})" if occurred_at else "")
    # Failsafe for mobile app bugs where quick reply/ice breaker is sent as plain text
    text_lower = message_text.strip().lower()
    if text_lower == "😴 sleep":
        handle_sleep_wake_log(
            "sleep", sender_id, occurred_at=occurred_at,
            force_new_row=force_new_row)
        return
    elif text_lower == "☀️ wake up":
        handle_sleep_wake_log(
            "wake", sender_id, occurred_at=occurred_at,
            force_new_row=force_new_row)
        return

    parts = message_text.strip().split(" ", 1)
    keyword = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    keyword = LOGGING_COMMAND_ALIASES.get(keyword, keyword)

    # Accept the conversational two-word command "wake up".
    if keyword == "w" and rest.lower() == "up":
        rest = ""

    # ── FINANCE: a [amount] [note] ──
    if keyword == "a":
        sub = rest.split(" ", 1)
        try:
            amount = float(sub[0])
            note = sub[1].strip() if len(sub) > 1 else ""
            handle_finance_added(
                amount, note, occurred_at=occurred_at,
                force_new_row=force_new_row)
            send_message(sender_id, f"✅ Added: {amount:.3f}K" + (f" — {note}" if note else "") + delayed_label)
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: a [amount] [optional note]\nExample: a 500 salary")

    # ── FINANCE: s [amount] [note]  OR  HEALTH: s (sleep timestamp) ──
    elif keyword in ("s", "spend", "sleep"):
        is_sleep_command = keyword in ("s", "sleep") and not rest
        if is_sleep_command:
            handle_sleep_wake_log(
                "sleep", sender_id, occurred_at=occurred_at,
                force_new_row=force_new_row)
            return
        if rest:
            sub = rest.split(" ", 1)
            try:
                amount = float(sub[0])
                note = sub[1].strip() if len(sub) > 1 else ""
                handle_finance_spent(
                    amount, note, occurred_at=occurred_at,
                    force_new_row=force_new_row)
                send_message(sender_id, f"✅ Spent: {amount:.3f}K" + (f" — {note}" if note else "") + delayed_label)
            except ValueError:
                send_message(sender_id,
                    "❌ Invalid format.\n"
                    "For spending: s [amount] [optional note] — e.g. s 15 lunch\n"
                    "For sleep: send 's' with nothing after it"
                )
        else:
            send_message(sender_id, "❌ Invalid format. Use: spend [amount] [optional note]\nExample: spend 15 lunch")

    # ── TOTAL: total [d/w/m/y date] or total [dd/mm/yy] ──
    elif keyword == "total":
        start_date, end_date, label = parse_total_args(rest)
        if start_date is None:
            send_message(sender_id, label)  # label contains error msg
        else:
            total_spent, spent_entries, total_added, added_entries, jerk_count, exercises = get_total_data(start_date, end_date)
            msg = format_total_message(label, total_spent, spent_entries, total_added, added_entries, jerk_count, exercises)
            send_message(sender_id, msg)

    # ── HEALTH: we [float] ──
    elif keyword == "we":
        try:
            weight = float(rest)
            handle_health_entry(
                "weight", weight, occurred_at=occurred_at,
                force_new_row=force_new_row)
            send_message(sender_id, f"✅ Weight logged: {weight} kg" + delayed_label)
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: we [number]\nExample: we 70.5")

    # ── HEALTH: ex [string] ──
    elif keyword == "ex":
        if rest:
            handle_health_entry(
                "exercise", rest, occurred_at=occurred_at,
                force_new_row=force_new_row)
            send_message(sender_id, f"✅ Exercise logged: {rest}" + delayed_label)
        else:
            send_message(sender_id, "❌ Invalid format. Use: ex [description]\nExample: ex 30 min run")

    # ── HEALTH: j (jerk timestamp) ──
    elif keyword == "j":
        timestamp = (occurred_at or datetime.now(VIETNAM_TZ)).strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(
            "jerk", occurred_at=occurred_at,
            force_new_row=force_new_row)
        send_message(sender_id, f"✅ Jerk logged: {timestamp}")

    # ── HEALTH: w (wake up timestamp) ──
    elif keyword == "w":
        if not rest:
            handle_sleep_wake_log(
                "wake", sender_id, occurred_at=occurred_at,
                force_new_row=force_new_row)
        else:
            send_message(sender_id, "❌ Invalid format. Use: w (with nothing after it)\nExample: w")

    # ── HEALTH: n [string] ──
    elif keyword == "n":
        if rest:
            handle_health_entry(
                "note", rest, occurred_at=occurred_at,
                force_new_row=force_new_row)
            send_message(sender_id, f"✅ Note logged: {rest}" + delayed_label)
        else:
            send_message(sender_id, "❌ Invalid format. Use: n [note]\nExample: n felt tired today")

    # ── TASKS: task [text] [l/m/h] | task | task [n] c/p ──
    elif keyword in ("task", "tasks", "todo"):
        handle_task_command(rest, sender_id)

    # ── MYID: show the sender's Messenger ID (set it as REMINDER_RECIPIENT_ID) ──
    elif keyword == "myid":
        send_message(sender_id, f"🆔 Your Messenger ID: {sender_id}\n"
                     "Set it as REMINDER_RECIPIENT_ID to get the 7pm / 10pm task list.")

    # ── REMOVE: rm / remove — delete last entry ──
    elif keyword in ("rm", "remove"):
        try:
            result = handle_remove_last()
            send_message(sender_id, result)
        except Exception as e:
            send_message(sender_id, f"❌ Error removing entry: {str(e)}")

    # ── UNDO: restore last removed entry ──
    elif keyword == "undo":
        try:
            result = handle_undo()
            send_message(sender_id, result)
        except Exception as e:
            send_message(sender_id, f"❌ Error restoring entry: {str(e)}")

    # ── LINK: get spreadsheet link ──
    elif keyword == "link":
        try:
            spreadsheet = get_spreadsheet()
            url = spreadsheet.url
            send_message(sender_id, f"🔗 Here is your spreadsheet:\n{url}")
        except Exception as e:
            send_message(sender_id, f"❌ Error getting link: {str(e)}")

    # ── SETUP: configure persistent menu ──
    elif keyword == "setup":
        status, result = _setup_messenger_profile()
        send_message(sender_id, f"⚙️ Profile setup ({status})\n\n⚠️ IMPORTANT: You MUST delete this conversation in Messenger and start a new one (or force-close the app) to see the ≡ hamburger menu.")

    # ── MENU: quick replies ──
    elif keyword == "menu":
        quick_replies = [
            {"content_type": "text", "title": "😴 Sleep", "payload": "HEALTH_SLEEP"},
            {"content_type": "text", "title": "☀️ Wake Up", "payload": "HEALTH_WAKEUP"}
        ]
        send_message(sender_id, "👇 Tap to log:", quick_replies=quick_replies)

    # ── UNKNOWN ──
    else:
        send_message(sender_id,
            "❓ Unknown command. Here's what you can use:\n\n"
            "💰 Finance:\n"
            "  spend [amount] [note] (or s) — log spending\n"
            "  income [amount] [note] (or a/add) — log income\n"
            "  total               — today's summary\n"
            "  total d [dd/mm]     — day summary\n"
            "  total w [dd/mm]     — week summary\n"
            "  total m [1-12]      — month summary\n"
            "  total y [yyyy]      — year summary\n"
            "  total [dd/mm/yy]    — exact date summary\n"
            "  rm / remove         — remove last entry\n"
            "  undo                 — restore removed entry\n\n"
            "🏃 Health:\n"
            "  weight [number] (or we) — weight\n"
            "  exercise [text] (or ex/workout) — exercise\n"
            "  note [text] (or n) — notes\n"
            "  sleep (or s) — log sleep\n"
            "  wake / wake up / wakeup (or w) — log wake up\n"
            "  jerk (or j) — log jerk\n\n"
            "📝 Tasks:\n"
            "  task [text] [l/m/h] — add a task\n"
            "  task                — list open tasks\n"
            "  task [n] c / task [n] p — complete / pass task n\n\n"
            "⏱ Add -hours to any log for an earlier time (Vietnam time).\n"
            "  spend 100 lunch -4 | exercise 30 pushups -1.5 | sleep -8\n\n"
            "🔧 Other:\n"
            "  link        — get spreadsheet link\n"
            "  myid        — show your Messenger ID (for reminders)\n"
            "  menu        — show sleep/wake buttons\n"
            "  setup       — configure hamburger menu"
        )

# ──────────────────────────────────────────────
# MESSENGER
# ──────────────────────────────────────────────

def send_message(recipient_id, message_text, quick_replies=None):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    
    if quick_replies is None:
        quick_replies = [
            {"content_type": "text", "title": "😴 Sleep", "payload": "HEALTH_SLEEP"},
            {"content_type": "text", "title": "☀️ Wake Up", "payload": "HEALTH_WAKEUP"}
        ]
        
    # Facebook Messenger max message length is 2000 characters
    max_length = 2000
    if len(message_text) <= max_length:
        messages = [message_text]
    else:
        messages = []
        current_msg = ""
        for line in message_text.split('\n'):
            if len(current_msg) + len(line) + 1 > max_length:
                if current_msg:
                    messages.append(current_msg)
                current_msg = line
            else:
                current_msg = current_msg + ('\n' + line if current_msg else line)
        if current_msg:
            messages.append(current_msg)

    final_status = 200
    for i, msg in enumerate(messages):
        data = {
            "recipient": {"id": recipient_id},
            "message": {
                "text": msg
            }
        }
        # Only attach quick replies to the last message chunk
        if i == len(messages) - 1:
            data["message"]["quick_replies"] = quick_replies
            
        response = requests.post(
            "https://graph.facebook.com/v19.0/me/messages",
            params=params, headers=headers, json=data
        )
        if response.status_code != 200:
            final_status = response.status_code
            print(f"Error sending message chunk: {response.text}")
            
    return final_status

# ──────────────────────────────────────────────
# MESSENGER PROFILE SETUP (ice breakers + persistent menu)
# Call once: GET /setup-profile to configure.
# ──────────────────────────────────────────────

def _setup_messenger_profile():
    """
    One-time setup: configures persistent menu + ice breakers
    so users can tap buttons instead of typing for sleep/wake/jerk.
    """
    url = f"https://graph.facebook.com/v19.0/me/messenger_profile"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}

    payload = {
        # Persistent menu: always visible ≡ button in chat
        "persistent_menu": [
            {
                "locale": "default",
                "composer_input_disabled": False,
                "call_to_actions": [
                    {"type": "postback", "title": "😴 Sleep",    "payload": "HEALTH_SLEEP"},
                    {"type": "postback", "title": "☀️ Wake Up",  "payload": "HEALTH_WAKEUP"},
                ]
            }
        ],
        # Ice breakers: suggested questions shown at start of conversation
        "ice_breakers": [
            {
                "locale": "default",
                "call_to_actions": [
                    {"question": "😴 Log Sleep",        "payload": "HEALTH_SLEEP"},
                    {"question": "☀️ Log Wake Up",      "payload": "HEALTH_WAKEUP"},
                ]
            }
        ],
        # Get Started button (optional, shown first time)
        "get_started": {
            "payload": "GET_STARTED"
        }
    }

    response = requests.post(url, params=params, headers=headers, json=payload)
    return response.status_code, response.json()


@app.route('/setup-profile', methods=['GET'])
def setup_profile():
    """Hit this endpoint once to configure persistent menu + ice breakers."""
    status, result = _setup_messenger_profile()
    return {"status": status, "result": result}, 200

# ──────────────────────────────────────────────
# SCHEDULED TASK REMINDER (7pm & 10pm Vietnam time)
# Render's free tier sleeps, so an in-process timer can't be relied on.
# An external scheduler (e.g. cron-job.org) calls this endpoint at
# 19:00 and 22:00 Asia/Ho_Chi_Minh, which also wakes the service:
#   GET /cron/tasks?key=<CRON_SECRET>
# ──────────────────────────────────────────────

@app.route('/cron/tasks', methods=['GET', 'POST'])
def cron_tasks():
    global _last_reminder_at
    key = request.args.get('key', '')
    if not CRON_SECRET or not hmac.compare_digest(key, CRON_SECRET):
        return 'Forbidden', 403
    if not REMINDER_RECIPIENT_ID:
        return 'REMINDER_RECIPIENT_ID is not set', 500
    # Schedulers retry on cold-start timeouts; send one reminder per slot.
    if time.time() - _last_reminder_at < REMINDER_MIN_INTERVAL:
        return 'ALREADY_SENT', 200
    _last_reminder_at = time.time()
    status = send_task_reminder(REMINDER_RECIPIENT_ID)
    return ('SENT', 200) if status == 200 else (f'Messenger error {status}', 502)

# ──────────────────────────────────────────────
# WEBHOOK
# ──────────────────────────────────────────────

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode and token:
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            return challenge, 200
        else:
            return 'Forbidden', 403
    return 'OK', 200

@app.route('/webhook', methods=['POST'])
def handle_messages():
    data = request.get_json()

    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):

                # ── Handle postback taps (persistent menu / ice breakers) ──
                if messaging_event.get("postback"):
                    sender_id = messaging_event["sender"]["id"]
                    payload = messaging_event["postback"].get("payload", "")
                    try:
                        handle_postback(payload, sender_id)
                    except Exception as e:
                        print(f"Postback error: {e}")
                        send_message(sender_id, f"❌ Unexpected error: {str(e)}")
                    continue

                # ── Handle text messages ──
                if messaging_event.get("message"):

                    if messaging_event["message"].get("is_echo"):
                        continue

                    mid = messaging_event["message"].get("mid")
                    if mid and _is_duplicate(mid):
                        continue

                    sender_id = messaging_event["sender"]["id"]

                    # ── Handle quick replies ──
                    if "quick_reply" in messaging_event["message"]:
                        payload = messaging_event["message"]["quick_reply"]["payload"]
                        try:
                            handle_postback(payload, sender_id)
                        except Exception as e:
                            print(f"Quick reply error: {e}")
                            send_message(sender_id, f"❌ Unexpected error: {str(e)}")
                        continue

                    message_text = messaging_event["message"].get("text", "").strip()

                    if not message_text:
                        continue

                    try:
                        parse_and_handle(message_text, sender_id)
                    except Exception as e:
                        print(f"Unhandled error: {e}")
                        send_message(sender_id, f"❌ Unexpected error: {str(e)}")

        return 'EVENT_RECEIVED', 200
    else:
        return 'Not Found', 404

if __name__ == '__main__':
    app.run(port=5000, debug=True)