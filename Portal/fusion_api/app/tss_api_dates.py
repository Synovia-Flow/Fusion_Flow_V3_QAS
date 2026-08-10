"""Date helpers for filters that must use TSS API payload fields only.

These helpers deliberately operate on values extracted from TSS payloads
(arrival_date_time and friends), not on Fusion metadata such as CreatedAt,
UpdatedAt, LastSyncedAt or STG dates.

Ported from Fusion_Flow_V2_BKD dev02 (commit 961f2e9, app/tss_api_dates.py).
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta

TSS_API_DATE_RANGE_OPTIONS = (
    ('all', 'All TSS date/times'),
    ('this_week', 'TSS arrival date/time this week'),
    ('this_month', 'TSS arrival date/time this month'),
    ('last_6_months', 'TSS arrival date/time last 6 months'),
    ('last_12_months', 'TSS arrival date/time last 12 months'),
    ('over_12_months', 'TSS arrival date/time over 12 months'),
)

_VALID_DATE_RANGE_KEYS = {key for key, _label in TSS_API_DATE_RANGE_OPTIONS}


def normalise_tss_api_date_range(value: object) -> str:
    key = str(value or 'all').strip().lower()
    return key if key in _VALID_DATE_RANGE_KEYS else 'all'


def parse_tss_api_datetime(value: object) -> datetime | None:
    """Parse common TSS API date/datetime strings into a naive datetime."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)

    text = str(value).strip()
    if not text:
        return None

    iso_text = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        return datetime.fromisoformat(iso_text).replace(tzinfo=None)
    except ValueError:
        pass

    formats = (
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
        '%d.%m.%Y %H:%M:%S',
        '%d.%m.%Y %H:%M',
        '%d.%m.%Y',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _shift_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def tss_api_date_bounds(range_key: str, *, today: date | None = None) -> tuple[datetime | None, datetime | None]:
    """The half-open [start, end) window for a range key, or (None, None) for 'all'.

    Exposed so a caller that can push the filter into SQL uses exactly the same
    definition of "this week" as the in-Python predicate below. Two definitions of
    a date window is one too many.
    """
    if normalise_tss_api_date_range(range_key) == 'all':
        return None, None
    return _range_bounds(range_key, today or date.today())


def _range_bounds(range_key: str, today: date) -> tuple[datetime | None, datetime | None]:
    key = normalise_tss_api_date_range(range_key)
    start_of_today = datetime.combine(today, time.min)
    tomorrow = start_of_today + timedelta(days=1)

    if key == 'this_week':
        start = datetime.combine(today - timedelta(days=today.weekday()), time.min)
        return start, start + timedelta(days=7)
    if key == 'this_month':
        start = datetime(today.year, today.month, 1)
        next_month = datetime.combine(_shift_months(start.date(), 1), time.min)
        return start, next_month
    if key == 'last_6_months':
        return datetime.combine(_shift_months(today, -6), time.min), tomorrow
    if key == 'last_12_months':
        return datetime.combine(_shift_months(today, -12), time.min), tomorrow
    if key == 'over_12_months':
        return None, datetime.combine(_shift_months(today, -12), time.min)
    return None, None


def tss_api_date_in_range(value: object, range_key: str, *, today: date | None = None) -> bool:
    key = normalise_tss_api_date_range(range_key)
    if key == 'all':
        return True

    parsed = parse_tss_api_datetime(value)
    if parsed is None:
        return False

    start, end = _range_bounds(key, today or date.today())
    if start is not None and parsed < start:
        return False
    if end is not None and parsed >= end:
        return False
    return True
