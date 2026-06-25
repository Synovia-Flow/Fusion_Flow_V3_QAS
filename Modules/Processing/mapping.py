#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 2 (Processing) - source->canonical mapping tables.

Pure-data, stdlib-only, dependency-free module. NO database calls, NO file or
network access, NO secrets. It is the single source of truth for:

  * how Birkdale ING raw columns map onto the canonical PRS TSS field names,
  * the Birkdale (BKD) QAS rule literals and the Critical Rule citations used
    when those rules are applied (logged to EXC.Data_Processing_Enhancement), and
  * the mandatory / conditional validation rule sets, keyed by movement_type.

It is imported by the processing runner (Modules/Processing/process_data.py),
which owns all I/O. Keep this module side-effect-free so it can be unit-tested
and py_compile'd on its own.

The Birkdale source shapes (see Modules/Ingestion):
  * ING.BKD_Raw_ENS    - one typed row per ENS-Headers CSV row. Column names are
                         the ENS API field names parsed in ens_headers.py.
  * ING.BKD_Raw_Sales_Orders - one row per Sales Order line, the verbatim row
                         landed as JSON in PayloadJson. Keys are the Sales Order
                         workbook column headers, normalised to snake_case (see
                         the goods Excel prototype's column aliases).

Critical Rules honoured here (see architect design sections 4-5):
  Rule 3  - 3a (RoRo Accompanied) effectively-mandatory set.
  Rule 4  - arrival_date_time strict format + not-past / <=14-days-future bounds.
  Rule 9  - choice value resolution (label cited; the cache introspection lives
            in the runner, not here).
  Rule 10 - BKD goods_domestic_status = 'D'.
  Rule 11 - BKD transport_charges = 'Y'.
  Rule 12 - BKD fixed arrival_port = 'GBAUBELBELBEL'.
  Rule 13 - BKD importer EORI fallback.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# =============================================================================
# 1. Source -> canonical field maps
# =============================================================================

# ING.BKD_Raw_ENS column name -> PRS.ENS_Header canonical field name.
# Every ENS source field present in 008_ing_bkd_raw_tables.sql / ens_headers.py
# is mapped here. The ENS column names already match the canonical header field
# names for most fields; the map is explicit so the runner never guesses.
ENS_CSV_TO_HEADER: dict[str, str] = {
    "movement_type": "movement_type",
    "type_of_passive_transport": "type_of_passive_transport",
    "identity_no_of_transport": "identity_no_of_transport",
    "nationality_of_transport": "nationality_of_transport",
    "carrier_eori": "carrier_eori",
    # ENS "transport document number / ICR number" is the consignment-level
    # transport document reference in TSS terms.
    "transport_document_number": "transport_document_number",
    "arrival_date_time": "arrival_date_time",
    "arrival_port": "arrival_port",
    "place_of_loading": "place_of_loading",
    "place_of_acceptance_same_as_loading": "place_of_acceptance_same_as_loading",
    "place_of_unloading": "place_of_unloading",
    "place_of_delivery_same_as_unloading": "place_of_delivery_same_as_unloading",
    "transport_charges": "transport_charges",
}

# Birkdale Sales Order column header (normalised snake_case as landed in
# PayloadJson) -> PRS.Goods_Item field name. Header variants come from the goods
# Excel validation prototype's column aliases; the runner normalises a raw header
# with the same rule (lower-case, non-alphanumeric -> "_", strip edges) before
# look-up. ENS-context goods fields only (R1 decision Q3 - SD valuation later).
SALES_ORDER_TO_GOODS: dict[str, str] = {
    # description
    "goods_description": "goods_description",
    "description": "goods_description",
    "item_description": "goods_description",
    "product_description": "goods_description",
    "commodity_description": "goods_description",
    # number of packages
    "number_of_packages": "number_of_packages",
    "packages": "number_of_packages",
    "package_count": "number_of_packages",
    "no_of_packages": "number_of_packages",
    "number_packages": "number_of_packages",
    # type of packages
    "type_of_packages": "type_of_packages",
    "package_type": "type_of_packages",
    "kind_of_packages": "type_of_packages",
    "package_kind": "type_of_packages",
    # gross mass
    "gross_mass_kg": "gross_mass_kg",
    "gross_mass": "gross_mass_kg",
    "gross_weight": "gross_mass_kg",
    "gross_weight_kg": "gross_mass_kg",
    "gross_kg": "gross_mass_kg",
    # net mass
    "net_mass_kg": "net_mass_kg",
    "net_mass": "net_mass_kg",
    "net_weight": "net_mass_kg",
    "net_weight_kg": "net_mass_kg",
    "net_kg": "net_mass_kg",
    # commodity / tariff
    "commodity_code": "commodity_code",
    "hs_code": "commodity_code",
    "tariff_code": "commodity_code",
    "cn_code": "commodity_code",
    "goods_code": "commodity_code",
    # invoice value
    "item_invoice_amount": "item_invoice_amount",
    "invoice_amount": "item_invoice_amount",
    "item_value": "item_invoice_amount",
    "value": "item_invoice_amount",
    "customs_value": "item_invoice_amount",
    # invoice currency
    "item_invoice_currency": "item_invoice_currency",
    "invoice_currency": "item_invoice_currency",
    "currency": "item_invoice_currency",
    "value_currency": "item_invoice_currency",
    # country of origin
    "country_of_origin": "country_of_origin",
    "origin_country": "country_of_origin",
    "coo": "country_of_origin",
    "origin": "country_of_origin",
    # package marks (TSS goods-mandatory; 'ADDR' fallback applied by the runner)
    "package_marks": "package_marks",
    "marks": "package_marks",
    "shipping_marks": "package_marks",
    # controlled-goods type (goods-level)
    "controlled_goods_type": "controlled_goods_type",
    "controlled_type": "controlled_goods_type",
    "dangerous_goods_type": "controlled_goods_type",
    # goods item ordinal / number (used for line ordering, not a TSS field per se)
    "goods_item_number": "goods_id",  # TODO confirm: source line no. -> goods_id
    "goods_item_no": "goods_id",      # TODO confirm
    "item_number": "goods_id",        # TODO confirm
    "item_no": "goods_id",            # TODO confirm
    "line_number": "goods_id",        # TODO confirm
    "line_no": "goods_id",            # TODO confirm
    # SDI-context goods fields that may already appear in the workbook. Mapped
    # for traceability; populated later (Q3) but harmless if present.
    "procedure_code": "procedure_code",
    "procedure": "procedure_code",
    "additional_procedure_code": "additional_procedure_code",
    "additional_procedure": "additional_procedure_code",
    "additional_procedure_codes": "additional_procedure_code",
    "taric_code": "taric_code",
    "taric": "taric_code",
    "preference": "preference",
    "preference_code": "preference",
    "valuation_method": "valuation_method",
    "nature_of_transaction": "nature_of_transaction",
    "transaction_nature": "nature_of_transaction",
    "ni_additional_information_codes": "ni_additional_information_codes",
    "national_additional_codes": "national_additional_code",  # TODO confirm scope
    "ni_addl_info_code": "ni_additional_information_codes",
    "invoice_number": "invoice_number",
    "invoice_no": "invoice_number",
    "commercial_invoice": "invoice_number",
}

# Sales Order column header (normalised) -> PRS.Consignment field name.
# Consignment-level grouping references, parties, and flags.
SALES_ORDER_TO_CONSIGNMENT: dict[str, str] = {
    # grouping / references
    "transport_document_number": "transport_document_number",
    "transport_doc_number": "transport_document_number",
    "tdn": "transport_document_number",
    "transport_document": "transport_document_number",
    "document_number": "transport_document_number",
    "trader_reference": "trader_reference",
    "trader_ref": "trader_reference",
    "customer_reference": "trader_reference",
    "manifest_reference": "trader_reference",
    "document_no": "trader_reference",
    # consignment-level description
    "consignment_description": "goods_description",  # TODO confirm vs item desc
    "consignment_desc": "goods_description",          # TODO confirm
    # controlled goods (consignment-mandatory in TSS)
    "controlled_goods": "controlled_goods",
    "is_controlled_goods": "controlled_goods",
    "controlled": "controlled_goods",
    # domestic status / destination / container
    "goods_domestic_status": "goods_domestic_status",
    "domestic_status": "goods_domestic_status",
    "destination_country": "destination_country",
    "dest_country": "destination_country",
    "country_of_destination": "destination_country",
    "container_indicator": "container_indicator",
    "containerised": "container_indicator",
    "containerized": "container_indicator",
    "container": "container_indicator",
    # importer party
    "importer_eori": "importer_eori",
    "importer_eori_number": "importer_eori",
    "importer_name": "importer_name",
    "importer_street_number": "importer_street_number",
    "importer_street_and_number": "importer_street_number",
    "importer_address": "importer_street_number",
    "importer_city": "importer_city",
    "importer_postcode": "importer_postcode",
    "importer_post_code": "importer_postcode",
    "importer_country": "importer_country",
    # consignor party (GB EORI not accepted - validated in the runner)
    "consignor_eori": "consignor_eori",
    "shipper_eori": "consignor_eori",
    "sender_eori": "consignor_eori",
    "consignor_name": "consignor_name",
    "shipper_name": "consignor_name",
    "sender_name": "consignor_name",
    "consignor_street_number": "consignor_street_number",
    "consignor_street_and_number": "consignor_street_number",
    "consignor_address": "consignor_street_number",
    "consignor_city": "consignor_city",
    "consignor_postcode": "consignor_postcode",
    "consignor_post_code": "consignor_postcode",
    "consignor_country": "consignor_country",
    # consignee party
    "consignee_eori": "consignee_eori",
    "receiver_eori": "consignee_eori",
    "consignee_name": "consignee_name",
    "receiver_name": "consignee_name",
    "consignee_street_number": "consignee_street_number",
    "consignee_street_and_number": "consignee_street_number",
    "consignee_address": "consignee_street_number",
    "consignee_city": "consignee_city",
    "consignee_postcode": "consignee_postcode",
    "consignee_post_code": "consignee_postcode",
    "consignee_country": "consignee_country",
    # exporter party
    "exporter_eori": "exporter_eori",
    "exporter_name": "exporter_name",
    "exporter_street_number": "exporter_street_number",
    "exporter_street_and_number": "exporter_street_number",
    "exporter_address": "exporter_street_number",
    "exporter_city": "exporter_city",
    "exporter_postcode": "exporter_postcode",
    "exporter_post_code": "exporter_postcode",
    "exporter_country": "exporter_country",
    # buyer party (uses buyer_street_and_number column per design)
    "buyer_same_as_importer": "buyer_same_as_importer",
    "buyer_importer_same": "buyer_same_as_importer",
    "buyer_eori": "buyer_eori",
    "buyer_name": "buyer_name",
    "buyer_street_number": "buyer_street_and_number",
    "buyer_street_and_number": "buyer_street_and_number",
    "buyer_address": "buyer_street_and_number",
    "buyer_city": "buyer_city",
    "buyer_postcode": "buyer_postcode",
    "buyer_post_code": "buyer_postcode",
    "buyer_country": "buyer_country",
    # seller party (uses seller_street_and_number column per design)
    "seller_same_as_exporter": "seller_same_as_exporter",
    "seller_exporter_same": "seller_same_as_exporter",
    "seller_eori": "seller_eori",
    "seller_name": "seller_name",
    "seller_street_number": "seller_street_and_number",
    "seller_street_and_number": "seller_street_and_number",
    "seller_address": "seller_street_and_number",
    "seller_city": "seller_city",
    "seller_postcode": "seller_postcode",
    "seller_post_code": "seller_postcode",
    "seller_country": "seller_country",
}


# =============================================================================
# 2. BKD QAS constants + Critical Rule citations (R1: hardcoded - decision Q4)
# =============================================================================

# Birkdale fixed literals applied in the ENRICH stage. EXACT key set is a locked
# contract: arrival_port, transport_charges, goods_domestic_status,
# importer_eori_fallback.
BKD_QAS_CONSTANTS: dict[str, str] = {
    "arrival_port": "GBAUBELBELBEL",          # Rule 12 - BKD fixed port of arrival
    "transport_charges": "Y",                 # Rule 11 - BKD transport charges
    "goods_domestic_status": "D",             # Rule 10 - single-char domestic status
    "importer_eori_fallback": "XI379692092000",  # Rule 13 - importer/consignor fallback
}

# Field / concept -> Critical Rule label, for the RuleApplied column on each DPE
# row. The runner composes the full label, e.g. "QAS:BKD_FIXED_PORT (Rule 12)".
QAS_RULE_CITATIONS: dict[str, str] = {
    "arrival_port": "Rule 12",            # BKD fixed arrival port
    "transport_charges": "Rule 11",       # BKD transport charges = Y
    "goods_domestic_status": "Rule 10",   # BKD domestic status = D
    "importer_eori_fallback": "Rule 13",  # BKD importer EORI fallback
    "arrival_date_time": "Rule 4",        # strict format + bounds
    "choice_value": "Rule 9",             # choice resolution via cache introspection
}


# =============================================================================
# 3. Cardinality / bounds constants
# =============================================================================

MAX_GOODS_PER_CONSIGNMENT: int = 99   # TSS goods-per-consignment limit
ARRIVAL_MAX_FUTURE_DAYS: int = 14     # Rule 4 - arrival must be <= now + 14 days


# =============================================================================
# 4. movement_type vocabulary
# =============================================================================

# Design section 5 movement_type codes.
MOVEMENT_TYPE_LABELS: dict[str, str] = {
    "1a": "Maritime",
    "2a": "Air",
    "3a": "RoRo Accompanied",
    "3b": "RoRo Unaccompanied",
    "4a": "Fixed",
    "5a": "Postal",
    "6a": "Rail",
    "7a": "Road",
}


# =============================================================================
# 5. Validation rule sets (design section 5)
# =============================================================================

# Header fields mandatory for every movement_type.
HEADER_ALWAYS_MANDATORY: list[str] = [
    "movement_type",
    "identity_no_of_transport",
    "nationality_of_transport",
    "arrival_date_time",
    "arrival_port",
    "place_of_loading",
    "place_of_unloading",
    "transport_charges",
    "carrier_eori",
]

# Consignment fields mandatory for every movement_type.
CONSIGNMENT_ALWAYS_MANDATORY: list[str] = [
    "goods_description",
    "transport_document_number",
    "controlled_goods",
    "consignor_eori",
    "consignee_eori",
    "importer_eori",
    "exporter_eori",
]

# Goods (ENS-context) fields mandatory for every goods item.
# number_of_packages range and gross_mass digit/precision limits are enforced
# numerically in the runner; listed here as presence-mandatory.
GOODS_ALWAYS_MANDATORY: list[str] = [
    "type_of_packages",
    "number_of_packages",
    "package_marks",
    "gross_mass_kg",
    "goods_description",
]

# movement_type code -> extra header fields that become mandatory for it.
# "3a" carries the full Rule-3 effectively-mandatory set; place_of_acceptance and
# place_of_delivery are conditionally added by CONDITIONAL_RULES when the
# matching "_same_as_" flag is "no" (i.e. differs from loading / unloading).
MOVEMENT_TYPE_MANDATORY: dict[str, list[str]] = {
    "3a": [  # RoRo Accompanied - Rule 3 effectively-mandatory set
        "type_of_passive_transport",
        "transport_charges",
        "carrier_name",
        "carrier_street_number",
        "carrier_city",
        "carrier_postcode",
        "carrier_country",
        "place_of_acceptance_same_as_loading",
        "place_of_delivery_same_as_unloading",
    ],
}

# Conditional rules: when a field has a given value (in a given scope), require
# the listed fields (in the rule's "scope"). The runner evaluates "when_field"
# in "when_scope" if present, otherwise in "scope".
CONDITIONAL_RULES: list[dict] = [
    {
        "when_field": "movement_type",
        "when_equals": "2a",
        "when_scope": "header",
        "require": ["conveyance_ref"],
        "scope": "header",
        "note": "Air (2a) requires conveyance_ref.",
    },
    {
        "when_field": "controlled_goods",
        "when_equals": "yes",
        "when_scope": "consignment",
        "require": ["goods_domestic_status"],
        "scope": "consignment",
        "note": "controlled_goods=yes requires goods_domestic_status on the consignment.",
    },
    {
        "when_field": "controlled_goods",
        "when_equals": "yes",
        "when_scope": "goods",
        "require": ["commodity_code"],
        "scope": "goods",
        "note": "controlled_goods=yes requires commodity_code on the goods item.",
    },
    {
        "when_field": "preference",
        "when_in": [str(n) for n in range(100, 200)],
        "when_scope": "goods",
        "require": ["country_of_origin"],
        "scope": "goods",
        "note": "preference 100-199 requires country_of_origin (Rule re: preferential origin).",
    },
    {
        "when_field": "use_importer_sde",
        "when_equals": "yes",
        "when_scope": "consignment",
        "require": ["declaration_choice"],
        "scope": "consignment",
        "note": "use_importer_sde=yes requires declaration_choice (+generate_SD when H1/H3/H4).",
    },
    {
        "when_field": "declaration_choice",
        "when_in": ["H2", "H3", "H4"],
        "when_scope": "consignment",
        "require": ["supervising_customs_office"],
        "scope": "consignment",
        "note": "declaration_choice H2/H3/H4 requires supervising_customs_office (+customs_warehouse_identifier when H2).",
    },
    {
        "when_field": "movement_type",
        "when_in": ["1a", "3a", "3b"],
        "when_scope": "header",
        "require": ["container_indicator"],
        "scope": "consignment",
        "note": "Maritime/RoRo (1a/3a/3b) require container_indicator.",
    },
    {
        "when_field": "place_of_acceptance_same_as_loading",
        "when_equals": "no",
        "when_scope": "header",
        "require": ["place_of_acceptance"],
        "scope": "header",
        "note": "Rule 3 (3a): place_of_acceptance required when it differs from place_of_loading.",
    },
    {
        "when_field": "place_of_delivery_same_as_unloading",
        "when_equals": "no",
        "when_scope": "header",
        "require": ["place_of_delivery"],
        "scope": "header",
        "note": "Rule 3 (3a): place_of_delivery required when it differs from place_of_unloading.",
    },
    {
        "when_field": "importer_registered",
        "when_equals": "no",
        "when_scope": "consignment",
        "require": ["no_sfd_reason"],
        "scope": "consignment",
        "note": "Unregistered importer requires no_sfd_reason.",
    },
]


# =============================================================================
# 6. Pure helper functions (no I/O)
# =============================================================================

def normalise_text(value) -> str | None:
    """Trim surrounding whitespace; map None / empty to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalise_code(value) -> str | None:
    """Trim and upper-case (EORI / country / port / currency codes). None if blank."""
    text = normalise_text(value)
    return text.upper() if text is not None else None


# Truthy / falsey token vocabularies for to_yes_no.
_YES_TOKENS = {"y", "yes", "true", "t", "1", "on"}
_NO_TOKENS = {"n", "no", "false", "f", "0", "off"}


def to_yes_no(value) -> str | None:
    """Map truthy -> "yes", falsey -> "no", blank/None -> None.

    Accepts booleans, numbers and common string tokens. Unrecognised non-empty
    text is treated as truthy "yes" (a present, non-blank flag)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return "yes" if value else "no"
    text = str(value).strip()
    if not text:
        return None
    token = text.lower()
    if token in _YES_TOKENS:
        return "yes"
    if token in _NO_TOKENS:
        return "no"
    return "yes"


# Relative-date phrases seen in the ENS source ("Tomorrow's Date / 06:30" etc.).
# Offsets in days from "today" (the date component of now_utc).
_RELATIVE_DAY_OFFSETS = {
    "today": 0,
    "todays date": 0,
    "tonight": 0,
    "tomorrow": 1,
    "tomorrows date": 1,
    "day after tomorrow": 2,
    "overmorrow": 2,
    "yesterday": -1,
    "yesterdays date": -1,
}

# Explicit datetime layouts attempted (in order) for absolute inputs.
_ABS_DATETIME_FORMATS = [
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d.%m.%y %H:%M:%S",
    "%d.%m.%y %H:%M",
    "%d.%m.%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
]

_OUTPUT_FORMAT = "%d/%m/%Y %H:%M:%S"


def _coerce_now(now_utc) -> datetime:
    """Return a timezone-aware UTC 'now'. Accepts an injected datetime for tests."""
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def _strip_ordinal_suffix(text: str) -> str:
    """Drop ordinal suffixes (1st, 2nd, 3rd, 4th) so dates like "1st Jul" parse."""
    return re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)


def parse_arrival_to_utc(raw, now_utc=None):
    """Parse an arrival value to a timezone-aware UTC datetime, or None.

    Handles the relative phrases seen in the Birkdale ENS source (e.g.
    "Tomorrow's Date / 06:30", "Today / 14:00") plus a range of absolute
    DD/MM/YYYY-style layouts and ISO 8601. The "/" between a relative day and a
    time is optional. Returns the datetime form used for Rule-4 bounds checks.
    now_utc may be injected for testability."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _coerce_now(raw)

    text = str(raw).strip()
    if not text:
        return None

    now = _coerce_now(now_utc)

    # --- ISO 8601 (with optional trailing Z) -------------------------------
    iso_candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        return _coerce_now(dt)
    except ValueError:
        pass

    # --- relative-day phrase + optional time -------------------------------
    # Normalise: lower-case, strip apostrophes, collapse separators to spaces.
    lowered = text.lower().replace("'", "").replace("’", "")
    # Pull out an explicit HH:MM(:SS) time if present anywhere.
    time_match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", lowered)
    hh = mm = ss = 0
    if time_match:
        hh = int(time_match.group(1))
        mm = int(time_match.group(2))
        ss = int(time_match.group(3) or 0)
    # The day phrase is everything that is not the time / separators.
    day_part = lowered
    if time_match:
        day_part = (lowered[:time_match.start()] + lowered[time_match.end():])
    day_key = re.sub(r"[^a-z ]+", " ", day_part)
    day_key = re.sub(r"\s+", " ", day_key).strip()
    if day_key in _RELATIVE_DAY_OFFSETS:
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            return None
        base = now + timedelta(days=_RELATIVE_DAY_OFFSETS[day_key])
        return base.replace(hour=hh, minute=mm, second=ss, microsecond=0)

    # --- absolute layouts ---------------------------------------------------
    cleaned = _strip_ordinal_suffix(text).replace("/", "/").strip()
    # Allow a lone "/" separating date and time (e.g. "01/07/2026 / 06:30").
    cleaned = re.sub(r"\s*/\s*(?=\d{1,2}:\d{2})", " ", cleaned)
    for fmt in _ABS_DATETIME_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalise_datetime(raw, now_utc=None) -> str | None:
    """Parse common arrival inputs and return a strict "DD/MM/YYYY HH:MM:SS" UTC
    string, or None if unparseable. Wraps parse_arrival_to_utc."""
    dt = parse_arrival_to_utc(raw, now_utc=now_utc)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime(_OUTPUT_FORMAT)


# =============================================================================
# Smoke test
# =============================================================================

if __name__ == "__main__":
    print(
        "mapping.py: "
        f"ENS_CSV_TO_HEADER={len(ENS_CSV_TO_HEADER)} "
        f"SALES_ORDER_TO_GOODS={len(SALES_ORDER_TO_GOODS)} "
        f"SALES_ORDER_TO_CONSIGNMENT={len(SALES_ORDER_TO_CONSIGNMENT)} "
        f"BKD_QAS_CONSTANTS={len(BKD_QAS_CONSTANTS)} "
        f"QAS_RULE_CITATIONS={len(QAS_RULE_CITATIONS)} "
        f"HEADER_ALWAYS_MANDATORY={len(HEADER_ALWAYS_MANDATORY)} "
        f"CONSIGNMENT_ALWAYS_MANDATORY={len(CONSIGNMENT_ALWAYS_MANDATORY)} "
        f"GOODS_ALWAYS_MANDATORY={len(GOODS_ALWAYS_MANDATORY)} "
        f"MOVEMENT_TYPE_MANDATORY={len(MOVEMENT_TYPE_MANDATORY)} "
        f"CONDITIONAL_RULES={len(CONDITIONAL_RULES)} "
        f"MOVEMENT_TYPE_LABELS={len(MOVEMENT_TYPE_LABELS)} "
        f"MAX_GOODS_PER_CONSIGNMENT={MAX_GOODS_PER_CONSIGNMENT} "
        f"ARRIVAL_MAX_FUTURE_DAYS={ARRIVAL_MAX_FUTURE_DAYS}"
    )
