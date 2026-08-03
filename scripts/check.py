#!/usr/bin/env python3
"""Fetch every site in a tier, extract readable text, and record what changed.

Writes:
  snapshots/<slug>.txt   current extracted text (committed, so git holds history)
  data/status.json       last check time, last error, consecutive failure count
  data/events.jsonl      append-only log of changes and failures
  data/run.json          just this run's events, for the notifier

Usage: python scripts/check.py --tier hourly
"""

import argparse
import datetime as dt
import difflib
import json
import pathlib
import re
import sys
import time

import requests
import urllib3
import yaml
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAP = ROOT / "snapshots"
DATA = ROOT / "data"

# Text that means the fetch was blocked, not that the page changed.
BLOCK_MARKERS = [
    "access denied", "403 forbidden", "request blocked", "are you a robot",
    "enable javascript to continue", "your connection is not private",
    "site is under maintenance", "service unavailable",
]


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_config():
    with open(ROOT / "watchlist.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg.get("defaults", {}), cfg["sites"]


def candidate_urls(url):
    """The URL as given, then the other scheme -- only worth trying when the
    first failure was DNS or TLS. A timeout means the host is unreachable, and
    retrying on port 80 just burns another 45 seconds."""
    yield url
    if url.startswith("https://"):
        yield "http://" + url[8:]
    elif url.startswith("http://"):
        yield "https://" + url[7:]


RETRY_OTHER_SCHEME = (requests.exceptions.SSLError, requests.exceptions.ConnectionError)


def browser_headers(site, d):
    """Headers a real Chrome sends. Several .gov.in hosts 403 anything else."""
    return {
        "User-Agent": site.get("user_agent", d["user_agent"]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,hi;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }


def fetch_http(site, d):
    headers = browser_headers(site, d)
    verify = site.get("verify_tls", True)
    timeout = site.get("timeout", d["timeout"])
    first_error = None

    for i, url in enumerate(candidate_urls(site["url"])):
        for attempt in range(site.get("retries", d.get("retries", 2)) + 1):
            try:
                r = requests.get(url, headers=headers, timeout=timeout,
                                 verify=verify, allow_redirects=True)
                r.raise_for_status()
                r.encoding = r.encoding or r.apparent_encoding
                if i:
                    print(f"         (reached via {url})")
                return r.text
            except requests.exceptions.SSLError as exc:
                first_error = first_error or exc
                if verify:
                    verify = False          # broken chain: drop verification once
                    continue
                break
            except requests.exceptions.Timeout as exc:
                first_error = first_error or exc
                break                        # unreachable, not a scheme problem
            except Exception as exc:  # noqa: BLE001
                first_error = first_error or exc
                time.sleep(2 * (attempt + 1))
        if not isinstance(first_error, RETRY_OTHER_SCHEME):
            break
    raise first_error


def fetch_browser(site, d):
    from playwright.sync_api import sync_playwright

    timeout = site.get("timeout", d["timeout"]) * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=site.get("user_agent", d["user_agent"]),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=not site.get("verify_tls", True),
            extra_http_headers={"Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8"},
        )
        page = ctx.new_page()
        try:
            page.goto(site["url"], timeout=timeout,
                      wait_until=site.get("wait_for", "domcontentloaded"))
            if site.get("wait_selector"):
                page.wait_for_selector(site["wait_selector"], timeout=25000)
            else:
                page.wait_for_timeout(site.get("settle_ms", 6000))
            html = page.content()
        finally:
            browser.close()
    return html


def extract(html, site, d):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "head"]):
        tag.decompose()

    node = soup
    if site.get("selector"):
        picked = soup.select(site["selector"])
        if picked:
            node = BeautifulSoup("".join(str(p) for p in picked), "html.parser")

    text = node.get_text("\n")
    patterns = [re.compile(p) for p in site.get("strip", d.get("strip", []))]

    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
        if not line:
            continue
        if any(p.match(line) for p in patterns):
            continue
        lines.append(line)

    # Collapse consecutive duplicates (menus repeated in mobile + desktop nav).
    out = [l for i, l in enumerate(lines) if i == 0 or l != lines[i - 1]]
    return "\n".join(out)


def looks_blocked(text, previous, floor):
    low = text.lower()
    for m in BLOCK_MARKERS:
        if m in low:
            # Say which marker fired and show the opening of the page, so the
            # digest is diagnostic rather than just discouraging.
            head = " ".join(text.split())[:180]
            return f"block marker {m!r} | page began: {head!r}"
    if len(text) < floor:
        return f"only {len(text)} chars extracted (floor {floor})"
    if previous and len(text) < len(previous) * 0.35:
        return f"content shrank {len(previous)} -> {len(text)} chars"
    return None


def diff_summary(old, new, context_lines=8):
    added = [l for l in new.splitlines() if l not in set(old.splitlines())]
    removed = [l for l in old.splitlines() if l not in set(new.splitlines())]
    udiff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]
    return {
        "added": len(added),
        "removed": len(removed),
        "sample_added": added[:context_lines],
        "sample_removed": removed[:context_lines],
        "diff": "\n".join(udiff[:120]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True)
    ap.add_argument("--only", help="comma-separated slugs, for testing")
    args = ap.parse_args()

    d, sites = load_config()
    SNAP.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)

    status_path = DATA / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}

    wanted = set(args.only.split(",")) if args.only else None
    events = []

    for site in sites:
        if site.get("tier", "daily") != args.tier:
            continue
        if wanted and site["slug"] not in wanted:
            continue

        slug = site["slug"]
        snap_file = SNAP / f"{slug}.txt"
        previous = snap_file.read_text(encoding="utf-8") if snap_file.exists() else ""
        st = status.setdefault(slug, {})
        st["name"] = site.get("name", slug)
        st["url"] = site["url"]
        st["notes"] = site.get("notes", "")
        st["tier"] = site.get("tier", "daily")
        st["checked_url"] = site.get("checked", False)
        st["last_check"] = now()

        try:
            html = (fetch_browser if site.get("render") == "browser" else fetch_http)(site, d)
            text = extract(html, site, d)
        except Exception as exc:  # noqa: BLE001
            st["state"] = "error"
            st["error"] = f"{type(exc).__name__}: {exc}"[:300]
            st["fails"] = st.get("fails", 0) + 1
            events.append({"ts": now(), "slug": slug, "name": st["name"],
                           "url": site["url"], "type": "error", "detail": st["error"]})
            print(f"[error]  {slug}: {st['error']}")
            continue

        problem = looks_blocked(text, previous, site.get("min_chars", d["min_chars"]))
        if problem:
            # Do NOT overwrite a good snapshot with a block page.
            st["state"] = "error"
            st["error"] = problem
            st["fails"] = st.get("fails", 0) + 1
            events.append({"ts": now(), "slug": slug, "name": st["name"],
                           "url": site["url"], "type": "error", "detail": problem})
            print(f"[suspect] {slug}: {problem}")
            continue

        st["error"] = ""
        st["fails"] = 0
        st["chars"] = len(text)

        if not previous:
            snap_file.write_text(text, encoding="utf-8")
            st["state"] = "baseline"
            print(f"[new]    {slug}: baseline saved ({len(text)} chars)")
            continue

        if text == previous:
            st["state"] = "unchanged"
            print(f"[same]   {slug}")
            continue

        summary = diff_summary(previous, text)
        snap_file.write_text(text, encoding="utf-8")
        st["state"] = "changed"
        st["last_change"] = now()
        events.append({"ts": now(), "slug": slug, "name": st["name"],
                       "url": site["url"], "notes": site.get("notes", ""),
                       "type": "change", **summary})
        print(f"[CHANGE] {slug}: +{summary['added']} -{summary['removed']}")

    status_path.write_text(json.dumps(status, indent=2, sort_keys=True))
    (DATA / "run.json").write_text(json.dumps(events, indent=2))
    with open(DATA / "events.jsonl", "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    changes = sum(1 for e in events if e["type"] == "change")
    print(f"\n{changes} change(s), {len(events) - changes} problem(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
