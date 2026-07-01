#!/usr/bin/env python3
"""Fusion Flow V3 QAS - choice-resolution diagnostic.

Prints what the engine's resolver does against the LIVE cache for a field, so you
can see exactly why a value did or didn't resolve.

    python check_choice.py movement_type "RoRo Accompanied ICS2"
    python check_choice.py port "Belfast Port"
    python check_choice.py movement_type        # just list the cached names/values
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from .process_engine import ChoiceResolver
    from .process_data import ProcessingDb, load_db_config, DEFAULT_INI
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from process_engine import ChoiceResolver  # type: ignore
    from process_data import ProcessingDb, load_db_config, DEFAULT_INI  # type: ignore


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python check_choice.py <ChoiceField> [test value]")
        return 2
    field = sys.argv[1]
    test = sys.argv[2] if len(sys.argv) > 2 else None

    db = ProcessingDb.connect(load_db_config(DEFAULT_INI), dry_run=True)
    try:
        rows = db._query("SELECT ChoiceValue, ChoiceName FROM CFG.Choice_Value_Cache "
                         "WHERE ChoiceField = ? AND IsActive = 1 ORDER BY ChoiceValue", field)
        print(f"{field}: {len(rows)} cached value(s)")
        for r in rows[:60]:
            print(f"  {r['ChoiceValue']!r:18} <- {r['ChoiceName']!r}")
        if len(rows) > 60:
            print(f"  ... (+{len(rows) - 60} more)")
        if test is not None:
            resolver = ChoiceResolver(db, {field})
            value, matched = resolver.resolve(field, test)
            print(f"\nresolve({field}, {test!r}) -> value={value!r} matched={matched} "
                  f"member={resolver.is_member(field, value)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
