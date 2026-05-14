"""HTTP client for a running SpiderFoot server.

SpiderFoot exposes a set of CherryPy endpoints that its own web UI uses.
They aren't a formal REST API but they're stable enough across recent
versions to wrap. All responses we care about are JSON.

Reference: https://github.com/smicallef/spiderfoot
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import requests


# SpiderFoot's scan status strings
RUNNING_STATES = {"CREATED", "STARTING", "STARTED", "RUNNING"}
FINISHED_STATES = {"FINISHED", "ABORTED", "ERROR-FAILED", "ABORT-REQUESTED"}


@dataclass
class Scan:
    id: str
    name: str
    target: str
    started: str
    ended: str
    status: str
    elements: int  # total result count

    @property
    def is_running(self) -> bool:
        return self.status in RUNNING_STATES

    @classmethod
    def from_row(cls, row: list) -> "Scan":
        # /scanlist returns rows shaped like:
        # [id, name, target, started_unix, ended_unix, ?, status, element_count]
        # Different SpiderFoot versions vary slightly in column order; we
        # access by index defensively.
        def _g(i: int, default=""):
            try:
                return row[i]
            except (IndexError, TypeError):
                return default

        return cls(
            id=str(_g(0)),
            name=str(_g(1)),
            target=str(_g(2)),
            started=str(_g(3)),
            ended=str(_g(4)),
            status=str(_g(6) or _g(5)),
            elements=int(_g(7) or 0) if str(_g(7)).isdigit() else 0,
        )


@dataclass
class ScanEvent:
    type: str
    data: str
    source_module: str
    source_data: str
    generated: str
    risk: str = ""


class SpiderFootError(RuntimeError):
    pass


class SpiderFootClient:
    """Thin wrapper around SpiderFoot's HTTP endpoints."""

    def __init__(self, base_url: str = "http://127.0.0.1:5001",
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    # --- helpers ------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.get(url, params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise SpiderFootError(f"GET {path} -> {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except json.JSONDecodeError:
            return r.text

    def _post(self, path: str, data: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.post(url, data=data, timeout=self.timeout)
        if r.status_code >= 400:
            raise SpiderFootError(f"POST {path} -> {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except json.JSONDecodeError:
            return r.text

    # --- health -------------------------------------------------

    def ping(self) -> bool:
        try:
            r = self._session.get(self.base_url + "/ping", timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            # Fallback: try the root
            try:
                r = self._session.get(self.base_url, timeout=3)
                return r.status_code < 500
            except requests.RequestException:
                return False

    # --- scans --------------------------------------------------

    def list_scans(self) -> list[Scan]:
        rows = self._get("/scanlist") or []
        return [Scan.from_row(r) for r in rows if isinstance(r, list)]

    def scan_info(self, scan_id: str) -> dict:
        return self._get("/scanopts", params={"id": scan_id}) or {}

    def scan_status(self, scan_id: str) -> dict:
        """Returns {created, started, ended, status, ...} (legacy shape)."""
        rows = self._get("/scanlist") or []
        for row in rows:
            if isinstance(row, list) and str(row[0]) == str(scan_id):
                s = Scan.from_row(row)
                return {
                    "id": s.id, "status": s.status, "started": s.started,
                    "ended": s.ended, "elements": s.elements,
                    "is_running": s.is_running,
                }
        raise SpiderFootError(f"Scan {scan_id!r} not found")

    def start_scan(
        self, name: str, target: str, target_type: str,
        usecase: str = "All",
        modules: Optional[list[str]] = None,
        types: Optional[list[str]] = None,
    ) -> str:
        """Kick off a new scan. Returns the scan id.

        SpiderFoot's /startscan endpoint behaves very differently across
        versions:
          - v4.x serves an HTML error page (with an alert div) on bad
            input, and redirects to /scaninfo?id=ID on success.
          - Newer builds return a JSON tuple ["SUCCESS", id] or
            ["ERROR", message].
        We handle both shapes.
        """
        data = {
            "scanname": name,
            "scantarget": target,
            "typelist": ",".join(types) if types else "",
            "modulelist": ",".join(modules) if modules else "",
            "usecase": usecase,
        }
        url = f"{self.base_url}/startscan"
        r = self._session.post(
            url, data=data, timeout=self.timeout, allow_redirects=False,
        )

        # 1. Success-via-redirect (v4): Location header points to /scaninfo?id=...
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            m = re.search(r"[?&]id=([A-Za-z0-9]+)", loc)
            if m:
                return m.group(1)
            raise SpiderFootError(
                f"SpiderFoot accepted the request but the Location "
                f"header was unexpected: {loc!r}"
            )

        # 2. HTML error page (v4 returns 200 with an alert div on bad input)
        ctype = r.headers.get("content-type", "").lower()
        text = r.text or ""
        if "text/html" in ctype or text.lstrip().lower().startswith("<!doctype") \
                or text.lstrip().lower().startswith("<html"):
            err = _extract_alert_error(text)
            raise SpiderFootError(err or "SpiderFoot returned an HTML error page.")

        # 3. JSON tuple shape (newer SpiderFoot)
        if r.status_code >= 400:
            raise SpiderFootError(f"POST /startscan -> {r.status_code} {text[:200]}")
        try:
            result = r.json()
        except (ValueError, json.JSONDecodeError):
            raise SpiderFootError(
                f"Unexpected non-JSON response from /startscan: {text[:200]}"
            )
        if isinstance(result, list) and len(result) >= 2:
            if str(result[0]).upper().startswith("ERROR"):
                raise SpiderFootError(str(result[1]))
            return str(result[1])
        if isinstance(result, str) and result:
            return result
        raise SpiderFootError(f"Unexpected response from /startscan: {result!r}")

    def stop_scan(self, scan_id: str) -> None:
        self._get("/stopscan", params={"id": scan_id})

    def delete_scan(self, scan_id: str) -> None:
        self._get("/scandelete", params={"id": scan_id, "confirm": "1"})

    # --- results ------------------------------------------------

    def scan_event_summary(self, scan_id: str) -> list[dict]:
        """Counts per event type for one scan."""
        rows = self._get("/scaneventresultsummary",
                         params={"id": scan_id, "by": "type"}) or []
        out = []
        for r in rows:
            # rows shaped like [event_type_name, total_count, ...]
            if not isinstance(r, list) or len(r) < 2:
                continue
            out.append({
                "type": r[0],
                "count": int(r[1]) if str(r[1]).isdigit() else 0,
            })
        return sorted(out, key=lambda x: -x["count"])

    def scan_events(self, scan_id: str, event_type: str = "ALL",
                    limit: int = 1000) -> list[ScanEvent]:
        rows = self._get("/scaneventresults",
                         params={"id": scan_id, "eventType": event_type}) or []
        out = []
        for r in rows[:limit]:
            if not isinstance(r, list) or len(r) < 4:
                continue
            # rows shaped like [generated, data, source_data, source_module,
            #                   event_type, ?, ?, risk, ...]
            out.append(ScanEvent(
                generated=str(r[0]),
                data=str(r[1]),
                source_data=str(r[2]) if len(r) > 2 else "",
                source_module=str(r[3]) if len(r) > 3 else "",
                type=str(r[4]) if len(r) > 4 else event_type,
                risk=str(r[7]) if len(r) > 7 else "",
            ))
        return out

    # --- modules & types ---------------------------------------

    def modules(self) -> list[dict]:
        """List all SpiderFoot modules."""
        return self._get("/modules") or []

    def event_types(self) -> list[dict]:
        return self._get("/eventtypes") or []


# --- helpers -------------------------------------------------------


_ALERT_RE = re.compile(
    r'<div[^>]*class="[^"]*alert[^"]*alert-danger[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _extract_alert_error(html: str) -> Optional[str]:
    """Pull the error text out of a SpiderFoot HTML error page."""
    if not html:
        return None
    m = _ALERT_RE.search(html)
    if not m:
        return None
    body = _TAG_RE.sub(" ", m.group(1))
    body = _WS_RE.sub(" ", body).strip()
    return body or None
