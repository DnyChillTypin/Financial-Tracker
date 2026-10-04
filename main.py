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

# ──────────────────────────────────────────────
# GOOGLE SHEETS SETUP
# ──────────────────────────────────────────────

def get_spreadsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME)

def get_finance_sheet():
    return get_spreadsheet().sheet1

def get_health_sheet():
    spreadsheet = get_spreadsheet()
    try:
        return spreadsheet.worksheet("health")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="health", rows=1000, cols=7)
        sheet.append_row(["Date", "Weight", "Exercises", "Jerk", "Sleep", "Wake Up", "Notes"])
        return sheet

# ──────────────────────────────────────────────
# SHARED HELPERS
# ──────────────────────────────────────────────

# Stack of logged entries so rm/remove can walk backwards through history.
# Each entry: {"sheet": "finance"|"health", "row_index": int, "cols": [int]}
# Set by handle_finance_spent, handle_finance_added, handle_health_entry.
_logged_stack = []

def get_today_date_str():
    return datetime.now(VIETNAM_TZ).strftime("%d-%m-%Y")

def _add_note(sheet, row_index, col_index, timestamp):
    """Add a 'Logged: <timestamp>' note to a cell. col_index is 1-based."""
    col_letter = chr(ord('A') + col_index - 1)
    sheet.update_note(f"{col_letter}{row_index}", f"Logged: {timestamp}")

def _insert_entry_rows(sheet, sheet_name, row_index, rows):
    """Insert explicitly positioned rows and keep removal history aligned."""
    sheet.insert_rows([[""] * 7 for _ in rows], row=row_index)
    sheet.update(f'A{row_index}:G{row_index + len(rows) - 1}', rows)
    for entry in _logged_stack:
        if entry["sheet"] == sheet_name and entry["row_index"] >= row_index:
            entry["row_index"] += len(rows)
    if (_last_removed and _last_removed["sheet"] == sheet_name
            and _last_removed["row_index"] >= row_index):
        _last_removed["row_index"] += len(rows)

def _entry_row(sheet, sheet_name, occurred_at, cols, scan_all=False):
    """Find an empty slot in the event's date block, creating it if needed."""
    values = sheet.get_all_values()
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

    candidates = reversed(matching) if scan_all else matching[-1:]
    for index in candidates:
        row = values[index - 1]
        if all(len(row) < col or not row[col - 1] for col in cols):
            return index

    if matching:
        index = matching[-1] + 1
        rows = [[""] * 7]
    elif later_row is not None:
        index = later_row
        rows = [[date_str] + [""] * 6, [""] * 7]
    else:
        index = len(values) + 1
        # Keep the separator even though Sheets omits trailing empty rows.
        if len(values) > 1:
            index += 1
        sheet.update(f'A{index}:G{index}', [[date_str] + [""] * 6])
        return index

    if index <= len(values):
        _insert_entry_rows(sheet, sheet_name, index, rows)
    else:
        sheet.update(f'A{index}:G{index}', rows)
    return index

def _write_finance_entry(amount, note, occurred_at, cols):
    sheet = get_finance_sheet()
    occurred_at = occurred_at or datetime.now(VIETNAM_TZ)
    row_index = _entry_row(sheet, "finance", occurred_at, cols)
    first, _, last = cols
    sheet.update(f'{chr(64 + first)}{row_index}:{chr(64 + last)}{row_index}',
                 [["x", amount, note]])
    timestamp = occurred_at.strftime("%Y-%m-%d %H:%M:%S")
    for col in cols[:2] + (cols[2:] if note else []):
        _add_note(sheet, row_index, col, timestamp)
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
        sheet.append_row([""] * 7)

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

def handle_finance_spent(amount, note, occurred_at=None):
    _write_finance_entry(amount, note, occurred_at, [2, 3, 4])


def handle_finance_added(amount, note, occurred_at=None):
    _write_finance_entry(amount, note, occurred_at, [5, 6, 7])

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

        # Exercise is col C (index 2)
        if len(row) > 2 and row[2]:
            ex_str = row[2]
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

        # Jerk is col D (index 3)
        if len(row) > 3 and row[3]:
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
# [Date] [Weight] [Exercises] [Jerk] [Sleep] [Wake Up] [Notes]
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
        sheet.update(f'A{date_row}:G{date_row}',
                     [[today_str, "", "", "", "", "", ""]])

def handle_health_entry(col_index, value, occurred_at=None):
    """
    col_index (1-based):
    1=Date, 2=Weight, 3=Exercises, 4=Jerk, 5=Sleep, 6=Wake Up, 7=Notes

    Tries to fill an existing today's row where the target column is empty.
    Only appends a new row if no such row is available.
    """
    sheet = get_health_sheet()
    occurred_at = occurred_at or datetime.now(VIETNAM_TZ)
    timestamp = occurred_at.strftime("%Y-%m-%d %H:%M:%S")

    # Time-based entries store 'x'; the real timestamp goes in the cell note
    cell_value = "x" if col_index in (4, 5, 6) else value
    row_index = _entry_row(sheet, "health", occurred_at, [col_index], scan_all=True)
    sheet.update_cell(row_index, col_index, cell_value)
    _add_note(sheet, row_index, col_index, timestamp)
    _logged_stack.append({"sheet": "health", "row_index": row_index, "cols": [col_index]})


def _parse_note_timestamp(note_text):
    """Extract datetime from 'Logged: YYYY-MM-DD HH:MM:SS' note text."""
    if not note_text:
        return None
    first_line = note_text.strip().split("\n")[0]
    cleaned = first_line.replace("Logged:", "").strip()
    try:
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=VIETNAM_TZ)
    except ValueError:
        return None

def _get_note(sheet, row_index, col_index):
    """Safely get cell note from sheet."""
    col_letter = chr(ord('A') + col_index - 1)
    cell_a1 = f"{col_letter}{row_index}"
    try:
        if hasattr(sheet, 'get_note'):
            return sheet.get_note(cell_a1) or ""
    except Exception as e:
        print(f"Warning: Failed to get note for {cell_a1}: {e}")
    return ""

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
    event_type is 'sleep' (col 5) or 'wake' (col 6).
    """
    all_values = sheet.get_all_values()
    candidates = []
    row_date = None
    for row_index, row in enumerate(all_values[1:], 2):
        if row and row[0]:
            row_date = _get_row_date(all_values, row_index)
        if not row_date or (before and row_date.date() > before.date()):
            continue
        for col, event_type in ((5, "sleep"), (6, "wake")):
            if len(row) < col or not row[col - 1].strip():
                continue
            candidates.append((row_date, row_index, col, event_type))
    latest = None
    # Only fetch notes until the most recent eligible day has been checked.
    for row_date, row_index, col, event_type in sorted(candidates, reverse=True):
        if latest and row_date.date() < latest[1].date():
            break
        event_dt = _parse_note_timestamp(_get_note(sheet, row_index, col)) or row_date
        if (before is None or event_dt <= before) and (latest is None or event_dt > latest[1]):
            latest = (event_type, event_dt, row_index, col)
    return latest


def handle_sleep_wake_log(action_type, sender_id, occurred_at=None):
    """
    Handle logging of sleep or wake up with alternation check and 4-hour duplicate handling.
    action_type: 'sleep' (col 5) or 'wake' (col 6)
    """
    sheet = get_health_sheet()
    now = occurred_at or datetime.now(VIETNAM_TZ)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    col_index = 5 if action_type == 'sleep' else 6
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
            if diff_hours is not None and 0 <= diff_hours <= 4.0:
                # An updated event crossing midnight belongs in its new date block.
                row_date = _get_row_date(sheet.get_all_values(), last_row)
                if row_date and row_date.date() != now.date():
                    sheet.update_cell(last_row, last_col, "")
                    sheet.update_note(f'{chr(64 + last_col)}{last_row}', "")
                    _logged_stack[:] = [e for e in _logged_stack if not (
                        e["sheet"] == "health" and e["row_index"] == last_row
                        and last_col in e["cols"])]
                    handle_health_entry(col_index, timestamp, occurred_at=now)
                    send_message(sender_id, f"{emoji} {label} time updated: {timestamp} "
                                 f"(overwrote previous {last_dt:%d-%m-%Y %H:%M:%S})")
                    return
                sheet.update_cell(last_row, last_col, "x")
                _add_note(sheet, last_row, last_col, timestamp)

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
            handle_health_entry(col_index, timestamp, occurred_at=now)
            send_message(
                sender_id,
                f"⚠️ Warning: Missing {opposite_label} log before this {label}.\n"
                f"{emoji} {label} logged: {timestamp}"
            )
            return

    # Normal alternating log or first-ever log
    handle_health_entry(col_index, timestamp, occurred_at=now)
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

    sheet = get_finance_sheet() if info["sheet"] == "finance" else get_health_sheet()
    row_idx = info["row_index"]
    cols = info["cols"]

    # Read current row values for preview and undo backup
    row_data = sheet.row_values(row_idx)
    # Pad row_data to at least 7 columns
    while len(row_data) < 7:
        row_data.append("")

    # Build preview of what's being cleared
    preview_vals = [row_data[c - 1] for c in cols if row_data[c - 1].strip()]
    preview = " | ".join(preview_vals) if preview_vals else "(empty)"
    row_notes = {col: _get_note(sheet, row_idx, col) for col in range(1, 8)}

    if info["sheet"] == "health":
        # Health: always delete the entire row (removes values + cell notes)
        sheet.delete_rows(row_idx)
        _last_removed = {
            "sheet": "health",
            "action": "delete_row",
            "row_index": row_idx,
            "row_data": row_data,
            "cols": cols,
        }
        # Adjust row indices in the stack for health entries below the deleted row
        for entry in _logged_stack:
            if entry["sheet"] == "health" and entry["row_index"] > row_idx:
                entry["row_index"] -= 1
    else:
        # Finance: clear only the specific cells
        for col in cols:
            sheet.update_cell(row_idx, col, "")
            sheet.update_note(f'{chr(64 + col)}{row_idx}', "")

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

    _last_removed["notes"] = row_notes
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

    sheet = get_finance_sheet() if entry["sheet"] == "finance" else get_health_sheet()

    if entry["action"] == "clear_cols":
        for col, val in zip(entry["cols_cleared"], entry["cleared_values"]):
            sheet.update_cell(entry["row_index"], col, val)
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

    note_cols = entry.get("cols_cleared", range(1, 8))
    for col in note_cols:
        note = entry.get("notes", {}).get(col, "")
        if note:
            sheet.update_note(f'{chr(64 + col)}{entry["row_index"]}', note)
    return f"↩️ Restored last {entry['sheet']} entry."

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
        handle_health_entry(4, timestamp)
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
    delayed_label = (f" (at {occurred_at:%Y-%m-%d %H:%M:%S})" if occurred_at else "")
    # Failsafe for mobile app bugs where quick reply/ice breaker is sent as plain text
    text_lower = message_text.strip().lower()
    if text_lower == "😴 sleep":
        handle_sleep_wake_log("sleep", sender_id, occurred_at=occurred_at)
        return
    elif text_lower == "☀️ wake up":
        handle_sleep_wake_log("wake", sender_id, occurred_at=occurred_at)
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
            handle_finance_added(amount, note, occurred_at=occurred_at)
            send_message(sender_id, f"✅ Added: {amount:.3f}K" + (f" — {note}" if note else "") + delayed_label)
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: a [amount] [optional note]\nExample: a 500 salary")

    # ── FINANCE: s [amount] [note]  OR  HEALTH: s (sleep timestamp) ──
    elif keyword in ("s", "spend", "sleep"):
        is_sleep_command = keyword in ("s", "sleep") and not rest
        if is_sleep_command:
            handle_sleep_wake_log("sleep", sender_id, occurred_at=occurred_at)
            return
        if rest:
            sub = rest.split(" ", 1)
            try:
                amount = float(sub[0])
                note = sub[1].strip() if len(sub) > 1 else ""
                handle_finance_spent(amount, note, occurred_at=occurred_at)
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
            handle_health_entry(2, weight, occurred_at=occurred_at)
            send_message(sender_id, f"✅ Weight logged: {weight} kg" + delayed_label)
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: we [number]\nExample: we 70.5")

    # ── HEALTH: ex [string] ──
    elif keyword == "ex":
        if rest:
            handle_health_entry(3, rest, occurred_at=occurred_at)
            send_message(sender_id, f"✅ Exercise logged: {rest}" + delayed_label)
        else:
            send_message(sender_id, "❌ Invalid format. Use: ex [description]\nExample: ex 30 min run")

    # ── HEALTH: j (jerk timestamp) ──
    elif keyword == "j":
        timestamp = (occurred_at or datetime.now(VIETNAM_TZ)).strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(4, timestamp, occurred_at=occurred_at)
        send_message(sender_id, f"✅ Jerk logged: {timestamp}")

    # ── HEALTH: w (wake up timestamp) ──
    elif keyword == "w":
        if not rest:
            handle_sleep_wake_log("wake", sender_id, occurred_at=occurred_at)
        else:
            send_message(sender_id, "❌ Invalid format. Use: w (with nothing after it)\nExample: w")

    # ── HEALTH: n [string] ──
    elif keyword == "n":
        if rest:
            handle_health_entry(7, rest, occurred_at=occurred_at)
            send_message(sender_id, f"✅ Note logged: {rest}" + delayed_label)
        else:
            send_message(sender_id, "❌ Invalid format. Use: n [note]\nExample: n felt tired today")

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
            "⏱ Add -hours to any log for an earlier time (Vietnam time).\n"
            "  spend 100 lunch -4 | exercise 30 pushups -1.5 | sleep -8\n\n"
            "🔧 Other:\n"
            "  link        — get spreadsheet link\n"
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