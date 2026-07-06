#!/usr/bin/env python3
"""
Find missing agent emails by scraping each agent's own website.

Reads agent-data out of sacramento.html, picks the agents with a blank
email, and crawls a small set of likely pages on their listed website
(homepage, /contact, /about, /team, ...) looking for a real address.

Requires only `requests` (pip install requests). Does NOT require bs4 --
emails and mailto: links are pulled with regex, which is enough for this
job and keeps the script portable.

This will NOT work from inside a network-sandboxed Claude Code session
(egress is proxied and only a small allowlist of hosts is reachable) --
run it from a machine with normal internet access, or from an environment
whose network policy allows outbound HTTPS to arbitrary hosts. See:
https://code.claude.com/docs/en/claude-code-on-the-web

Usage:
    pip install requests
    python3 find_missing_emails.py --html ../sacramento.html --out results.csv

Output CSV columns:
    name, brokerage, website, status, best_guess, all_candidates, source_url

`status` is one of:
    found              - at least one plausible personal/team email found
    generic-only       - only role addresses found (info@, contact@, ...)
    skipped-generic    - website is a bare brokerage domain with no agent-
                         specific page (e.g. "kw.com", "exprealty.com");
                         scraping it just returns the corporate homepage
    no-match           - fetched pages but found no email at all
    error: <reason>    - network error, timeout, blocked by robots.txt, etc.
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.robotparser
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; AgentContactResearch/1.0; +mailto:research@example.com)"
REQUEST_TIMEOUT = 12
DELAY_BETWEEN_REQUESTS = 1.5  # seconds -- be polite, don't hammer small brokerage sites
CANDIDATE_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us", "/team", "/agents", "/bio", "/meet-the-team"]

GENERIC_LOCALPARTS = {
    "info", "admin", "support", "webmaster", "noreply", "no-reply", "sales",
    "help", "contact", "office", "hello", "team", "marketing", "press",
    "privacy", "legal", "careers", "jobs",
}

# Bare brokerage-corporate domains that are useless to scrape: they have no
# agent-specific page for the person we're looking for. Flag and skip instead
# of wasting requests on the parent company's homepage.
GENERIC_BROKERAGE_DOMAINS = {
    "remax.com", "exprealty.com", "kw.com", "homesmart.com",
    "realtyonegroup.com", "coldwellbankerhomes.com", "coldwellbanker.com",
    "compass.com", "redfin.com", "c21selectgroup.com",
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
MAILTO_RE = re.compile(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.IGNORECASE)


def load_targets(html_path):
    """Pull {name, brokerage, website} for every agent with a blank email."""
    html = open(html_path, encoding="utf-8").read()
    m = re.search(r'<script id="agent-data"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        raise SystemExit(f"Could not find #agent-data block in {html_path}")
    data = json.loads(m.group(1))
    return [a for a in data if not a.get("email")]


def normalize_url(website):
    website = website.strip()
    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    return website


def domain_of(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def is_generic_brokerage_domain(url):
    return domain_of(url) in GENERIC_BROKERAGE_DOMAINS


def get_robot_parser(base_url):
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        resp = requests.get(robots_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp.allow_all = True
    except requests.RequestException:
        rp.allow_all = True
    return rp


def fetch(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def extract_emails(html_text):
    """Return set of candidate emails from mailto: links and raw text."""
    found = set(e.lower() for e in MAILTO_RE.findall(html_text))
    # Fallback: emails written as plain text (obfuscated forms like
    # "name [at] domain [dot] com" are intentionally not handled -- too
    # unreliable to guess correctly).
    for e in EMAIL_RE.findall(unescape(html_text)):
        if not e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            found.add(e.lower())
    return found


def is_generic_localpart(email):
    localpart = email.split("@", 1)[0].lower()
    return localpart in GENERIC_LOCALPARTS


def rank_candidates(name, brokerage, emails):
    """Prefer emails that look like they belong to this specific person."""
    name_parts = [p.lower() for p in re.split(r"[\s'\-]+", name) if len(p) > 1]
    specific, generic = [], []
    for e in emails:
        (generic if is_generic_localpart(e) else specific).append(e)

    def score(e):
        local = e.split("@", 1)[0].lower()
        return sum(1 for p in name_parts if p in local)

    specific.sort(key=score, reverse=True)
    return specific, generic


def find_person_specific_candidates(html_text, agent_name):
    """
    For team/roster pages listing many agents, find the text window around
    each occurrence of this agent's last name and pull emails only from
    that neighborhood -- much more precise than page-wide extraction when
    a domain is shared by several people (e.g. a small brokerage site).
    """
    parts = [p for p in re.split(r"[\s'\-]+", agent_name) if len(p) > 1]
    if not parts:
        return set()
    last = parts[-1]
    windows = []
    for m in re.finditer(re.escape(last), html_text, re.IGNORECASE):
        start = max(0, m.start() - 400)
        end = min(len(html_text), m.end() + 400)
        windows.append(html_text[start:end])
    found = set()
    for w in windows:
        found |= set(e.lower() for e in MAILTO_RE.findall(w))
        found |= set(e.lower() for e in EMAIL_RE.findall(unescape(w)) if not is_generic_localpart(e))
    return found


def research_agent(agent, rp_cache):
    name, brokerage, website = agent["name"], agent.get("brokerage", ""), agent.get("website", "")
    base = normalize_url(website)
    if not base:
        return {"status": "error: no website on file", "best_guess": "", "all_candidates": "", "source_url": ""}
    if is_generic_brokerage_domain(base):
        return {"status": "skipped-generic", "best_guess": "", "all_candidates": "", "source_url": base}

    dom = domain_of(base)
    if dom not in rp_cache:
        rp_cache[dom] = get_robot_parser(base)
    rp = rp_cache[dom]

    all_found = set()
    person_found = set()
    last_ok_url = ""
    errors = []

    for path in CANDIDATE_PATHS:
        url = urljoin(base, path)
        if rp and not rp.allow_all and not rp.can_fetch(USER_AGENT, url):
            continue
        try:
            html_text = fetch(url)
        except requests.RequestException as e:
            errors.append(f"{path or '/'}: {type(e).__name__}")
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue
        last_ok_url = url
        all_found |= extract_emails(html_text)
        person_found |= find_person_specific_candidates(html_text, name)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Prefer emails found specifically near this person's name on a shared
    # page; fall back to page-wide candidates ranked by name similarity.
    pool = person_found if person_found else all_found
    specific, generic = rank_candidates(name, brokerage, pool)

    if specific:
        return {
            "status": "found",
            "best_guess": specific[0],
            "all_candidates": "; ".join(specific + generic),
            "source_url": last_ok_url,
        }
    if generic:
        return {
            "status": "generic-only",
            "best_guess": generic[0],
            "all_candidates": "; ".join(generic),
            "source_url": last_ok_url,
        }
    if last_ok_url:
        return {"status": "no-match", "best_guess": "", "all_candidates": "", "source_url": last_ok_url}
    return {"status": f"error: {'; '.join(errors) or 'unreachable'}", "best_guess": "", "all_candidates": "", "source_url": base}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default="../sacramento.html", help="Path to sacramento.html")
    ap.add_argument("--out", default="missing_emails_results.csv", help="Output CSV path")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N targets (0 = all)")
    args = ap.parse_args()

    targets = load_targets(args.html)
    if args.limit:
        targets = targets[: args.limit]

    print(f"Researching {len(targets)} agents with missing emails...\n")

    rp_cache = {}
    rows = []
    for i, agent in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {agent['name']} ({agent.get('website','')})", end=" ... ", flush=True)
        result = research_agent(agent, rp_cache)
        print(result["status"])
        rows.append({
            "name": agent["name"],
            "brokerage": agent.get("brokerage", ""),
            "website": agent.get("website", ""),
            **result,
        })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "brokerage", "website", "status", "best_guess", "all_candidates", "source_url"])
        writer.writeheader()
        writer.writerows(rows)

    found = sum(1 for r in rows if r["status"] == "found")
    generic = sum(1 for r in rows if r["status"] == "generic-only")
    print(f"\nDone. {found} found, {generic} generic-only, {len(rows) - found - generic} unresolved.")
    print(f"Results written to {args.out}")


if __name__ == "__main__":
    main()
