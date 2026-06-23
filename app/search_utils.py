"""Shared search helpers for operational worklists."""

from __future__ import annotations

import re
from typing import Any, Iterable


def compact_search_token(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def search_matches_values(search: Any, values: Iterable[Any]) -> bool:
    text = str(search or "").strip()
    if not text:
        return True

    haystack = " ".join(str(value or "") for value in values)
    if text.casefold() in haystack.casefold():
        return True

    compact = compact_search_token(text)
    return bool(compact and compact in compact_search_token(haystack))
