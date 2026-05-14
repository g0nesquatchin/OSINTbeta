"""Curated country list for the live-stream region picker.

For each country we keep:
  - name        : human-readable label
  - gdelt       : GDELT's FIPS-style country code (for sourcecountry:XX)
  - gnews_gl    : Google News `gl` country parameter
  - gnews_hl    : Google News `hl` language parameter (default)

This is intentionally a curated short list of the most-asked-for
regions, not every country on earth. Add to it freely.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Country:
    code: str        # short id we use everywhere (uppercase, 2-3 chars)
    name: str        # human label
    gdelt: str       # GDELT FIPS code
    gnews_gl: str    # Google News country code
    gnews_hl: str    # Google News language code
    region: str = "" # for grouping


COUNTRIES: list[Country] = [
    # --- North America -------------------------------------------------
    Country("US", "United States",  "US", "US", "en-US", "North America"),
    Country("CA", "Canada",         "CA", "CA", "en-CA", "North America"),
    Country("MX", "Mexico",         "MX", "MX", "es-MX", "North America"),
    # --- Europe --------------------------------------------------------
    Country("GB", "United Kingdom", "UK", "GB", "en-GB", "Europe"),
    Country("IE", "Ireland",        "EI", "IE", "en-IE", "Europe"),
    Country("DE", "Germany",        "GM", "DE", "de",    "Europe"),
    Country("FR", "France",         "FR", "FR", "fr",    "Europe"),
    Country("ES", "Spain",          "SP", "ES", "es",    "Europe"),
    Country("IT", "Italy",          "IT", "IT", "it",    "Europe"),
    Country("NL", "Netherlands",    "NL", "NL", "nl",    "Europe"),
    Country("SE", "Sweden",         "SW", "SE", "sv",    "Europe"),
    Country("NO", "Norway",         "NO", "NO", "no",    "Europe"),
    Country("PL", "Poland",         "PL", "PL", "pl",    "Europe"),
    Country("UA", "Ukraine",        "UP", "UA", "uk",    "Europe"),
    Country("RU", "Russia",         "RS", "RU", "ru",    "Europe"),
    Country("TR", "Turkey",         "TU", "TR", "tr",    "Europe"),
    # --- Middle East ---------------------------------------------------
    Country("IL", "Israel",         "IS", "IL", "en",    "Middle East"),
    Country("SA", "Saudi Arabia",   "SA", "SA", "ar",    "Middle East"),
    Country("AE", "UAE",            "AE", "AE", "en",    "Middle East"),
    Country("IR", "Iran",           "IR", "US", "en",    "Middle East"),  # gnews fallback
    # --- Asia ----------------------------------------------------------
    Country("CN", "China",          "CH", "CN", "zh-CN", "Asia"),
    Country("HK", "Hong Kong",      "HK", "HK", "zh-HK", "Asia"),
    Country("TW", "Taiwan",         "TW", "TW", "zh-TW", "Asia"),
    Country("JP", "Japan",          "JA", "JP", "ja",    "Asia"),
    Country("KR", "South Korea",    "KS", "KR", "ko",    "Asia"),
    Country("IN", "India",          "IN", "IN", "en-IN", "Asia"),
    Country("PK", "Pakistan",       "PK", "PK", "en",    "Asia"),
    Country("BD", "Bangladesh",     "BG", "BD", "en",    "Asia"),
    Country("ID", "Indonesia",      "ID", "ID", "id",    "Asia"),
    Country("PH", "Philippines",    "RP", "PH", "en-PH", "Asia"),
    Country("VN", "Vietnam",        "VM", "VN", "vi",    "Asia"),
    Country("TH", "Thailand",       "TH", "TH", "th",    "Asia"),
    # --- Latin America -------------------------------------------------
    Country("BR", "Brazil",         "BR", "BR", "pt-BR", "Latin America"),
    Country("AR", "Argentina",      "AR", "AR", "es-AR", "Latin America"),
    Country("CL", "Chile",          "CI", "CL", "es-CL", "Latin America"),
    Country("CO", "Colombia",       "CO", "CO", "es-CO", "Latin America"),
    Country("PE", "Peru",           "PE", "PE", "es-PE", "Latin America"),
    Country("VE", "Venezuela",      "VE", "VE", "es",    "Latin America"),
    # --- Africa --------------------------------------------------------
    Country("ZA", "South Africa",   "SF", "ZA", "en",    "Africa"),
    Country("NG", "Nigeria",        "NI", "NG", "en-NG", "Africa"),
    Country("KE", "Kenya",          "KE", "KE", "en-KE", "Africa"),
    Country("EG", "Egypt",          "EG", "EG", "ar",    "Africa"),
    Country("MA", "Morocco",        "MO", "MA", "ar",    "Africa"),
    Country("ET", "Ethiopia",       "ET", "ET", "en",    "Africa"),
    # --- Oceania -------------------------------------------------------
    Country("AU", "Australia",      "AS", "AU", "en-AU", "Oceania"),
    Country("NZ", "New Zealand",    "NZ", "NZ", "en-NZ", "Oceania"),
]


_BY_CODE = {c.code: c for c in COUNTRIES}


def get(code: str) -> Country | None:
    return _BY_CODE.get(code.upper())


def by_region() -> dict[str, list[Country]]:
    out: dict[str, list[Country]] = {}
    for c in COUNTRIES:
        out.setdefault(c.region or "Other", []).append(c)
    return out
