# OSINT

A target-driven OSINT investigation app built around
[SpiderFoot](https://github.com/smicallef/spiderfoot) as the scanning
engine, with [Sherlock](https://github.com/sherlock-project/sherlock)
for username lookups and a curated launchpad for the wider OSINT
ecosystem.

The primary workflow is:

1. Pick a target — domain, IP, email, username, person, phone.
2. Choose how thorough the scan should be (passive / investigate / all).
3. Watch SpiderFoot's ~200 modules enrich the target in real time.
4. Drill into results by event type (subdomains, related accounts,
   breach data, infrastructure, etc.).

## Setup

You'll do this once.

```bash
cd "/Users/nathan/Documents/Claude/Projects/OSINT"

# 1. Python venv + this app's deps
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. SpiderFoot (the scanning engine)
git clone https://github.com/smicallef/spiderfoot.git
cd spiderfoot
pip install -r requirements.txt
cd ..

# 3. (optional) Sherlock for the Usernames tab
pip install sherlock-project
```

## Run

```bash
source .venv/bin/activate
python webapp.py
```

Open <http://localhost:5000>. The app auto-starts SpiderFoot in the
background on port 5001. The indicator in the top-right shows whether
SpiderFoot is reachable.

## Pages

- **Scans** — list of every investigation you've run (target-driven OSINT via SpiderFoot).
- **New scan** — start an investigation. Pick a target type, enter the
  target, choose passive / investigate / all.
- **Scan detail** — results grouped by event type (subdomains, emails,
  related accounts, etc.), live-updating while the scan is running.
  Each scan has a link to open it in SpiderFoot directly for advanced
  options.
- **Monitor** — keyword-driven watching across news and social. Manage
  topics (keyword groups), configure sources, run collections on demand
  or on a schedule.
- **Usernames** — Sherlock username lookup across hundreds of platforms.
- **Resources** — curated launchpad: OSINT Framework, TraceLabs, NCMEC
  CyberTipline, SpiderFoot docs, image reverse-search, archives,
  breach databases, etc.
- **Settings** — SpiderFoot install status, Sherlock status, link to
  SpiderFoot's own settings (where you add API keys for modules like
  Shodan, VirusTotal, HIBP).

## Two workflows

The app supports two complementary OSINT modes:

**Scans** (SpiderFoot) — target-driven. Give it a specific thing
(domain, IP, email, username, person, phone) and it actively enriches
it with ~200 modules. Best when you have a lead and want to expand it.

**Monitor** (Monitor tab) — keyword-driven. Define topics with
keywords, enable global news and social sources, and the app
continuously watches for any new content mentioning your keywords.
Best for tracking a topic, place, person, or incident type as
coverage emerges.

## Monitor: supported sources

| Source         | Auth required        | Notes                                        |
|----------------|---------------------|----------------------------------------------|
| GDELT          | None                | Global news, ~100 languages, refreshed every 15 min |
| Google News    | None                | RSS keyword search                           |
| RSS / Atom     | None                | Any feed URL you supply                      |
| Reddit         | Free API key        | reddit.com/prefs/apps                        |
| Bluesky        | None                | Public search endpoint                       |
| Mastodon       | Optional            | Public hashtag timeline                      |
| X / Twitter    | Paid API ($200/mo)  | v2 Basic tier required                       |

**Not supported:** Facebook and Instagram. Meta removed public
keyword-search APIs years ago and CrowdTangle (the one legitimate
alternative) was shut down in August 2024. There is no compliant path.

## Configuring SpiderFoot modules

Many SpiderFoot modules require their own API keys (Shodan, VirusTotal,
HaveIBeenPwned, etc.). Set those inside SpiderFoot itself — there's a
direct link from this app's Settings page, or open
<http://localhost:5001/optsraw> in your browser.

Without API keys, the free-tier modules still produce useful results;
keys just unlock more modules.

## File layout

```
webapp.py               Flask web UI
spiderfoot_client.py    HTTP client for SpiderFoot's API
spiderfoot_manager.py   subprocess lifecycle for SpiderFoot
investigate.py          Sherlock username lookup wrapper
templates/              Jinja2 templates (scans, new_scan, scan_detail,
                        setup, settings, investigate, resources)
spiderfoot/             SpiderFoot itself (cloned by you, gitignored)
_archive/               older feed-scraper code, kept for reference
```

## Why no more feed scraping?

The first iteration of this app was a generic keyword-monitor across
RSS, Reddit, Bluesky, Mastodon, and similar feeds. In practice it was
low-yield: feeds carry few items per pull, narrow keywords rarely hit,
and platforms increasingly require paid API tiers. SpiderFoot is a
better center of gravity for target-driven OSINT — you investigate a
*specific thing* rather than waiting for keywords to flash by. The old
code is preserved under `_archive/` if you ever want to look back.
