"""Download and parse GDELT GKG 2.0 files.

Files live at https://data.gdeltproject.org/gdeltv2/. The current
"latest" pointer is at https://data.gdeltproject.org/gdeltv2/lastupdate.txt
which contains three URLs (export, mentions, gkg) — we want the GKG.

Each GKG file is a zip of a TSV (despite the .csv extension) with
27 columns. The schema is documented at
https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/

For our purposes we extract:
  col  1 (GKGRECORDID)
  col  2 (DATE) — YYYYMMDDHHMMSS
  col  4 (SourceCommonName) — the source domain
  col  5 (DocumentIdentifier) — the article URL
  col  9 (V2Themes) — "theme,offset" pairs, pipe-separated
  col 11 (V2Locations) — "type#name#country#adm1#lat#lon#feature_id,offset"
  col 16 (V2Tone) — comma-separated; first value is average tone

We discard the rest.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from typing import Iterable, Optional

import requests


LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
USER_AGENT = "osint-monitor/0.1 (research)"
DEFAULT_TIMEOUT = 60.0


@dataclass
class GkgArticle:
    url: str
    source: str
    date_str: str
    themes: list[str]
    tone: float
    locations: list["GkgLocation"]


@dataclass
class GkgLocation:
    loc_type: int       # 1=Country 2=US State 3=US City 4=World City 5=World State
    name: str
    country_code: str   # FIPS country code
    admin1_code: str    # state/province code
    lat: float
    lon: float
    feature_id: str = ""


def _latest_gkg_url(verify: bool = True) -> str:
    """Read lastupdate.txt and return the URL of the most recent GKG file.

    `verify` is forwarded to requests for the SSL cert check. GDELT's
    data subdomain has had hostname-mismatch SSL cert issues from time
    to time; pass ``verify=False`` to bypass when that's biting.
    """
    r = requests.get(
        LASTUPDATE_URL, timeout=15, headers={"User-Agent": USER_AGENT},
        verify=verify,
    )
    r.raise_for_status()
    for line in r.text.splitlines():
        parts = line.split()
        # Format: "<size> <md5> <url>"
        if len(parts) >= 3 and parts[2].endswith(".gkg.csv.zip"):
            return parts[2]
    raise RuntimeError("Could not find GKG URL in lastupdate.txt")


def fetch_latest(
    timeout: float = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> Iterable[GkgArticle]:
    """Fetch and parse the most recent GKG file."""
    url = _latest_gkg_url(verify=verify)
    yield from fetch_url(url, timeout=timeout, verify=verify)


def fetch_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    verify: bool = True,
) -> Iterable[GkgArticle]:
    """Download and parse a specific GKG file URL."""
    r = requests.get(
        url, timeout=timeout, headers={"User-Agent": USER_AGENT},
        verify=verify,
    )
    r.raise_for_status()
    yield from _parse_zip_bytes(r.content)


def parse_file(path: str) -> Iterable[GkgArticle]:
    """Parse a GKG file already on disk."""
    with open(path, "rb") as f:
        yield from _parse_zip_bytes(f.read())


def _parse_zip_bytes(data: bytes) -> Iterable[GkgArticle]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # GKG zips contain one .csv (actually TSV) file
        names = [n for n in zf.namelist() if n.endswith(".csv") or n.endswith(".CSV")]
        if not names:
            return
        with zf.open(names[0]) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            reader = csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in reader:
                rec = _row_to_article(row)
                if rec is not None:
                    yield rec


def _row_to_article(row: list[str]) -> Optional[GkgArticle]:
    if len(row) < 16:
        return None
    try:
        date_str = row[1]
        source = row[3]
        url = row[4]
        themes_field = row[8]
        locations_field = row[10]
        tone_field = row[15]
    except IndexError:
        return None
    if not url:
        return None

    themes = _parse_themes(themes_field)
    locations = list(_parse_locations(locations_field))
    if not locations:
        # An article with no tagged locations is uninteresting for a map
        return None

    tone = 0.0
    if tone_field:
        try:
            tone = float(tone_field.split(",", 1)[0])
        except (ValueError, IndexError):
            tone = 0.0

    return GkgArticle(
        url=url, source=source, date_str=date_str,
        themes=themes, tone=tone, locations=locations,
    )


def _parse_themes(field: str) -> list[str]:
    """V2Themes: pipe-separated 'theme,offset'. Return de-duped theme tags."""
    if not field:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in field.split(";"):
        if not chunk:
            continue
        theme = chunk.split(",", 1)[0].strip()
        if theme and theme not in seen:
            seen.add(theme)
            out.append(theme)
    return out


def _parse_locations(field: str) -> Iterable[GkgLocation]:
    """V2Locations: semicolon-separated entries.

    Each entry is '#'-separated fields with an optional trailing
    ',<charoffset>'. The location name itself often contains commas
    (e.g. 'Dallas, Texas, United States'), so we only strip the
    trailing chunk if it's purely digits.

    Two field layouts exist in the wild:
      7 fields  type#name#cc#adm1#lat#lon#fid
      8 fields  type#name#cc#adm1#adm2#lat#lon#fid
    We pick lat/lon positions based on field count.
    """
    if not field:
        return
    for entry in field.split(";"):
        if not entry:
            continue
        entry = _strip_trailing_offset(entry)
        parts = entry.split("#")
        if len(parts) < 6:
            continue

        if len(parts) >= 8:
            lat_idx, lon_idx, adm1_idx, fid_idx = 5, 6, 3, 7
        else:
            lat_idx, lon_idx, adm1_idx, fid_idx = 4, 5, 3, 6

        try:
            lat = float(parts[lat_idx])
            lon = float(parts[lon_idx])
        except (ValueError, IndexError):
            continue
        if lat == 0.0 and lon == 0.0:
            # GKG uses 0,0 as "unknown" — skip
            continue
        try:
            loc_type = int(parts[0]) if parts[0] else 0
        except ValueError:
            loc_type = 0
        yield GkgLocation(
            loc_type=loc_type,
            name=parts[1] or "",
            country_code=parts[2] or "",
            admin1_code=parts[adm1_idx] if adm1_idx < len(parts) else "",
            lat=lat,
            lon=lon,
            feature_id=parts[fid_idx] if fid_idx < len(parts) else "",
        )


def _strip_trailing_offset(entry: str) -> str:
    """Remove a trailing ',<digits>' offset, preserving commas inside fields."""
    if "," not in entry:
        return entry
    head, _, tail = entry.rpartition(",")
    return head if tail.strip().isdigit() else entry
