---
name: financial-tracker
description: >-
  Core procedures, architecture patterns, and domain memory for the Financial-Tracker
  Facebook Messenger chatbot and Google Sheets tracker. Use when building, testing,
  refactoring, or debugging features in this repository.
---

# Financial Tracker Knowledge & Procedures

This skill encapsulates workflow procedures and domain knowledge for developing, maintaining, and debugging the Financial-Tracker application.

## Quick Reference Commands

- **Run unit tests**:
  ```powershell
  python test_parse.py
  ```
- **Start local server**:
  ```powershell
  python main.py
  ```
- **Start production server (WSGI)**:
  ```powershell
  gunicorn main:app
  ```

---

## Architectural Rules & Gotchas

1. **Vietnam Timezone (UTC+7)**:
   - Always calculate dates and timestamps using `VIETNAM_TZ = timezone(timedelta(hours=7))`.
   - Never use naive `datetime.now()` or `date.today()`, which would default to UTC on Render.

2. **Google Sheets Explicit Addressing**:
   - Google Sheets API table detection can shift cell insertions horizontally if blank rows or partial rows are appended.
   - For health separators and row creations, use explicit range coordinates (e.g., `sheet.update('A{row}', [[""]])`).

3. **Facebook Messenger Constraints**:
   - Message character cap is 2000 characters. Always safeguard summaries in `format_total_message` against exceeding this limit.
   - Dedup incoming messages via `_processed_messages` using message ID (`mid`) and a 300-second TTL.

4. **Finance vs. Health Sheet Layouts**:
   - Finance (`sheet1`): `[Date, time spent, amount spent, note spent, time added, amount added, note added]`. Spent & added can share the same row if logged on the same day.
   - Health (`health`): `[Date, Weight, Exercises, Jerk, Sleep, Wake Up, Notes]`. Time entries (`j`, `s`, `w`) store `"x"` with timestamps in cell notes.

---

## Modifying Parsing or Adding New Commands

1. Update argument parsing in `parse_and_handle(message_text, sender_id)` in [main.py](file:///a:/Financial-Tracker/main.py).
2. If adding date ranges or total metrics, update `parse_total_args`, `get_total_data`, and `format_total_message`.
3. Add corresponding test cases in [test_parse.py](file:///a:/Financial-Tracker/test_parse.py) and execute `python test_parse.py` to verify.
4. Keep the help menu (`else` block in `parse_and_handle`) synchronized with all available commands.
