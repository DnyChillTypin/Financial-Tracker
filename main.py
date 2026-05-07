import os
import re
from datetime import datetime
from flask import Flask, request
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configuration
PAGE_ACCESS_TOKEN = os.getenv('PAGE_ACCESS_TOKEN')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE')
SPREADSHEET_NAME = os.getenv('SPREADSHEET_NAME')

# Setup Google Sheets
def get_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).sheet1
    return sheet

def send_message(recipient_id, message_text):
    params = {
        "access_token": PAGE_ACCESS_TOKEN
    }
    headers = {
        "Content-Type": "application/json"
    }
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    response = requests.post(
        "https://graph.facebook.com/v19.0/me/messages",
        params=params,
        headers=headers,
        json=data
    )
    return response.status_code

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Verify the webhook subscription with Meta"""
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
    """Handle incoming webhook events from Messenger"""
    data = request.get_json()

    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):
                if messaging_event.get("message"):
                    sender_id = messaging_event["sender"]["id"]
                    message_text = messaging_event["message"].get("text", "").strip()
                    
                    if not message_text:
                        continue

                    # Expected format: Amount Category [Optional Note]
                    # Example: 15.50 food lunch
                    parts = message_text.split(" ", 2)
                    
                    if len(parts) >= 2:
                        try:
                            amount = float(parts[0])
                            category = parts[1]
                            note = parts[2] if len(parts) > 2 else ""
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                            # Append to Google Sheets
                            try:
                                sheet = get_sheet()
                                row = [timestamp, amount, category, note, message_text]
                                sheet.append_row(row)
                                send_message(sender_id, f"✅ Logged: ${amount:.2f} for {category}")
                            except Exception as e:
                                print(f"Google Sheets Error: {e}")
                                send_message(sender_id, f"❌ Failed to save to Sheets. Error: {str(e)}")

                        except ValueError:
                            # The amount wasn't a valid number
                            send_message(sender_id, "❌ Invalid format. Please use: [Amount] [Category] [Optional Note]\nExample: 15.50 food lunch")
                    else:
                        send_message(sender_id, "❌ Invalid format. Please use: [Amount] [Category] [Optional Note]\nExample: 15.50 food lunch")

        return 'EVENT_RECEIVED', 200
    else:
        return 'Not Found', 404

if __name__ == '__main__':
    app.run(port=5000, debug=True)
