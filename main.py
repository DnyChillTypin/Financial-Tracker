import os
from datetime import datetime
from flask import Flask, request
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

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
    return datetime.now().strftime("%d-%m-%Y")

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

def handle_finance_spent(amount, note):
    sheet = get_finance_sheet()
    finance_new_day_separator(sheet)
    date_col = get_finance_date_col(sheet)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([date_col, timestamp, amount, note, "", "", ""])

def handle_finance_added(amount, note):
    sheet = get_finance_sheet()
    finance_new_day_separator(sheet)
    date_col = get_finance_date_col(sheet)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([date_col, "", "", "", timestamp, amount, note])

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
    row = [""] * 7
    row[col_index - 1] = value
    sheet.append_row(row)

# ──────────────────────────────────────────────
# MESSAGE PARSING
# ──────────────────────────────────────────────

def parse_and_handle(message_text, sender_id):
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
            send_message(sender_id, f"✅ Added: ${amount:.2f}" + (f" — {note}" if note else ""))
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
                send_message(sender_id, f"✅ Spent: ${amount:.2f}" + (f" — {note}" if note else ""))
            except ValueError:
                send_message(sender_id,
                    "❌ Invalid format.\n"
                    "For spending: s [amount] [optional note] — e.g. s 15.50 lunch\n"
                    "For sleep: send 's' with nothing after it"
                )
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            handle_health_entry(5, timestamp)
            send_message(sender_id, f"✅ Sleep logged: {timestamp}")

    # ── HEALTH: w [float] ──
    elif keyword == "w":
        try:
            weight = float(rest)
            handle_health_entry(2, weight)
            send_message(sender_id, f"✅ Weight logged: {weight} kg")
        except ValueError:
            send_message(sender_id, "❌ Invalid format. Use: w [number]\nExample: w 70.5")

    # ── HEALTH: ex [string] ──
    elif keyword == "ex":
        if rest:
            handle_health_entry(3, rest)
            send_message(sender_id, f"✅ Exercise logged: {rest}")
        else:
            send_message(sender_id, "❌ Invalid format. Use: ex [description]\nExample: ex 30 min run")

    # ── HEALTH: j (jerk timestamp) ──
    elif keyword == "j":
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(4, timestamp)
        send_message(sender_id, f"✅ Jerk logged: {timestamp}")

    # ── HEALTH: wu (wake up timestamp) ──
    elif keyword == "wu":
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handle_health_entry(6, timestamp)
        send_message(sender_id, f"✅ Wake up logged: {timestamp}")

    # ── HEALTH: n [string] ──
    elif keyword == "n":
        if rest:
            handle_health_entry(7, rest)
            send_message(sender_id, f"✅ Note logged: {rest}")
        else:
            send_message(sender_id, "❌ Invalid format. Use: n [note]\nExample: n felt tired today")

    # ── UNKNOWN ──
    else:
        send_message(sender_id,
            "❓ Unknown command. Here's what you can use:\n\n"
            "💰 Finance:\n"
            "  s [amount] [note] — log spending\n"
            "  a [amount] [note] — log income\n\n"
            "🏃 Health:\n"
            "  w [float]   — weight\n"
            "  ex [string] — exercise\n"
            "  j           — jerk (logs timestamp)\n"
            "  s           — sleep (logs timestamp)\n"
            "  wu          — wake up (logs timestamp)\n"
            "  n [string]  — notes"
        )

# ──────────────────────────────────────────────
# MESSENGER
# ──────────────────────────────────────────────

def send_message(recipient_id, message_text):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    response = requests.post(
        "https://graph.facebook.com/v19.0/me/messages",
        params=params, headers=headers, json=data
    )
    return response.status_code

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
                if messaging_event.get("message"):

                    if messaging_event["message"].get("is_echo"):
                        continue

                    sender_id = messaging_event["sender"]["id"]
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