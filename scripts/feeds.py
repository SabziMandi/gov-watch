#!/usr/bin/env python3
"""Ingest the news layer: RSS, Google News RSS queries and GDELT.

Runs on GitHub. Knows nothing about what you are working on — no watch terms
are read here, by design. Matching happens in scripts/match.py, on your Mac.

Writes:
  data/items/<YYYY-MM-DD>.jsonl   one line per new item, append-only
  data/seen.json                  url + title fingerprints, for deduplication
  data/clusters.json              rolling story clusters (same event, many outlets)
  data/feed_status.json           per-feed health, mirroring status.json

Usage:
  python scripts/feeds.py --cadence fast
  python scripts/feeds.py --cadence standard --only hindu-national,dawn
  python scripts/feeds.py --verify          # probe every feed, change nothing
"""

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.parse as up

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ITEMS = DATA / "items"

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS_RSS = "https://news.google.com/rss/search"

# Query parameters that identify a campaign, not a document. Stripping these is
# what stops the same story arriving four times from four referrers.
TRACKING = re.compile(
    r"^(utm_|fbclid|gclid|igshid|mc_cid|mc_eid|ref|ref_src|source|amp|"
    r"__twitter_impression|at_medium|at_campaign|CMP|ncid|spm)", re.I)

# Google News indexes a site's tag pages, pagination, search results and
# homepage alongside its journalism, and a site: query returns the lot. On the
# first full ingest these accounted for the four largest clusters in the store
# — 54 items of bdnews24 tag pages alone — while crowding real reporting out
# of each feed's item allowance. Add your own patterns via junk_title_extra
# and junk_path_extra in feeds.yaml.
JUNK_TITLE = [
    r"\btag related all news\b",
    r"\bpage \d+ of\b",
    r"^page \d+\b",
    r"\bsearch results? for\b",
    r"\|\s*page \d+",
    r"\be-?paper\b",
    r"\bcookie policy\b",
    r"\bprivacy policy\b",
    r"\bterms (?:of|and) (?:use|service|conditions)\b",
    r"\bsubscribe\b.*\bnewsletter\b",
    r"\barchives?\s*[-|–]\s*",
    r"\b(?:latest|breaking|today'?s) news (?:in|from|online)\b",
    r"\bnews agency\b.*\bnews,\s*business\b",
    r"\bprices,\s*indices\b",
    r"\bstock quotes\b",
    r"\brelated all news\b",
    r"\bsitemap\b",
    r"\brecruitment\b.*\bpost of\b",
    r"\btender for\b",
    # Airline route landing pages. airindia.com has hundreds and Google News
    # indexes them all: "Flights from Oman", "Hyderabad to Italy Flights",
    # "Flights to Amsterdam (AMS)". None of them says "booking" or "ticket",
    # which is why keyword exclusions on the watch side missed them.
    r"^flights? (?:from|to)\b",
    r"^book flights?\b",
    r"\bflights? (?:from|to) [\w\s]+\(\w{3}\)",
    r"\bticket price (?:from|starting)\b",
    r"^\w[\w\s]* to [\w\s]* flights?\b",
    r"\bcheapest flights?\b",
    r"\bfare[s]? (?:from|starting at)\b",
]

JUNK_PATH = [
    r"/tags?/", r"/topics?/", r"/page/\d+", r"/search",
    r"/author/", r"/e-?paper", r"/archives?/", r"/sitemap",
    r"/privacy", r"/cookie", r"/subscribe", r"/newsletter",
    r"/flights?[-/]", r"/book[-/]", r"/destinations?/", r"/routes?/",
    r"/fares?/", r"/offers?/",
]

STOP = {
    "the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with", "as",
    "at", "by", "from", "is", "are", "was", "were", "be", "been", "it", "its",
    "that", "this", "these", "those", "after", "over", "amid", "says", "said",
    "new", "up", "out", "his", "her", "their", "has", "have", "will", "not",
}


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_config():
    with open(ROOT / "feeds.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg.get("defaults", {}), cfg["feeds"]


# ----------------------------------------------------------------- urls ----

def canonical(url):
    """Strip tracking, fragments and AMP, so the same article hashes the same
    however it reached us."""
    if not url:
        return ""
    try:
        p = up.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    keep = [(k, v) for k, v in up.parse_qsl(p.query, keep_blank_values=False)
            if not TRACKING.match(k)]
    path = re.sub(r"/(amp|amp\.html)/?$", "", p.path).rstrip("/") or "/"
    return up.urlunsplit((p.scheme or "https", host, path,
                          up.urlencode(sorted(keep)), ""))


def unwrap_google(url):
    """Google News links wrap the publisher URL. Recover it where we can, so
    dedupe works against direct RSS hits on the same story."""
    try:
        q = dict(up.parse_qsl(up.urlsplit(url).query))
    except ValueError:
        return url
    return q.get("url", url)


def domain(url):
    try:
        h = up.urlsplit(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except ValueError:
        return ""


def dedupe_key(url):
    """Identity, not a link. Scheme is dropped because the same story arrives
    over http from one feed and https from another, and those are one story.
    The stored URL keeps its scheme so it stays clickable."""
    c = canonical(url)
    return re.sub(r"^https?://", "", c)


def fingerprint(url, title):
    return hashlib.sha1(f"{dedupe_key(url)}|{norm_title(title)}"
                        .encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------- titles ----

def norm_title(t):
    t = re.sub(r"\s+", " ", (t or "")).strip().lower()
    t = re.sub(r"\s*[-–—|]\s*[^-–—|]{2,40}$", "", t)   # drop trailing outlet name
    return re.sub(r"[^\w\s\u0900-\u097F\u0600-\u06FF]", "", t).strip()


def is_furniture(title, url, extra_title=(), extra_path=()):
    """True when this is a section, tag, search or policy page rather than a
    story. Cheap to check and it is the difference between a feed's allowance
    holding 60 articles or 60 tag pages."""
    t = (title or "").lower()
    u = (url or "").lower()
    for pat in list(JUNK_TITLE) + list(extra_title):
        if re.search(pat, t, re.I):
            return True
    for pat in list(JUNK_PATH) + list(extra_path):
        if re.search(pat, u, re.I):
            return True
    # A homepage. Google News indexes these and the title is the masthead
    # tagline, which then clusters with every other hit from the same site.
    try:
        path = up.urlsplit(u).path
        if path in ("", "/", "/en", "/en/", "/home", "/index.html"):
            return True
    except ValueError:
        pass
    # A title that is only the outlet's name, e.g. "- Frontier Myanmar":
    # once the trailing outlet suffix is stripped, nothing substantive is left.
    core = norm_title(title)
    if len(core.split()) < 2:
        return True
    return False


def tokens(t):
    return {w for w in norm_title(t).split() if w not in STOP and len(w) > 2}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a, b):
    """Shared tokens as a fraction of the SHORTER headline.

    Jaccard is the wrong measure here. It divides by the union, so it punishes
    two headlines for the words they do not share — and outlets write
    deliberately different headlines about the same event. "AAIB report on Air
    India crash due" and "Air India crash probe takes new turn" share three
    substantial tokens and score only 0.33 on Jaccard, below any usable
    threshold. On overlap they score 0.50 and merge correctly.

    The guard against over-merging is MIN_SHARED, not the ratio: two headlines
    must share at least two content tokens. A single shared "india" is what
    would otherwise glue a test match to a trade deal."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


MIN_SHARED = 2


# -------------------------------------------------------------- fetchers ---

def browser_headers(d):
    """Headers for fetching a FEED, not for loading a page.

    An earlier version of this sent Sec-Fetch-Dest: document, Sec-Fetch-Mode:
    navigate, Upgrade-Insecure-Requests and an Accept that listed text/html.
    Together those say "I am a browser opening a web page", and servers
    obliged by returning the HTML article page instead of the XML feed.
    feedparser parsed the HTML happily, found no <item> elements, and reported
    zero entries with no error flag — so 18 working sources went silently
    empty and looked like they had blocked us.

    Two rules here. Do not claim to accept text/html. Do not describe the
    request as a top-level navigation. And no Accept-Encoding: urllib3 already
    advertises exactly what it can decode.
    """
    return {
        "User-Agent": d["user_agent"],
        "Accept": ("application/rss+xml, application/atom+xml, "
                   "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"),
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,hi;q=0.7,ur;q=0.6",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "none",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }


def http_get(url, d, timeout=None, retry_429=0):
    """retry_429 > 0 backs off and retries. GDELT rate-limits hard when a
    verify pass walks the whole file, and a 429 is not a broken source."""
    for attempt in range(retry_429 + 1):
        r = requests.get(url, headers=browser_headers(d),
                         timeout=timeout or d.get("timeout", 30),
                         allow_redirects=True)
        if r.status_code == 429 and attempt < retry_429:
            wait = 15 * (attempt + 1)
            print(f"         (429, waiting {wait}s)")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def parse_rss(text, url=""):
    import feedparser
    parsed = feedparser.parse(text)

    out = []
    for e in parsed.entries:
        published = ""
        for key in ("published_parsed", "updated_parsed"):
            if getattr(e, key, None):
                published = dt.datetime(*getattr(e, key)[:6],
                                        tzinfo=dt.timezone.utc).isoformat()
                break
        summary = re.sub(r"<[^>]+>", " ", getattr(e, "summary", "") or "")
        # Google News wraps every link in news.google.com. Without recovering
        # the real publisher from <source url="">, every item would share one
        # domain and the corroboration score — which counts distinct outlets
        # on a story — would read 1 for a story ten outlets had run.
        src = getattr(e, "source", None)
        src_url = ""
        if isinstance(src, dict):
            src_url = src.get("href", "") or ""
        # thedailystar.net puts a raw <a href=...> in the title element. An
        # unstripped title would poison both the fingerprint and the tokens.
        title = re.sub(r"<[^>]+>", " ", getattr(e, "title", "") or "")
        title = re.sub(r"\s+", " ", title).strip()
        out.append({
            "title": title,
            "url": (getattr(e, "link", "") or "").strip(),
            "summary": re.sub(r"\s+", " ", summary).strip()[:600],
            "published": published,
            "source_url": src_url,
        })
    return out


def fetch_rss(feed, d):
    r = http_get(feed["url"], d)
    items = parse_rss(r.text, feed["url"])
    if not items:
        # Say what arrived. A feed that parses to nothing is almost always a
        # server returning HTML, and the content type shows that instantly —
        # which is the check that should have existed three attempts ago.
        ctype = r.headers.get("Content-Type", "?").split(";")[0]
        enc = r.headers.get("Content-Encoding", "-")
        head = " ".join(r.text[:110].split())
        print(f"         (HTTP {r.status_code} | {ctype} | enc={enc} | "
              f"starts: {head!r})")
    return items


# Google News serves a different edition per language. Querying a Hindi
# outlet through the English edition returns almost nothing.
GNEWS_LOCALE = {
    "en": ("en-IN", "IN", "IN:en"),
    "hi": ("hi", "IN", "IN:hi"),
    "ur": ("ur", "IN", "IN:ur"),
    "mr": ("mr", "IN", "IN:mr"),
    "bn": ("bn", "IN", "IN:bn"),
    "ne": ("ne", "NP", "NP:ne"),
    "fa": ("fa", "AF", "AF:fa"),
    "dv": ("en-IN", "IN", "IN:en"),
}


# Without a time window a `site:` query is a site SEARCH, not a feed: Google
# returns whatever it ranks highest for that domain, which turns out to be
# cookie policies, login pages and course brochures. `when:` forces recency
# and is what makes these behave like feeds at all.
GNEWS_WINDOW = {"fast": "1d", "standard": "2d", "slow": "7d"}


def fetch_gnews(feed, d):
    hl, gl, ceid = GNEWS_LOCALE.get(feed.get("lang", "en"),
                                    GNEWS_LOCALE["en"])
    if feed.get("gnews_locale"):
        hl, gl, ceid = feed["gnews_locale"]
    q = feed["query"]
    if "when:" not in q:
        window = feed.get("gnews_window") or GNEWS_WINDOW.get(
            feed.get("cadence", d.get("cadence", "standard")), "2d")
        q = f"{q} when:{window}"
    url = (f"{GNEWS_RSS}?"
           f"{up.urlencode({'q': q, 'hl': hl, 'gl': gl, 'ceid': ceid})}")
    items = parse_rss(http_get(url, d).text)
    for it in items:
        it["url"] = unwrap_google(it["url"])
    return items


def fetch_gdelt(feed, d):
    q = feed["query"]
    if feed.get("sourcecountry"):
        q += f" sourcecountry:{feed['sourcecountry']}"
    if feed.get("sourcelang"):
        q += f" sourcelang:{feed['sourcelang']}"
    params = {
        "query": q,
        "mode": "artlist",
        "format": "json",
        "maxrecords": feed.get("maxrecords", d.get("gdelt_maxrecords", 200)),
        "timespan": feed.get("timespan", d.get("gdelt_timespan", "3h")),
        "sort": "datedesc",
    }
    r = http_get(f"{GDELT_API}?{up.urlencode(params)}", d,
                 retry_429=feed.get("_retry_429", 1))
    # GDELT answers a bad query with HTML, not a JSON error. Say so plainly.
    try:
        payload = r.json()
    except ValueError:
        raise ValueError(f"GDELT returned non-JSON (likely a malformed query): "
                         f"{r.text[:160]!r}")
    out = []
    for a in payload.get("articles", []):
        stamp = a.get("seendate", "")
        try:
            published = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            published = ""
        out.append({
            "title": (a.get("title") or "").strip(),
            "url": (a.get("url") or "").strip(),
            "summary": "",
            "published": published,
            "gdelt_lang": (a.get("language") or "").lower(),
            "gdelt_country": (a.get("sourcecountry") or "").upper(),
        })
    return out


FETCHERS = {"rss": fetch_rss, "gnews": fetch_gnews, "gdelt": fetch_gdelt}


# ------------------------------------------------------------ clustering ---

def cluster(items, clusters, threshold=0.50, window_hours=72):
    """Group items describing the same event. Token overlap on the headline,
    no model and no API — good enough that paying for embeddings would be hard
    to justify at this volume."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    live = []
    for c in clusters:
        try:
            if dt.datetime.fromisoformat(c["last_seen"]) >= cutoff:
                c["_tok"] = set(c["tokens"])
                live.append(c)
        except (ValueError, KeyError):
            continue

    for it in items:
        tok = tokens(it["title"])
        if not tok:
            it["cluster"] = None
            continue
        best, score, shared = None, 0.0, 0
        for c in live:
            common = len(tok & c["_tok"])
            if common < MIN_SHARED:
                continue
            s = overlap(tok, c["_tok"])
            if s > score:
                best, score, shared = c, s, common
        if best and score >= threshold and shared >= MIN_SHARED:
            # Keep only tokens seen more than once as the cluster grows, so a
            # large cluster does not accumulate a wide net and start pulling in
            # loosely related stories.
            best["_tok"] = (best["_tok"] & tok) | set(list(best["_tok"])[:24])
            best["tokens"] = sorted(best["_tok"])[:60]
            best["size"] += 1
            best["last_seen"] = it["ingested"]
            if it["domain"] and it["domain"] not in best["domains"]:
                best["domains"].append(it["domain"])
            langs = best.setdefault("first_by_lang", {})
            langs.setdefault(it["lang"], it["ingested"])
            it["cluster"] = best["id"]
        else:
            cid = hashlib.sha1(
                f"{it['fingerprint']}{it['ingested']}".encode()).hexdigest()[:12]
            new = {
                "id": cid,
                "headline": it["title"][:200],
                "tokens": sorted(tok)[:60],
                "size": 1,
                "domains": [it["domain"]] if it["domain"] else [],
                "first_seen": it["ingested"],
                "last_seen": it["ingested"],
                "first_by_lang": {it["lang"]: it["ingested"]},
                "_tok": tok,
            }
            live.append(new)
            it["cluster"] = cid

    for c in live:
        c.pop("_tok", None)
    return live


# ------------------------------------------------------------------ main ---

def promote(passed):
    """Flip verified: false -> true for slugs that returned items. Edits the
    YAML as text so comments, ordering and formatting survive."""
    path = ROOT / "feeds.yaml"
    lines = path.read_text(encoding="utf-8").split("\n")
    slug, changed = None, 0
    for i, line in enumerate(lines):
        m = re.match(r"  - slug: (\S+)", line)
        if m:
            slug = m.group(1)
        elif slug in passed and re.match(r"    verified: false\s*$", line):
            lines[i] = "    verified: true"
            changed += 1
    path.write_text("\n".join(lines), encoding="utf-8")
    return changed


def verify(defaults, feeds, do_promote=False):
    """Probe every source. Changes nothing on disk — this is discover.py for
    the news layer."""
    ok = bad = 0
    passed = set()
    for f in feeds:
        kind = f.get("kind", "rss")
        label = f"{f['slug']:<28} {kind:<6}"
        try:
            if kind == "gdelt":
                f = {**f, "_retry_429": 2}
            items = FETCHERS[kind](f, defaults)
            if not items:
                print(f"[empty]  {label} parsed, but returned 0 items")
                bad += 1
            else:
                print(f"[ok]     {label} {len(items):>3} items | "
                      f"{items[0]['title'][:60]!r}")
                passed.add(f["slug"])
                ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL]   {label} {type(exc).__name__}: {exc}"[:160])
            bad += 1
        # GDELT rate-limits aggressively. One request every eight seconds is
        # slow but it is the difference between five false failures and none.
        time.sleep(8.0 if kind == "gdelt" else 0.6)
    print(f"\n{ok} working, {bad} to fix.")
    if do_promote:
        n = promote(passed)
        print(f"promoted {n} source(s) to verified: true in feeds.yaml. "
              f"The {bad} that failed are untouched — still ingested, but "
              f"scored down and barred from leading a brief.")
    else:
        print("Set verified: true on the ones that pass, or re-run with "
              "--promote to do it automatically.")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cadence", choices=["fast", "standard", "slow", "all"])
    ap.add_argument("--only", help="comma-separated slugs, for testing")
    ap.add_argument("--verify", action="store_true",
                    help="probe every feed and exit without writing")
    ap.add_argument("--promote", action="store_true",
                    help="with --verify: set verified: true in feeds.yaml for "
                         "every source that returned items, leaving the rest "
                         "untouched. Beats hand-editing 90-odd entries.")
    ap.add_argument("--skip-gdelt", action="store_true",
                    help="leave GDELT alone during verify. Its quota is "
                         "per-IP and cumulative, so repeated verify runs "
                         "trip it even though production load never would.")
    args = ap.parse_args()

    d, feeds = load_config()
    if args.only:
        wanted = set(args.only.split(","))
        feeds = [f for f in feeds if f["slug"] in wanted]

    if args.verify:
        if args.skip_gdelt:
            feeds = [f for f in feeds if f.get("kind") != "gdelt"]
        return verify(d, feeds, do_promote=args.promote)

    if not args.cadence:
        ap.error("--cadence is required unless --verify is given")

    DATA.mkdir(exist_ok=True)
    ITEMS.mkdir(parents=True, exist_ok=True)

    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    clus_path = DATA / "clusters.json"
    clusters = json.loads(clus_path.read_text()) if clus_path.exists() else []
    stat_path = DATA / "feed_status.json"
    status = json.loads(stat_path.read_text()) if stat_path.exists() else {}

    stamp = now()
    fresh = []

    for f in feeds:
        if args.cadence != "all" and \
                f.get("cadence", d.get("cadence", "standard")) != args.cadence:
            continue

        slug = f["slug"]
        st = status.setdefault(slug, {})
        st.update({"name": f.get("name", slug), "tier": f.get("tier"),
                   "kind": f.get("kind", "rss"), "lang": f.get("lang", "en"),
                   "verified": f.get("verified", False),
                   "trust": f.get("trust", "reported"), "last_check": stamp})

        try:
            raw = FETCHERS[f.get("kind", "rss")](f, d)
        except Exception as exc:  # noqa: BLE001
            st["state"] = "error"
            st["error"] = f"{type(exc).__name__}: {exc}"[:300]
            st["fails"] = st.get("fails", 0) + 1
            print(f"[error]  {slug}: {st['error']}")
            continue

        st["error"] = ""
        st["fails"] = 0
        added = dropped = 0

        for it in raw[:f.get("max_items_per_feed", d.get("max_items_per_feed", 60))]:
            if not it["title"] or not it["url"]:
                continue
            if is_furniture(it["title"], it["url"],
                            d.get("junk_title_extra", ()),
                            d.get("junk_path_extra", ())):
                dropped += 1
                continue
            fp = fingerprint(it["url"], it["title"])
            if fp in seen:
                continue
            seen[fp] = stamp
            url = canonical(it["url"])
            real = domain(url)
            if real.endswith("news.google.com") and it.get("source_url"):
                real = domain(it["source_url"])
            fresh.append({
                "fingerprint": fp,
                "title": it["title"][:300],
                "url": url,
                "domain": real,
                "summary": it.get("summary", ""),
                "published": it.get("published", ""),
                "ingested": stamp,
                "source": slug,
                "source_name": f.get("name", slug),
                "tier": f.get("tier", 2),
                # GDELT reports the article's own language; a configured feed
                # is trusted for its own. Accuracy here drives language lag.
                "lang": it.get("gdelt_lang") or f.get("lang", "en"),
                "country": it.get("gdelt_country") or f.get("country", ""),
                "trust": f.get("trust", "reported"),
                "verified": bool(f.get("verified", False)),
            })
            added += 1

        st["state"] = "ok"
        st["last_items"] = added
        st["last_dropped"] = dropped
        note = f", {dropped} furniture dropped" if dropped else ""
        print(f"[ok]     {slug}: {added} new of {len(raw)}{note}")
        time.sleep(0.4)

    clusters = cluster(fresh, clusters)

    if fresh:
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        with open(ITEMS / f"{day}.jsonl", "a", encoding="utf-8") as fh:
            for it in fresh:
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")

    # Retention. Without this the repo grows without limit and eventually
    # git operations get slow enough that you stop using the thing.
    keep = int(d.get("retain_days", 90))
    floor = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep)).date()
    for path in ITEMS.glob("*.jsonl"):
        try:
            if dt.date.fromisoformat(path.stem) < floor:
                path.unlink()
                print(f"[prune]  {path.name}")
        except ValueError:
            continue
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cut}

    seen_path.write_text(json.dumps(seen, indent=0, sort_keys=True))
    clus_path.write_text(json.dumps(clusters, indent=1, ensure_ascii=False))
    stat_path.write_text(json.dumps(status, indent=2, sort_keys=True))

    print(f"\n{len(fresh)} new item(s), {len(clusters)} live cluster(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
