"""GDELT GKG 2.0 integration for town-level mapping.

GDELT's Global Knowledge Graph (GKG) publishes a new file every 15
minutes at https://data.gdeltproject.org/gdeltv2/. Each file contains
the articles GDELT has processed in the previous 15 minutes, tagged
with the locations they mention (with lat/lon), themes, people, and
organizations.

We fetch these files periodically, parse them into a local SQLite
store, and query against them to power the world map. The free
public APIs don't expose this data with lat/lon — getting town-level
precision means doing the parsing ourselves.

Pipeline:
  fetcher  -> downloads + parses one .gkg.csv.zip file
  storage  -> SQLite tables for articles + locations
  worker   -> background thread, runs every 15 min, prunes old data
  search   -> keyword query against the stored data
"""

from .storage import GkgStore
from .fetcher import fetch_latest, fetch_url, parse_file

__all__ = ["GkgStore", "fetch_latest", "fetch_url", "parse_file"]
