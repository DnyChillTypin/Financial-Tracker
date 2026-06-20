import sys
from datetime import datetime, timezone, timedelta

VIETNAM_TZ = timezone(timedelta(hours=7))

def parse_total_args(rest):
    now = datetime.now(VIETNAM_TZ)
    today = now.date()

    if not rest:
        return today, today, "today"

    parts = rest.split(" ", 1)
    mode = parts[0].lower()

    date_arg = parts[1].strip() if len(parts) > 1 else ""

    if mode == "m":
        if not date_arg:
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
    else:
        return None, None, "Other mode"

print(parse_total_args("m"))
print(parse_total_args("m 6"))
