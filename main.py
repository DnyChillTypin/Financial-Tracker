import os
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

def get_today_date_str():
    return datetime.now(VIETNAM_TZ).strftime("%d-%m-%Y")

def _add_note(sheet, row_index, col_index, timestamp):
    """Add a 'Logged: <timestamp>' note to a cell. col_index is 1-based."""
    col_letter = chr(ord('A') + col_index - 1)
    sheet.update_note(f"{col_letter}{row_index}", f"Logged: {timestamp}")

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

def handle_finance_spent(amount, note):
    sheet = get_finance_sheet()
    finance_new_day_separator(sheet)
    today_str = get_today_date_str()
    timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    
    # Get all values and check if last row has today's date and empty spent portion
    all_values = sheet.get_all_values()
    if all_values:
        last_row = all_values[-1]
        # Check if last row has today's date (col 0) and spent portion is empty (cols 1-3)
        if len(last_row) > 0 and last_row[0] == today_str and not last_row[1] and not last_row[2] and not last_row[3]:
            # Update the last row instead of appending
            last_row_index = len(all_values)
            sheet.update_cell(last_row_index, 2, "x")        # time spent → x
            sheet.update_cell(last_row_index, 3, amount)     # amount spent
            sheet.update_cell(last_row_index, 4, note)       # note spent
            _add_note(sheet, last_row_index, 2, timestamp)
            _add_note(sheet, last_row_index, 3, timestamp)
            if note:
                _add_note(sheet, last_row_index, 4, timestamp)
            return
    
    # Otherwise, append a new row
    date_col = get_finance_date_col(sheet)
    sheet.append_row([date_col, "x", amount, note, "", "", ""])
    row_index = len(sheet.get_all_values())
    _add_note(sheet, row_index, 2, timestamp)
    _add_note(sheet, row_index, 3, timestamp)
    if note:
        _add_note(sheet, row_index, 4, timestamp)

def handle_finance_added(amount, note):
    sheet = get_finance_sheet()
    finance_new_day_separator(sheet)
    today_str = get_today_date_str()
    timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    
    # Get all values and check if last row has today's date and empty added portion
    all_values = sheet.get_all_values()
    if all_values:
        last_row = all_values[-1]
        # Check if last row has today's date (col 0) and added portion is empty (cols 4-6)
        if len(last_row) > 0 and last_row[0] == today_str and not last_row[4] and not last_row[5] and not last_row[6]:
            # Update the last row instead of appending
            last_row_index = len(all_values)
            sheet.update_cell(last_row_index, 5, "x")        # time added → x
            sheet.update_cell(last_row_index, 6, amount)     # amount added
            sheet.update_cell(last_row_index, 7, note)       # note added
            _add_note(sheet, last_row_index, 5, timestamp)
            _add_note(sheet, last_row_index, 6, timestamp)
            if note:
                _add_note(sheet, last_row_index, 7, timestamp)
            return
    
    # Otherwise, append a new row
    date_col = get_finance_date_col(sheet)
    sheet.append_row([date_col, "", "", "", "x", amount, note])
    row_index = len(sheet.get_all_values())
    _add_note(sheet, row_index, 5, timestamp)
    _add_note(sheet, row_index, 6, timestamp)
    if note:
        _add_note(sheet, row_index, 7, timestamp)

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

def ensure_health_today(sheet):
    """If today's date row doesn't exist yet, add separator + date row."""
    today_str = get_today_date_str()
    last_date = get_last_health_date(sheet)
    if last_date != today_str:
        if last_date is not None:
            sheet.append_row([""] * 7)
        sheet.append_row([today_str, "", "", "", "", "", ""])

def handle_health_entry(col_index, value):
    """
    col_index (1-based):
    1=Date, 2=Weight, 3=Exercises, 4=Jerk, 5=Sleep, 6=Wake Up, 7=Notes
    """
    sheet = get_health_sheet()
    ensure_health_today(sheet)
    timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # Time-based entries store 'x'; the real timestamp goes in the cell note
    is_time_entry = col_index in (4, 5, 6)
    cell_value = "x" if is_time_entry else value

    row = [""] * 7
    row[col_index - 1] = cell_value
    sheet.append_row(row)

    row_index = len(sheet.get_all_values())
    _add_note(sheet, row_index, col_index, timestamp)

# ──────────────────────────────────────────────
# REMOVE LAST ENTRY + UNDO
# ──────────────────────────────────────────────

_last_removed = None  # stores data needed to undo the last rm

def handle_remove_last():
    """
    Remove only the last individual entry from the most recent data row.
    For finance: clears just the 'added' OR 'spent' cell group, not the whole row.
    For health:  clears just the last non-empty data column.
    Deletes the row only if it becomes completely empty after clearing.
    Saves state into _last_removed so handle_undo() can restore it.
    """
    global _last_removed

    finance_sheet = get_finance_sheet()
    health_sheet  = get_health_sheet()
    fin_values    = finance_sheet.get_all_values()
    health_values = health_sheet.get_all_values()

    def find_last_data_row(values):
        """Return (0-based index, row list) of last row that has data beyond the date col."""
        for i in range(len(values) - 1, -1, -1):
            row = values[i]
            if row[0] in ("", "Date"):
                continue
            if any(row[j].strip() for j in range(1, len(row))):
                return i, list(row)
        return None, None

    fin_i,    fin_row    = find_last_data_row(fin_values)
    health_i, health_row = find_last_data_row(health_values)

    if fin_i is None and health_i is None:
        return "❌ Nothing to remove — both sheets are empty."

    # Pick sheet: finance first (most common typo target); health only if finance is empty
    if fin_i is not None:
        sheet, row_i, row_data, sheet_name = finance_sheet, fin_i, fin_row, "finance"
    else:
        sheet, row_i, row_data, sheet_name = health_sheet, health_i, health_row, "health"

    row_idx = row_i + 1  # gspread is 1-based

    # ── Determine WHICH cells to clear ──────────────────────────
    if sheet_name == "finance":
        # Finance cols (1-based): 1=Date, 2=time_s, 3=amt_s, 4=note_s,
        #                          5=time_a, 6=amt_a,  7=note_a
        # If 'added' portion is filled (col 6 = amount), that was the last entry.
        # Otherwise the 'spent' portion (col 3 = amount) is the last entry.
        has_added = len(row_data) > 5 and row_data[5].strip()
        cols_to_clear = [5, 6, 7] if has_added else [2, 3, 4]
    else:
        # Health cols (1-based): 1=Date, 2=Weight, 3=Exercise, 4=Jerk,
        #                         5=Sleep, 6=Wake Up, 7=Notes
        # Clear the last non-empty data column.
        last_col = None
        for j in range(len(row_data) - 1, 0, -1):  # skip index 0 = date
            if row_data[j].strip():
                last_col = j + 1  # convert to 1-based
                break
        if last_col is None:
            return "❌ Last row has no data to remove."
        cols_to_clear = [last_col]

    # Build preview of what's being cleared
    preview_vals = [row_data[c - 1] for c in cols_to_clear if c - 1 < len(row_data)]
    preview = " | ".join(v for v in preview_vals if v.strip())

    # Clear the cells
    for col in cols_to_clear:
        sheet.update_cell(row_idx, col, "")

    # Check if the row still has any data beyond the date column
    updated_row = sheet.row_values(row_idx)
    has_remaining = any(updated_row[j].strip() for j in range(1, len(updated_row)))

    if not has_remaining:
        # Row is now empty → delete it entirely
        sheet.delete_rows(row_idx)
        _last_removed = {
            "sheet": sheet_name,
            "action": "delete_row",
            "row_index": row_idx,
            "row_data": row_data,
        }
    else:
        _last_removed = {
            "sheet": sheet_name,
            "action": "clear_cols",
            "row_index": row_idx,
            "cols_cleared": cols_to_clear,
            "cleared_values": [row_data[c - 1] if c - 1 < len(row_data) else "" for c in cols_to_clear],
        }

    return f"🗑️ Removed: {preview}\nType 'undo' to restore."


def handle_undo():
    """Restore the entry that was cleared by the last rm/remove command."""
    global _last_removed

    if _last_removed is None:
        return "❌ Nothing to undo."

    entry = _last_removed
    _last_removed = None  # one level of undo only

    sheet = get_finance_sheet() if entry["sheet"] == "finance" else get_health_sheet()

    if entry["action"] == "clear_cols":
        for col, val in zip(entry["cols_cleared"], entry["cleared_values"]):
            sheet.update_cell(entry["row_index"], col, val)
    elif entry["action"] == "delete_row":
        sheet.insert_row(entry["row_data"], entry["row_index"])

    return f"↩️ Restored last {entry['sheet']} entry."

# ──────────────────────────────────────────────
# POSTBACK HANDLER (for persistent menu / ice breakers)
# ──────────────────────────────────────────────

def handle_postback(payload, sender_id):
    """Handle postback payloads from persistent menu and ice breakers."""
    timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")

    if payload == "HEALTH_SLEEP":
        handle_health_entry(5, timestamp)
        send_message(sender_id, f"😴 Sleep logged: {timestamp}")
    elif payload == "HEALTH_WAKEUP":
        handle_health_entry(6, timestamp)
        send_message(sender_id, f"☀️ Wake up logged: {timestamp}")
    elif payload == "HEALTH_JERK":
        handle_health_entry(4, timestamp)
        send_message(sender_id, f"✅ Jerk logged: {timestamp}")
    elif payload == "GET_STARTED":
        send_message(sender_id,
            "👋 Welcome! Here are the commands:\n\n"
            "💰 Finance:\n"
            "  s [amount] [note] — log spending\n"
            "  a [amount] [note] — log income\n"
            "  total               — today's summary\n"
            "  total d [dd/mm]     — day summary\n"
            "  total w [dd/mm]     — week summary\n"
            "  total m [1-12]      — month summary\n"
            "  total y [yyyy]      — year summary\n"
            "  total [dd/mm/yy]    — exact date summary\n"
            "  rm / remove         — remove last entry\n"
            "  undo                 — restore removed entry\n\n"
            "🏃 Health:\n"
            "  we [float]  — weight\n"
            "  ex [string] — exercise\n"
            "  n [string]  — notes\n"
            "  s           — log sleep\n"
            "  w           — log wake up\n"
            "  j           — log jerk\n\n"
            "💡 Use the ≡ menu for quick sleep/wake/jerk buttons!"
        )
    else:
        send_message(sender_id, "❓ Unknown action.")

# ──────────────────────────────────────────────
# MESSAGE PARSING
# ──────────────────────────────────────────────

def parse_and_handle(message_text, sender_id):
    # Failsafe for mobile app bugs where quick reply/ice breaker is sent as plain text
    text_lower = message_text.strip().lower()
    if text_lower == "😴 sleep":
        timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(5, timestamp)
        send_message(sender_id, f"😴 Sleep logged: {timestamp}")
        return
    elif text_lower == "☀️ wake up":
        timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(6, timestamp)
        send_message(sender_id, f"☀️ Wake up logged: {timestamp}")
        return

    parts = message_text.strip().split(" ", 1)
    keyword = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # ── FINANCE: a [amount] [note] ──
    if keyword == "a":
        sub = rest.split(" ", 1)
        try:
            amount = float(sub[0])
            note = sub[1].strip() if len(sub) > 1 else ""
            handle_finance_added(amount, note)
            send_message(sender_id, f"✅ Added: {amount:.3f}K" + (f" — {note}" if note else ""))
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: a [amount] [optional note]\nExample: a 500 salary")

    # ── FINANCE: s [amount] [note]  OR  HEALTH: s (sleep timestamp) ──
    elif keyword == "s":
        if rest:
            sub = rest.split(" ", 1)
            try:
                amount = float(sub[0])
                note = sub[1].strip() if len(sub) > 1 else ""
                handle_finance_spent(amount, note)
                send_message(sender_id, f"✅ Spent: {amount:.3f}K" + (f" — {note}" if note else ""))
            except ValueError:
                send_message(sender_id,
                    "❌ Invalid format.\n"
                    "For spending: s [amount] [optional note] — e.g. s 15 lunch\n"
                    "For sleep: send 's' with nothing after it"
                )
        else:
            timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
            handle_health_entry(5, timestamp)
            send_message(sender_id, f"😴 Sleep logged: {timestamp}")

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
            handle_health_entry(2, weight)
            send_message(sender_id, f"✅ Weight logged: {weight} kg")
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: we [number]\nExample: we 70.5")

    # ── HEALTH: ex [string] ──
    elif keyword == "ex":
        if rest:
            handle_health_entry(3, rest)
            send_message(sender_id, f"✅ Exercise logged: {rest}")
        else:
            send_message(sender_id, "❌ Invalid format. Use: ex [description]\nExample: ex 30 min run")

    # ── HEALTH: j (jerk timestamp) ──
    elif keyword == "j":
        timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(4, timestamp)
        send_message(sender_id, f"✅ Jerk logged: {timestamp}")

    # ── HEALTH: w (wake up timestamp) ──
    elif keyword == "w":
        if not rest:
            timestamp = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
            handle_health_entry(6, timestamp)
            send_message(sender_id, f"☀️ Wake up logged: {timestamp}")
        else:
            send_message(sender_id, "❌ Invalid format. Use: w (with nothing after it)\nExample: w")

    # ── HEALTH: n [string] ──
    elif keyword == "n":
        if rest:
            handle_health_entry(7, rest)
            send_message(sender_id, f"✅ Note logged: {rest}")
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
            "  s [amount] [note] — log spending\n"
            "  a [amount] [note] — log income\n"
            "  total               — today's summary\n"
            "  total d [dd/mm]     — day summary\n"
            "  total w [dd/mm]     — week summary\n"
            "  total m [1-12]      — month summary\n"
            "  total y [yyyy]      — year summary\n"
            "  total [dd/mm/yy]    — exact date summary\n"
            "  rm / remove         — remove last entry\n"
            "  undo                 — restore removed entry\n\n"
            "🏃 Health:\n"
            "  we [float]  — weight\n"
            "  ex [string] — exercise\n"
            "  n [string]  — notes\n"
            "  s           — log sleep\n"
            "  w           — log wake up\n"
            "  j           — log jerk\n\n"
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