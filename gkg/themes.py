"""Curated GDELT GKG theme picks for the map filter UI.

GDELT's full theme list runs to several thousand codes
(http://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT).
For an OSINT investigation workflow we only need a handful of grouped
quick-picks — users with deeper needs can paste raw theme codes into
the free-text input.

Each group is a (label, list-of-theme-codes) tuple. Selecting a group
in the UI OR's the codes together; selecting multiple groups OR's
across all of them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThemeGroup:
    id: str          # short stable key, used in URLs/forms
    label: str       # human label
    codes: list[str]  # GDELT theme codes
    description: str = ""  # one-line description for the UI


THEME_GROUPS: list[ThemeGroup] = [
    ThemeGroup(
        id="missing",
        label="Missing persons",
        codes=[
            "MISSING_PERSON",
            "WB_2473_HUMAN_TRAFFICKING_CHILD_TRAFFICKING",  # adjacent signal
            "KIDNAP",
            "WB_2462_CHILD_PROTECTION",
        ],
        description="People reported missing, kidnapped, or abducted.",
    ),
    ThemeGroup(
        id="trafficking",
        label="Human trafficking",
        codes=[
            "HUMAN_TRAFFICKING",
            "WB_2473_HUMAN_TRAFFICKING_CHILD_TRAFFICKING",
            "WB_2474_HUMAN_TRAFFICKING_LABOR_TRAFFICKING",
            "WB_2475_HUMAN_TRAFFICKING_TRAFFICKING_FOR_FORCED_MARRIAGE",
            "WB_2476_HUMAN_TRAFFICKING_TRAFFICKING_FOR_ORGAN_REMOVAL",
            "WB_2477_HUMAN_TRAFFICKING_SEX_TRAFFICKING",
            "TRAFFICKING",
            "SMUGGLING",
        ],
        description="Trafficking-related coverage across types.",
    ),
    ThemeGroup(
        id="violence",
        label="Violence & conflict",
        codes=[
            "KILL",
            "WOUND",
            "ASSAULT",
            "ARMEDCONFLICT",
            "TERROR",
            "TERRORISM",
            "VIOLENCE",
            "MIL_WEAPONS",
        ],
        description="Killings, woundings, armed conflict, terror events.",
    ),
    ThemeGroup(
        id="crime",
        label="Crime",
        codes=[
            "CRIME",
            "GENERAL_CRIME",
            "WB_2202_GENERAL_CRIME",
            "ORGANIZED_CRIME",
            "DRUG_TRADE",
            "DRUGS",
            "CORRUPTION",
        ],
        description="General crime, organized crime, drugs, corruption.",
    ),
    ThemeGroup(
        id="unrest",
        label="Civil unrest",
        codes=[
            "PROTEST",
            "RIOT",
            "STRIKE",
            "REBELLION",
            "CIVIL_UNREST",
        ],
        description="Protests, riots, strikes, rebellions.",
    ),
    ThemeGroup(
        id="migration",
        label="Migration & displacement",
        codes=[
            "REFUGEES",
            "IMMIGRATION",
            "MIGRATION",
            "DISPLACED",
            "ASYLUM",
            "BORDER",
        ],
        description="Refugees, migration flows, displacement, borders.",
    ),
    ThemeGroup(
        id="health",
        label="Health emergencies",
        codes=[
            "INFECTIOUS_DISEASE",
            "EPIDEMIC",
            "PANDEMIC",
            "HEALTH_PANDEMIC",
            "OUTBREAK",
        ],
        description="Outbreaks, epidemics, public-health emergencies.",
    ),
    ThemeGroup(
        id="disaster",
        label="Natural disasters",
        codes=[
            "NATURAL_DISASTER",
            "NATURAL_DISASTER_FLOOD",
            "NATURAL_DISASTER_EARTHQUAKE",
            "NATURAL_DISASTER_HURRICANE",
            "NATURAL_DISASTER_WILDFIRE",
            "CRISISLEX_CRISISLEXREC",
        ],
        description="Floods, earthquakes, fires, storms, generic crisis tags.",
    ),
]


_BY_ID = {g.id: g for g in THEME_GROUPS}


def get_group(group_id: str) -> ThemeGroup | None:
    return _BY_ID.get(group_id)


def codes_for_groups(group_ids: list[str]) -> list[str]:
    """Flatten a list of group ids into their underlying theme codes,
    de-duplicated, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for gid in group_ids:
        g = _BY_ID.get(gid)
        if not g:
            continue
        for c in g.codes:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out
