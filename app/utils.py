"""
utils.py — Pure helper functions shared across the application.
"""

import re
from datetime import datetime, timezone


def format_price(raw_entry: str) -> str:
    """
    Format a price string into a human-readable form.

    "118250.5"  → "118,250.50"
    "3367.4"    → "3,367.40"
    Falls back to the raw string if it cannot be parsed as a float.
    """
    try:
        value = float(raw_entry)
        return f"{value:,.2f}"
    except (ValueError, TypeError):
        return str(raw_entry)


def parse_tradingview_time(raw_time: str) -> str:
    """
    Parse a time string and return it formatted in IST (UTC+5:30).

    Handles:
      • ISO-8601        "2024-01-15T10:35:00Z"
      • Space-separated "2024-01-15 10:35:00"  (assumed UTC)
      • Pandas datetime "2024-01-15 10:35:00"  (assumed UTC)
      • Unix epoch      "1705315800"

    Falls back to the raw string if no format matches.
    Output format: "10:35 PM IST"
    """
    from datetime import timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    raw = str(raw_time).strip()

    # Unix epoch (10-digit seconds or 13-digit milliseconds)
    if re.fullmatch(r"\d{10,13}", raw):
        epoch = int(raw[:10])
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(IST)
        return dt.strftime("%I:%M %p IST").lstrip("0")

    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            # If no tzinfo (naive), assume UTC then convert to IST
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_ist = dt.astimezone(IST)
            return dt_ist.strftime("%I:%M %p IST").lstrip("0")
        except ValueError:
            continue

    return raw



def normalize_timeframe(raw_timeframe: str) -> str:
    """
    Normalise shorthand timeframe codes to display labels.

    "5m" → "5 Minutes", "1h" → "1 Hour", etc.
    Returns the raw value unchanged if no mapping is found.
    """
    mapping = {
        "1m":   "1 Minute",
        "3m":   "3 Minutes",
        "5m":   "5 Minutes",
        "5min": "5 Minutes",
        "15m":  "15 Minutes",
        "30m":  "30 Minutes",
        "1h":   "1 Hour",
        "2h":   "2 Hours",
        "4h":   "4 Hours",
        "1d":   "1 Day",
        "1w":   "1 Week",
    }
    return mapping.get(raw_timeframe.lower(), raw_timeframe)
