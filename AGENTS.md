# Financial-Tracker Project Memories & Agent Guidelines

This document serves as the persistent memory and operational guide for the **Financial-Tracker** project. It outlines the architecture, data schemas, command syntax, deployment environment, and critical past bug fixes to ensure consistent and safe future development.

---

## 1. Project Overview

- **Purpose**: A personal financial and daily health tracker chatbot powered by Facebook Messenger and backed by Google Sheets.
- **Stack**:
  - **Language**: Python 3
  - **Web Framework**: Flask (`main.py`)
  - **Integrations**: `gspread` + `oauth2client` (Google Sheets API), `requests` (Facebook Graph API)
  - **Production Server**: `gunicorn` on Render
  - **Configuration**: `.env` via `python-dotenv`
- **Timezone**: **Vietnam Standard Time (UTC+7)** (`VIETNAM_TZ = timezone(timedelta(hours=7))`). All timestamps, date logic, separators, and summaries **must** strictly use `VIETNAM_TZ`. Never rely on server local time (Render defaults to UTC).

---

## 2. Environment Configuration

The application requires the following environment variables in `.env`:
- `PAGE_ACCESS_TOKEN`: Facebook Page Access Token for Graph API (sending messages and setting profile/menus).
- `VERIFY_TOKEN`: Secret token for Facebook Webhook verification (`/webhook` GET).
- `GOOGLE_CREDENTIALS_FILE`: Path to Google Cloud Service Account JSON credentials file.
- `SPREADSHEET_NAME`: Target Google Spreadsheet name.

---

## 3. Google Sheets Schema & Behaviors

The Google Spreadsheet contains two core sheets:

### A. Finance Sheet (`sheet1`)
- **Columns (7)**:
  1. `Date` (Col A): Formatted `DD-MM-YYYY`. Stored **only** on the first row of each day; subsequent rows for that day leave Col A blank.
  2. `time spent` (Col B): Filled with `"x"`. The exact timestamp (`YYYY-MM-DD HH:MM:SS`) is stored in the **cell note** via `_add_note`.
  3. `amount spent` (Col C): Float value in thousands (K VND), e.g. `15` = 15.000K VND.
  4. `note spent` (Col D): Optional text note.
  5. `time added` (Col E): Filled with `"x"`, exact timestamp in cell note.
  6. `amount added` (Col F): Float value in thousands (K VND).
  7. `note added` (Col G): Optional text note.
- **Day Separator**: When a new day begins, an empty row (`[""] * 7`) separates days.
- **Row Pairing / In-place Update**:
  - When logging spent or added, if the last row is today's date and the opposite side is empty, update the empty columns of that row instead of appending a new row.

### B. Health Sheet (`health`)
- **Columns (7)**:
  1. `Date` (Col A): Formatted `DD-MM-YYYY`.
  2. `Weight` (Col B): Float (kg), e.g., `70.5`.
  3. `Exercises` (Col C): Text description (e.g., `30 pushups`, `10 pull-ups`).
  4. `Jerk` (Col D): Stores `"x"`, timestamp in cell note.
  5. `Sleep` (Col E): Stores `"x"`, timestamp in cell note.
  6. `Wake Up` (Col F): Stores `"x"`, timestamp in cell note.
  7. `Notes` (Col G): Text note.
- **Day Separator & In-place Fill**:
  - Checks `ensure_health_today`. Uses explicit cell ranges (e.g. `A{sep_row}`, `A{date_row}:G{date_row}`) to prevent Google Sheets auto-table detection from misaligning columns.
  - When logging an entry, scans backwards through today's rows to fill an existing row where that column is empty before appending a new row.

---

## 4. Chat Commands & Parsing Rules

All messages arriving from Facebook Messenger are routed to `parse_and_handle(message_text, sender_id)`:

| Domain | Command Syntax | Description | Example |
|---|---|---|---|
| **Finance** | `s` / `spend` / `spent [amount] [note]` | Log spending (amount in K) | `spend 15 lunch` |
| | `a` / `add` / `income [amount] [note]` | Log income/added (amount in K) | `income 500 salary` |
| | `total` | Summary for today (spent, added, exercises, jerk) | `total` |
| | `total d [dd/mm]` | Summary for a specific day | `total d 15/09` |
| | `total w [dd/mm]` | Summary for that week (Mon–Sun) | `total w 15/09` |
| | `total m [1-12]` | Summary for that month | `total m 9` |
| | `total y [yyyy]` | Summary for that year | `total y 2026` |
| | `total [dd/mm/yy]` | Summary for exact date | `total 15/09/26` |
| | `rm` or `remove` | Delete the last logged entry | `rm` |
| | `undo` | Restore the last removed entry | `undo` |
| **Health** | `we` / `weight [float]` | Log weight in kg | `weight 70.5` |
| | `ex` / `exercise` / `workout [string]` | Log exercise | `exercise 30 pushups` |
| | `n` / `note [string]` | Log health note | `note feeling energetic` |
| | `s` / `sleep` (alone) | Log sleep timestamp | `sleep` |
| | `w` / `wake` / `wake up` / `wakeup` (alone) | Log wake up timestamp | `wake up` |
| | `j` / `jerk` | Log jerk timestamp | `jerk` |
| **System** | `link` | Returns the Google Sheet URL | `link` |
| | `menu` | Sends quick reply buttons for Sleep / Wake Up | `menu` |
| | `setup` | Calls Graph API to configure persistent menu & ice breakers | `setup` |

All logging commands accept a trailing delay in hours, with or without a space after the hyphen. For example, `sleep -10`, `sleep - 10`, and `spend 100 lunch -4` record the event at the corresponding earlier Vietnam time and date.

---

## 5. Critical Gotchas & Architectural Memories

### 1. Cell Wandering Bug (Google Sheets API)
- When appending rows with empty elements or inserting blank rows, Google Sheets API table detection can place cells into unexpected columns (e.g., starting in Col B instead of Col A).
- **Rule**: Always write explicit cell ranges (e.g. `sheet.update(f'A{row}', [[""]])` and `sheet.update(f'A{row}:G{row}', [...])`).

### 2. Render Cold Starts & Webhook Deduplication
- On Render free tier, spin-up from sleep can take 30–60 seconds. Facebook re-transmits unacknowledged webhook events every few seconds during cold starts.
- **Rule**: Deduplication is handled by `_processed_messages` with a 5-minute TTL (`MESSAGE_TTL = 300`). Always keep deduplication active on `/webhook`.

### 3. Facebook Messenger 2000-Character Limit
- Facebook Graph API rejects messages longer than 2000 characters.
- **Rule**: Any generated message (such as monthly or yearly `total` summaries with numerous entries) must remain concise or be chunked to avoid 400 Bad Request errors from Facebook.

### 4. Stack-Based History (`rm` & `undo`)
- `_logged_stack` tracks recent entries: `{"sheet": "finance"|"health", "row_index": int, "cols": [int]}`.
- Deleting a health entry deletes the entire row (clearing cell notes) and adjusts row indices for lower entries in the stack.
- Deleting a finance entry clears the specific cells, deleting the row only if it becomes completely empty.
- `_last_removed` preserves the last deleted entry for single-level `undo`.

### 5. Mobile App Bug Fallback
- On some Facebook Messenger mobile clients, tapping ice breakers or quick replies transmits the button label as plain text (e.g., `"😴 sleep"`, `"☀️ wake up"`).
- `parse_and_handle` explicitly includes string fallbacks at the top of the function to catch these exact phrases.

### 6. Currency Formatting
- Currency amounts are formatted with `.3f` representing thousands (e.g. `15.000K` for 15K).

### 7. Sleep & Wake Up Alternation and Duplicate Handling
- Sleep (Col E, 5) and Wake Up (Col F, 6) logs must strictly alternate.
- **Duplicate within 4 hours** (Sleep after Sleep <= 4h, or Wake Up after Wake Up <= 4h): Overwrites the previous timestamp note in place (`E{row}` or `F{row}`) and notifies the user with the updated time and previous overwritten time.
- **Duplicate after > 4 hours** (Sleep after Sleep > 4h, or Wake Up after Wake Up > 4h): Logs as a new entry (new row) and alerts the user with a warning that the alternating event was missed (e.g. `⚠️ Warning: Missing Wake Up log before this Sleep.`).
- **Alternating event**: Logs normally as an in-place fill on today's row or appends a new row.

---

## 6. Testing & Development Guidelines

- **Unit Testing**:
  - Run `python test_parse.py` to test argument parsing logic for `total`.
  - Run `python test_sleep_wake.py` to test sleep/wake alternation and duplicate window handling.
- **Local Run**: `python main.py` runs Flask on port 5000 in debug mode.
- **Tunneling**: For local webhook testing with Facebook, use a tunnel like Cloudflare Tunnel (`cloudflared`) or ngrok.
- **Preserving Comments**: When modifying `main.py`, preserve all section banners and existing docstrings.

