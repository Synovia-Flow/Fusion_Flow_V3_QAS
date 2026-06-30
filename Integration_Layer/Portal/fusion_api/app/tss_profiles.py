from __future__ import annotations

from copy import deepcopy

# Portal bridge only: these are UI/client aliases from the screenshot, not DB tables.
# Data ownership and TSS credential state are verified from CFG.Clients and
# CFG.TSS_Credential at request time.
FALLBACK_PORTAL_PROFILES: dict[str, dict[str, object]] = {
    "PLE": {
        "portalClientCode": "PLE",
        "clientCode": "PLE",
        "clientName": "Primeline Express",
        "tssCredentialClientCode": "PLE",
        "preferredEnvCode": "PRD",
        "requiresEnsBeforeSubmit": True,
        "fileSelection": {
            "requiredFileOrdinal": 1,
            "acceptedExtensions": ".xlsx,.xls,.csv",
            "targetLandingTable": "ING.Inbound_File / ING.Raw_Record",
            "targetCanonicalRoot": "PRS.Consignment / PRS.Goods_Item",
            "notes": "Primeline maps the first attached file.",
        },
    },
    "CW": {
        "portalClientCode": "CW",
        "clientCode": "CWD",
        "clientName": "CountryWide",
        "tssCredentialClientCode": "CWF",
        "preferredEnvCode": "TST",
        "requiresEnsBeforeSubmit": True,
        "fileSelection": {
            "requiredFileOrdinal": 2,
            "acceptedExtensions": ".xlsx,.xls,.csv",
            "targetLandingTable": "ING.Inbound_File / ING.Raw_Record",
            "targetCanonicalRoot": "PRS.Consignment / PRS.Goods_Item",
            "notes": "Countrywide maps the second attached file.",
        },
    },
}

ALIASES = {
    "PRIMELINE": "PLE",
    "PRIMELINE EXPRESS": "PLE",
    "PLE": "PLE",
    "CW": "CW",
    "CWD": "CW",
    "CWF": "CW",
    "CWH": "CW",
    "COUNTRYWIDE": "CW",
    "COUNTRY WIDE": "CW",
}


def normalize_portal_code(value: str) -> str:
    clean = " ".join((value or "").strip().upper().split())
    return ALIASES.get(clean, clean)


def fallback_profile(value: str) -> dict[str, object] | None:
    code = normalize_portal_code(value)
    profile = FALLBACK_PORTAL_PROFILES.get(code)
    return deepcopy(profile) if profile else None


def required_file_ordinal(profile: dict[str, object]) -> int:
    raw = (profile.get("fileSelection") or {}).get("requiredFileOrdinal")
    try:
        ordinal = int(raw or 1)
    except (TypeError, ValueError):
        ordinal = 1
    return max(1, ordinal)


def required_file_index(profile: dict[str, object]) -> int:
    return required_file_ordinal(profile) - 1


def select_required_file(files: list[object], profile: dict[str, object]) -> object:
    ordinal = required_file_ordinal(profile)
    if len(files) < ordinal:
        code = profile.get("portalClientCode") or profile.get("clientCode") or "Client"
        raise ValueError(f"{code} requires attached file #{ordinal}; only {len(files)} file(s) were provided.")
    return files[required_file_index(profile)]

def fallback_profiles() -> list[dict[str, object]]:
    return [deepcopy(profile) for profile in FALLBACK_PORTAL_PROFILES.values()]
