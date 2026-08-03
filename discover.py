#!/usr/bin/env python3
"""Confirm every URL in the watchlist is live, and propose listing pages.

Run this once before you trust the watchlist, and again whenever a site
starts failing. It never edits watchlist.yaml -- it prints what it found so
you stay the editor of your own source list.

Usage: python scripts/discover.py            (all sites)
       python scripts/discover.py rbi-press  (one slug)
"""

import sys
import pathlib
import urllib.parse as up

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Link text that usually marks the page where announcements actually land.
WANTED = [
    "press release", "press releases", "what's new", "whats new", "what s new",
    "notification", "notifications", "circular", "circulars", "media centre",
    "media center", "latest news", "announcement", "announcements", "tender",
    "recent update", "news update", "orders", "gazette",
]


def score(text):
    t = " ".join(text.lower().split())
    for i, w in enumerate(WANTED):
        if w in t:
            return len(WANTED) - i
    return 0


def main():
    cfg = yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))
    d = cfg.get("defaults", {})
    only = set(sys.argv[1:]) or None
    headers = {"User-Agent": d["user_agent"], "Accept-Language": "en-GB,en;q=0.9"}

    for site in cfg["sites"]:
        if only and site["slug"] not in only:
            continue
        print(f"\n=== {site['slug']}  {site['url']}")
        try:
            r = requests.get(site["url"], headers=headers, timeout=d.get("timeout", 45),
                             verify=site.get("verify_tls", True), allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED  {type(exc).__name__}: {exc}")
            continue

        print(f"    HTTP {r.status_code}  {len(r.text):,} bytes")
        if r.url.rstrip("/") != site["url"].rstrip("/"):
            print(f"    redirected to: {r.url}")
        if r.status_code >= 400:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        seen, found = set(), []
        for a in soup.find_all("a", href=True):
            s = score(a.get_text(" ", strip=True))
            if not s:
                continue
            href = up.urljoin(r.url, a["href"])
            if href in seen:
                continue
            seen.add(href)
            found.append((s, a.get_text(" ", strip=True)[:60], href))

        if not found:
            print("    no listing-page candidates found -- watch the homepage "
                  "with a selector instead")
        for s, label, href in sorted(found, reverse=True)[:6]:
            print(f"    candidate: {label!r}\n               {href}")

        # A crude selector hint: the largest block holding the most links.
        best, best_n = None, 0
        for tag in soup.find_all(["main", "section", "div"], class_=True):
            n = len(tag.find_all("a"))
            if n > best_n and n < 200:
                best, best_n = tag, n
        if best is not None:
            cls = ".".join(c for c in best.get("class", [])[:2] if c)
            if cls:
                print(f"    selector hint: {best.name}.{cls}  ({best_n} links)")


if __name__ == "__main__":
    main()
