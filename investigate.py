"""Single-target investigation helpers.

Currently wires up Sherlock for username lookups across a few hundred
public platforms. Sherlock works by hitting each platform's public
profile URL for the given username and reporting which return a "found"
response. It only sees what an anonymous visitor would see.

We shell out to the `sherlock` CLI rather than using the Python API
directly so version drift doesn't break us. Install with:

    pip install sherlock-project

If the binary isn't on PATH, the route surfaces a friendly error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


SHERLOCK_TIMEOUT_S = 90


@dataclass
class SherlockHit:
    site: str
    url: str


@dataclass
class SherlockResult:
    username: str
    hits: list[SherlockHit]
    raw_count: int
    error: str | None = None


_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def _which() -> str | None:
    return shutil.which("sherlock")


def sherlock_available() -> bool:
    return _which() is not None


def lookup_username(username: str) -> SherlockResult:
    """Run Sherlock against `username` and parse the results."""
    username = (username or "").strip()
    if not username:
        return SherlockResult(username, [], 0, "Empty username.")
    if not _USERNAME_RE.match(username):
        return SherlockResult(
            username, [], 0,
            "Username must be 1-40 chars, letters/digits/._- only.",
        )
    if not sherlock_available():
        return SherlockResult(
            username, [], 0,
            "Sherlock isn't installed. Run: pip install sherlock-project",
        )

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = tmp
        # Sherlock writes <username>.txt with lines like:
        #   [+] SiteName: https://...
        try:
            subprocess.run(
                [
                    "sherlock", username,
                    "--folderoutput", out_dir,
                    "--print-found",
                    "--timeout", "10",
                ],
                check=False,
                timeout=SHERLOCK_TIMEOUT_S,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return SherlockResult(
                username, [], 0,
                f"Sherlock timed out after {SHERLOCK_TIMEOUT_S}s.",
            )
        except Exception as e:  # pragma: no cover
            return SherlockResult(username, [], 0, f"Sherlock failed: {e}")

        out_file = os.path.join(out_dir, f"{username}.txt")
        if not os.path.exists(out_file):
            return SherlockResult(
                username, [], 0,
                "Sherlock produced no output file. Is the username valid?",
            )

        hits: list[SherlockHit] = []
        with open(out_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                # New format: "Site: https://..."
                # Old format: "[+] Site: https://..."
                if not line or line.startswith("Total"):
                    continue
                m = re.match(r"^(?:\[\+\]\s*)?([^:]+):\s+(https?://\S+)$", line)
                if m:
                    hits.append(SherlockHit(site=m.group(1).strip(),
                                            url=m.group(2).strip()))
        return SherlockResult(username, hits, len(hits))


def export_result_json(result: SherlockResult) -> str:
    return json.dumps(
        {
            "username": result.username,
            "count": result.raw_count,
            "hits": [{"site": h.site, "url": h.url} for h in result.hits],
            "error": result.error,
        },
        indent=2,
    )
