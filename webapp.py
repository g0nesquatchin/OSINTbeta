"""OSINT investigation app, built around SpiderFoot.

The primary workflow is: pick a target (domain, email, IP, person,
username, etc.), choose a use-case preset, start a scan, watch results
come in, drill into events by type.

Sherlock and the Resources launchpad live alongside as auxiliary tools.

Run with:
    python webapp.py
Then open http://localhost:5000 in your browser.
"""

from __future__ import annotations

import os
from typing import Optional

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, url_for,
)

from investigate import lookup_username, sherlock_available
from spiderfoot_client import (
    SpiderFootClient, SpiderFootError, FINISHED_STATES, RUNNING_STATES,
)
from spiderfoot_manager import manager, setup_instructions

from monitor import SOURCE_NAMES as MONITOR_SOURCES
from monitor.countries import COUNTRIES, by_region
from monitor.geo_api import GeoApiError, build_query as geo_build_query, fetch_geo
from monitor.map_data import articles_for_country, country_counts
from monitor.runner import MonitorRunner
from monitor.scheduler import Scheduler
from monitor.storage import MonitorStore


APP_ROOT = os.path.dirname(os.path.abspath(__file__))


# --- Monitor singletons -------------------------------------------


MONITOR_DB = os.environ.get(
    "MONITOR_DB", os.path.join(APP_ROOT, "monitor.db")
)
monitor_store = MonitorStore(MONITOR_DB)
monitor_store.bootstrap_defaults()
monitor_runner = MonitorRunner(monitor_store)
monitor_scheduler = Scheduler(monitor_store, monitor_runner)


MONITOR_SOURCE_LABELS = {
    "gdelt": "GDELT (global news)",
    "google_news": "Google News",
    "rss": "RSS / Atom feeds",
    "reddit": "Reddit",
    "bluesky": "Bluesky",
    "mastodon": "Mastodon",
    "x_twitter": "X / Twitter",
}


app = Flask(__name__)
app.secret_key = os.environ.get("OSINT_SECRET", "local-dev-key")


# --- SpiderFoot helpers --------------------------------------------


def _client() -> SpiderFootClient:
    return SpiderFootClient(base_url=manager.state.base_url)


def _require_spiderfoot():
    """Returns None if reachable, else a Flask response that redirects
    to the setup page."""
    if manager.is_reachable():
        return None
    if not manager.state.installed:
        return redirect(url_for("setup"))
    ok, msg = manager.ensure_started()
    if not ok:
        flash(f"SpiderFoot didn't start: {msg}", "error")
        return redirect(url_for("setup"))
    return None


# --- Use-case presets ---------------------------------------------


# Maps user-friendly target shapes to SpiderFoot target_type and the
# default use-case preset that fits.
TARGET_PRESETS = [
    {
        "id": "domain",
        "label": "Domain or website",
        "placeholder": "example.com",
        "target_type": "INTERNET_NAME",
        "usecase": "All",
        "help": "Subdomains, infrastructure, related services, mentions.",
    },
    {
        "id": "ip",
        "label": "IP address",
        "placeholder": "8.8.8.8",
        "target_type": "IP_ADDRESS",
        "usecase": "All",
        "help": "Reverse DNS, hosting, ASN, abuse history, related IPs.",
    },
    {
        "id": "email",
        "label": "Email address",
        "placeholder": "user@example.com",
        "target_type": "EMAILADDR",
        "usecase": "All",
        "help": "Breach data, linked accounts, related domains.",
    },
    {
        "id": "username",
        "label": "Username",
        "placeholder": "handle (no @)",
        "target_type": "USERNAME",
        "usecase": "All",
        "help": "Public profiles on other platforms.",
    },
    {
        "id": "person",
        "label": "Person's name",
        "placeholder": "Firstname Lastname",
        "target_type": "HUMAN_NAME",
        "usecase": "Passive",
        "help": "Linked online accounts, mentions. Passive only.",
    },
    {
        "id": "phone",
        "label": "Phone number",
        "placeholder": "+15551234567",
        "target_type": "PHONE_NUMBER",
        "usecase": "Passive",
        "help": "Public listings, breach data.",
    },
]


def _preset_by_id(pid: str) -> Optional[dict]:
    for p in TARGET_PRESETS:
        if p["id"] == pid:
            return p
    return None


# --- routes: setup -------------------------------------------------


@app.route("/setup")
def setup():
    return render_template(
        "setup.html",
        status=manager.status(),
        steps=setup_instructions(APP_ROOT),
        app_root=APP_ROOT,
    )


@app.route("/setup/start", methods=["POST"])
def setup_start():
    ok, msg = manager.ensure_started()
    if ok:
        flash("SpiderFoot started.", "ok")
        return redirect(url_for("scans"))
    flash(msg, "error")
    return redirect(url_for("setup"))


# --- routes: scans -------------------------------------------------


@app.route("/")
def index():
    return redirect(url_for("scans"))


@app.route("/scans")
def scans():
    guard = _require_spiderfoot()
    if guard:
        return guard
    try:
        scan_list = _client().list_scans()
    except SpiderFootError as e:
        flash(f"Could not list scans: {e}", "error")
        scan_list = []
    return render_template("scans.html", scans=scan_list)


@app.route("/scans/new", methods=["GET", "POST"])
def scans_new():
    guard = _require_spiderfoot()
    if guard:
        return guard

    if request.method == "POST":
        preset_id = request.form.get("preset", "")
        target = (request.form.get("target") or "").strip()
        name = (request.form.get("name") or "").strip()
        usecase = request.form.get("usecase") or None
        preset = _preset_by_id(preset_id)
        if not preset:
            flash("Pick a target type.", "error")
            return redirect(url_for("scans_new"))
        if not target:
            flash("Enter a target.", "error")
            return redirect(url_for("scans_new"))
        if not name:
            name = f"{preset['label']}: {target}"
        try:
            scan_id = _client().start_scan(
                name=name,
                target=target,
                target_type=preset["target_type"],
                usecase=usecase or preset["usecase"],
            )
            flash(f"Scan started.", "ok")
            return redirect(url_for("scan_detail", scan_id=scan_id))
        except SpiderFootError as e:
            flash(f"Failed to start scan: {e}", "error")
            return redirect(url_for("scans_new"))

    return render_template("new_scan.html", presets=TARGET_PRESETS)


@app.route("/scans/<scan_id>")
def scan_detail(scan_id: str):
    guard = _require_spiderfoot()
    if guard:
        return guard
    c = _client()
    event_type = request.args.get("type", "ALL")
    try:
        status = c.scan_status(scan_id)
        summary = c.scan_event_summary(scan_id)
        events = c.scan_events(scan_id, event_type=event_type, limit=500)
    except SpiderFootError as e:
        flash(f"Could not load scan: {e}", "error")
        return redirect(url_for("scans"))
    # Find the scan record for header info
    try:
        all_scans = c.list_scans()
        scan = next((s for s in all_scans if s.id == scan_id), None)
    except SpiderFootError:
        scan = None
    return render_template(
        "scan_detail.html",
        scan=scan,
        status=status,
        summary=summary,
        events=events,
        current_type=event_type,
        sf_base=manager.state.base_url,
    )


@app.route("/scans/<scan_id>/status")
def scan_status_json(scan_id: str):
    try:
        return jsonify(_client().scan_status(scan_id))
    except SpiderFootError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/scans/<scan_id>/stop", methods=["POST"])
def scan_stop(scan_id: str):
    try:
        _client().stop_scan(scan_id)
        flash("Stop requested.", "ok")
    except SpiderFootError as e:
        flash(f"Stop failed: {e}", "error")
    return redirect(url_for("scan_detail", scan_id=scan_id))


@app.route("/scans/<scan_id>/delete", methods=["POST"])
def scan_delete(scan_id: str):
    try:
        _client().delete_scan(scan_id)
        flash("Scan deleted.", "ok")
    except SpiderFootError as e:
        flash(f"Delete failed: {e}", "error")
    return redirect(url_for("scans"))


# --- routes: investigate (Sherlock) -------------------------------


@app.route("/investigate", methods=["GET", "POST"])
def investigate():
    result = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        if username:
            result = lookup_username(username)
    return render_template(
        "investigate.html",
        username=username,
        result=result,
        sherlock_installed=sherlock_available(),
    )


# --- routes: resources --------------------------------------------


@app.route("/resources")
def resources():
    return render_template("resources.html")


# --- routes: monitor ----------------------------------------------


@app.route("/monitor")
def monitor():
    q = request.args.get("q") or None
    topic_id = request.args.get("topic_id", type=int)
    source = request.args.get("source") or None
    since = request.args.get("since") or None
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    except ValueError:
        limit = 100
    rows = monitor_store.search_matches(
        query=q, topic_id=topic_id, source=source, since=since, limit=limit,
    )
    return render_template(
        "monitor.html",
        rows=rows,
        q=q or "",
        topic_id=topic_id or "",
        source=source or "",
        since=since or "",
        limit=limit,
        topics=monitor_store.list_topics(),
        all_sources=MONITOR_SOURCES,
        source_labels=MONITOR_SOURCE_LABELS,
        stats=monitor_store.stats(),
        last_run=monitor_store.last_run(),
        run=monitor_runner.status.to_dict(),
        scheduler_enabled=monitor_scheduler.enabled(),
        scheduler_interval=monitor_scheduler.interval_min(),
        scheduler_next=monitor_scheduler.next_trigger(),
    )


@app.route("/monitor/run", methods=["POST"])
def monitor_run():
    only = request.form.getlist("source") or None
    if monitor_runner.start(only):
        flash("Monitor run started.", "ok")
    else:
        flash("A monitor run is already in progress.", "error")
    return redirect(url_for("monitor"))


@app.route("/monitor/status")
def monitor_status():
    return jsonify(monitor_runner.status.to_dict())


@app.route("/monitor/topics")
def monitor_topics():
    return render_template(
        "monitor_topics.html",
        topics=monitor_store.list_topics(),
    )


@app.route("/monitor/topics/new", methods=["GET", "POST"])
def monitor_topic_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        mode = request.form.get("match_mode", "word")
        keywords = [
            ln.strip() for ln in (request.form.get("keywords") or "").splitlines()
            if ln.strip()
        ]
        if not name:
            flash("Topic name is required.", "error")
            return redirect(url_for("monitor_topic_new"))
        if monitor_store.get_topic_by_name(name):
            flash(f"Topic {name!r} already exists.", "error")
            return redirect(url_for("monitor_topic_new"))
        tid = monitor_store.create_topic(name, mode, keywords)
        flash(f"Topic {name!r} created.", "ok")
        return redirect(url_for("monitor_topic_edit", topic_id=tid))
    return render_template("monitor_topic_edit.html", topic=None)


@app.route("/monitor/topics/<int:topic_id>", methods=["GET", "POST"])
def monitor_topic_edit(topic_id: int):
    topic = monitor_store.get_topic(topic_id)
    if not topic:
        abort(404)
    if request.method == "POST":
        name = (request.form.get("name") or topic.name).strip()
        mode = request.form.get("match_mode", topic.match_mode)
        keywords = [
            ln.strip() for ln in (request.form.get("keywords") or "").splitlines()
            if ln.strip()
        ]
        monitor_store.update_topic(topic_id, name, mode, keywords)
        flash(f"Topic {name!r} saved.", "ok")
        return redirect(url_for("monitor_topic_edit", topic_id=topic_id))
    return render_template("monitor_topic_edit.html", topic=topic)


@app.route("/monitor/topics/<int:topic_id>/delete", methods=["POST"])
def monitor_topic_delete(topic_id: int):
    topic = monitor_store.get_topic(topic_id)
    if not topic:
        abort(404)
    monitor_store.delete_topic(topic_id)
    flash(f"Topic {topic.name!r} deleted.", "ok")
    return redirect(url_for("monitor_topics"))


@app.route("/monitor/sources")
def monitor_sources_page():
    sources = monitor_store.all_sources()
    # Ensure every source has a row (even if untouched)
    for name in MONITOR_SOURCES:
        sources.setdefault(name, {"enabled": False, "config": {}})
    return render_template(
        "monitor_sources.html",
        sources=sources,
        all_sources=MONITOR_SOURCES,
        source_labels=MONITOR_SOURCE_LABELS,
    )


@app.route("/monitor/sources/<name>", methods=["POST"])
def monitor_source_save(name: str):
    if name not in MONITOR_SOURCES:
        abort(404)
    enabled = request.form.get("enabled") == "on"
    cfg: dict = {}

    def _lines(field):
        return [
            ln.strip() for ln in (request.form.get(field) or "").splitlines()
            if ln.strip()
        ]

    if name == "gdelt":
        cfg["timespan"] = (request.form.get("timespan") or "24h").strip() or "24h"
        try:
            cfg["max_records"] = int(request.form.get("max_records", "75"))
        except ValueError:
            cfg["max_records"] = 75
        cfg["language"] = (request.form.get("language") or "").strip()
    elif name == "google_news":
        cfg["lang"] = (request.form.get("lang") or "en-US").strip()
        cfg["country"] = (request.form.get("country") or "US").strip()
        cfg["ceid"] = (request.form.get("ceid") or "").strip()
    elif name == "rss":
        cfg["feeds"] = _lines("feeds")
    elif name == "reddit":
        cfg["client_id"] = (request.form.get("client_id") or "").strip()
        cfg["client_secret"] = (request.form.get("client_secret") or "").strip()
        cfg["user_agent"] = (request.form.get("user_agent") or "osint-monitor/0.1").strip()
        cfg["subreddits"] = _lines("subreddits")
        try:
            cfg["limit"] = int(request.form.get("limit", "25"))
        except ValueError:
            cfg["limit"] = 25
    elif name == "bluesky":
        try:
            cfg["limit_per_term"] = int(request.form.get("limit_per_term", "50"))
        except ValueError:
            cfg["limit_per_term"] = 50
    elif name == "mastodon":
        cfg["instance_url"] = (request.form.get("instance_url") or "").strip()
        cfg["access_token"] = (request.form.get("access_token") or "").strip()
        cfg["hashtags"] = _lines("hashtags")
        try:
            cfg["limit_per_hashtag"] = int(request.form.get("limit_per_hashtag", "40"))
        except ValueError:
            cfg["limit_per_hashtag"] = 40
    elif name == "x_twitter":
        cfg["bearer_token"] = (request.form.get("bearer_token") or "").strip()
        cfg["search_queries"] = _lines("search_queries")
        try:
            cfg["max_results_per_query"] = int(
                request.form.get("max_results_per_query", "50")
            )
        except ValueError:
            cfg["max_results_per_query"] = 50

    monitor_store.save_source(name, enabled, cfg)
    flash(f"{MONITOR_SOURCE_LABELS.get(name, name)} saved.", "ok")
    return redirect(url_for("monitor_sources_page") + f"#src-{name}")


@app.route("/monitor/runs")
def monitor_runs():
    return render_template(
        "monitor_runs.html",
        runs=monitor_store.list_runs(limit=100),
    )


# --- routes: live stream -----------------------------------------


def _last_region_pick() -> list[str]:
    raw = monitor_store.get_setting("live_last_regions", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _last_topic_pick() -> int | None:
    raw = monitor_store.get_setting("live_last_topic", "")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


@app.route("/monitor/live")
def monitor_live():
    return render_template(
        "monitor_live.html",
        topics=monitor_store.list_topics(),
        countries=sorted(COUNTRIES, key=lambda c: c.name),
        countries_by_region=by_region(),
        last_regions=_last_region_pick(),
        last_topic_id=_last_topic_pick(),
        run=monitor_runner.status.to_dict(),
        source_statuses=_source_statuses(),
    )


@app.route("/monitor/live/start", methods=["POST"])
def monitor_live_start():
    topic_id_raw = (request.form.get("topic_id") or "").strip()
    topic_ids: list[int] | None = None
    if topic_id_raw:
        try:
            topic_ids = [int(topic_id_raw)]
        except ValueError:
            topic_ids = None
    country_codes = request.form.getlist("country") or []
    monitor_store.set_setting("live_last_topic", str(topic_ids[0]) if topic_ids else "")
    monitor_store.set_setting("live_last_regions", ",".join(country_codes))
    # Live runs use every enabled source — news (with geo filter) and
    # social (which ignores it).
    started = monitor_runner.start(
        only=None,
        topic_ids=topic_ids,
        country_codes=country_codes,
        require_enabled=True,
    )
    if not started:
        return jsonify({"ok": False, "error": "A run is already in progress."}), 409
    return jsonify({"ok": True})


@app.route("/monitor/sources/status")
def monitor_sources_status():
    return jsonify(_source_statuses())


def _source_statuses() -> list[dict]:
    """Per-source readiness summary used by the Live page."""
    out = []
    for name in MONITOR_SOURCES:
        s = monitor_store.get_source(name)
        cfg = s.get("config") or {}
        configured, hint = True, ""
        if name == "reddit" and not (cfg.get("client_id") and cfg.get("client_secret")):
            configured, hint = False, "needs Reddit API key"
        elif name == "x_twitter" and not cfg.get("bearer_token"):
            configured, hint = False, "needs paid X API bearer token"
        elif name == "rss" and not cfg.get("feeds"):
            configured, hint = False, "no feed URLs configured"
        elif name == "mastodon" and not cfg.get("instance_url"):
            configured, hint = False, "no Mastodon instance set"
        out.append({
            "name": name,
            "label": MONITOR_SOURCE_LABELS.get(name, name),
            "enabled": bool(s.get("enabled")),
            "configured": configured,
            "hint": hint,
            "geo_aware": name in ("gdelt", "google_news"),
        })
    return out


# --- routes: world map -------------------------------------------


@app.route("/map")
def world_map():
    return render_template(
        "map.html",
        topics=monitor_store.list_topics(),
        last_keyword=monitor_store.get_setting("map_last_keyword", ""),
        last_topic_id=_safe_int(monitor_store.get_setting("map_last_topic", "")),
        last_timespan=monitor_store.get_setting("map_last_timespan", "7d"),
    )


def _safe_int(s: str) -> int | None:
    try:
        return int(s) if s else None
    except ValueError:
        return None


@app.route("/map/geo")
def world_map_geo():
    """Live GeoJSON of locations mentioning a keyword/topic."""
    keyword = (request.args.get("keyword") or "").strip()
    topic_id = request.args.get("topic_id", type=int)
    timespan = (request.args.get("timespan") or "7d").strip()

    keywords: list[str] = []
    chosen_topic_name = ""
    if topic_id:
        t = monitor_store.get_topic(topic_id)
        if t:
            keywords = list(t.keywords)
            chosen_topic_name = t.name
    if keyword:
        keywords.append(keyword)

    if not keywords:
        return jsonify({
            "error": "Pick a topic or type a keyword to search.",
            "query": "",
        }), 400

    # Persist for next visit
    monitor_store.set_setting("map_last_keyword", keyword)
    monitor_store.set_setting("map_last_topic", str(topic_id) if topic_id else "")
    monitor_store.set_setting("map_last_timespan", timespan)

    query = geo_build_query(keywords)
    try:
        geojson = fetch_geo(query, timespan=timespan, maxpoints=500)
    except GeoApiError as e:
        return jsonify({"error": str(e), "query": query}), 502

    features = geojson.get("features") or []
    return jsonify({
        "type": "FeatureCollection",
        "features": features,
        "query": query,
        "timespan": timespan,
        "topic_name": chosen_topic_name,
        "keyword": keyword,
        "count": len(features),
    })


# --- historical-data routes kept for backward compat -------------


@app.route("/map/data")
def world_map_data():
    topic_id = request.args.get("topic_id", type=int)
    since = request.args.get("since") or None
    counts = country_counts(monitor_store, topic_id=topic_id, since=since)
    return jsonify({
        "counts": counts,
        "total": sum(counts.values()),
        "countries": len(counts),
    })


@app.route("/map/country")
def world_map_country():
    country = (request.args.get("country") or "").strip()
    topic_id = request.args.get("topic_id", type=int)
    since = request.args.get("since") or None
    if not country:
        return jsonify({"country": "", "articles": []})
    rows = articles_for_country(
        monitor_store, country_name=country,
        topic_id=topic_id, since=since, limit=100,
    )
    articles = []
    for r in rows:
        articles.append({
            "title": r["title"] or "",
            "content": (r["content"] or "")[:280],
            "url": r["url"] or "",
            "author": r["author"] or "",
            "created_at": r["created_at"] or r["collected_at"] or "",
        })
    return jsonify({"country": country, "articles": articles})


@app.route("/monitor/scheduler", methods=["POST"])
def monitor_scheduler_save():
    enabled = request.form.get("enabled") == "on"
    try:
        interval = int(request.form.get("interval_min", "60"))
    except ValueError:
        interval = 60
    monitor_scheduler.configure(enabled, interval)
    flash("Scheduler updated.", "ok")
    return redirect(url_for("monitor"))


# --- routes: settings ---------------------------------------------


@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        sf=manager.status(),
        sherlock_installed=sherlock_available(),
    )


# --- jinja filters ------------------------------------------------


@app.template_filter("short")
def short(text, n=140):
    if not text:
        return ""
    text = str(text).replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


@app.template_filter("ts")
def ts(value):
    """Format a SpiderFoot timestamp (string-of-unix or 0)."""
    if not value or value == "0":
        return "—"
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return str(value)


@app.context_processor
def inject_globals():
    return {
        "running_states": RUNNING_STATES,
        "finished_states": FINISHED_STATES,
        "sf_status": manager.status(),
    }


if __name__ == "__main__":
    # Try to start SpiderFoot at boot so the first page load is snappy.
    if manager.state.installed:
        manager.ensure_started()
    # Start the Monitor scheduler in the background (idle unless enabled)
    monitor_scheduler.start()
    host = os.environ.get("OSINT_HOST", "127.0.0.1")
    port = int(os.environ.get("OSINT_PORT", "5000"))
    print(f"OSINT app:        http://{host}:{port}")
    print(f"SpiderFoot:       {manager.state.base_url} "
          f"({'reachable' if manager.is_reachable() else 'not reachable'})")
    print(f"Monitor DB:       {MONITOR_DB}")
    app.run(host=host, port=port, debug=False)
