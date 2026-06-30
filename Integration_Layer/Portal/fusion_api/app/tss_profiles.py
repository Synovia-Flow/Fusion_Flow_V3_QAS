from __future__ import annotations

from copy import deepcopy

FALLBACK_PORTAL_PROFILES: dict[str, dict[str, object]] = {
    "PLE": {
        "portalClientCode": "PLE",
        "clientCode": "PLE",
        "clientName": "Primeline Express",
        "tssCredentialClientCode": "PLE",
        "preferredEnvCode": "PRD",
        "uploadProfileCode": "PLE_PRIMELINE_CONSIGNMENT_UPLOAD",
        "requiresEnsBeforeSubmit": True,
        "fileProfile": {
            "profileCode": "PLE_PRIMELINE_CONSIGNMENT_UPLOAD",
            "fileRole": "CONSIGNMENT_UPLOAD",
            "requiredFileOrdinal": 1,
            "fileDisplayName": "Primeline mandatory consignment file",
            "acceptedExtensions": ".xlsx,.xls,.csv",
            "targetLandingTable": "ING.Inbound_File / ING.Raw_Record",
            "targetCanonicalRoot": "PRS.Consignment / PRS.Goods_Item",
            "mappingStatus": "AWAITING_SAMPLE_COLUMNS",
            "notes": "Map only the first attached file for Primeline.",
        },
    },
    "CW": {
        "portalClientCode": "CW",
        "clientCode": "CWD",
        "clientName": "CountryWide",
        "tssCredentialClientCode": "CWF",
        "preferredEnvCode": "TST",
        "uploadProfileCode": "CW_COUNTRYWIDE_CONSIGNMENT_UPLOAD",
        "requiresEnsBeforeSubmit": True,
        "fileProfile": {
            "profileCode": "CW_COUNTRYWIDE_CONSIGNMENT_UPLOAD",
            "fileRole": "CONSIGNMENT_UPLOAD",
            "requiredFileOrdinal": 2,
            "fileDisplayName": "Countrywide mandatory consignment file",
            "acceptedExtensions": ".xlsx,.xls,.csv",
            "targetLandingTable": "ING.Inbound_File / ING.Raw_Record",
            "targetCanonicalRoot": "PRS.Consignment / PRS.Goods_Item",
            "mappingStatus": "AWAITING_SAMPLE_COLUMNS",
            "notes": "Map only the second attached file for Countrywide.",
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


def fallback_profiles() -> list[dict[str, object]]:
    return [deepcopy(profile) for profile in FALLBACK_PORTAL_PROFILES.values()]
