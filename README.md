# gov-watch

Change monitoring for Indian government websites, built on GitHub Actions. No
server, no subscription. Every check is a commit, so you get a timestamped,
line-level record of what a page said and when it stopped saying it.

- PIB is checked hourly and emails you the moment it changes.
- Everything else is checked at 09:30 and 18:30 IST and arrives as one digest.
- A static feed is published to GitHub Pages from `docs/`.

## Setup

1. Create a **public** repository and push these files.
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder `/docs`.
4. Settings → Secrets and variables → Actions → add:

   | Secret | Value |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | the sending address |
   | `SMTP_PASS` | a Gmail **app password**, not your account password |
   | `ALERT_TO` | where alerts should land |

5. Run `discover.py` before you trust the list (below), then trigger
   **watch daily** manually from the Actions tab. The first run records
   baselines and reports no changes — that is correct.

## Confirm the URLs first

```bash
pip install -r requirements.txt
python scripts/discover.py            # all sites
python scripts/discover.py rbi-press  # one site
```

For each site this prints the HTTP status, any redirect, candidate
press-release or what's-new links found on the page, and a rough CSS selector
hint. Anything marked `checked: false` in `watchlist.yaml` came from general
knowledge rather than your own list and **has not been verified live** — the
regulator and finance-department entries are all in that state. Confirm each
one, correct the URL, then set `checked: true`. The feed counts unconfirmed
URLs so the gap stays visible.

## Tuning a site

Everything is in `watchlist.yaml`.

```yaml
  - slug: rbi-press
    name: "RBI — press releases"
    url: "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
    selector: "#pressrelease_wrapper"   # narrow it once you know the page
    tier: daily
    render: browser                     # only for JavaScript-built pages
    verify_tls: false                   # only for broken certificates
    notes: "Why you are watching this."
```

`render: browser` also accepts `wait_for` (`load`, `domcontentloaded`,
`networkidle`) and `wait_selector`, for pages that build themselves in stages.

If a URL fails, `check.py` retries it on the other scheme and drops TLS
verification once on a certificate error, then reports which address actually
answered. That is how the finance portal and other `nic.in` hosts are handled.

Two rules of thumb. Point `url` at the listing page rather than the homepage
wherever one exists — homepages carry carousels and counters that generate
noise. And add a `selector` as soon as a site starts producing changes you do
not care about; it is the single most effective fix.

The `strip:` patterns in `defaults` already remove "last updated" stamps,
visitor counters, bare date lines and standalone numbers before anything is
compared.

## The failure mode to watch

A blocked request often returns a nicely styled "access denied" page with HTTP
200. Naively, that looks like a page that changed once and then went quiet —
which reads as *nothing is happening* when the truth is *you have stopped
looking*. `check.py` refuses to overwrite a snapshot when the new text contains
block-page markers, falls under `min_chars`, or shrinks by more than 65%. Those
cases are logged as errors, surfaced in the digest, and flagged on the feed.

**Silence from a page is only meaningful if the fetch succeeded.** Check the
watchlist table before writing that a ministry has said nothing.

## The local tier

GitHub's runners sit in US data centres. Several NIC-hosted ministry portals
refuse connections from those IP ranges outright, or serve a block page. Those
sites carry `tier: local` and are checked from your own machine instead, on the
same repository, so the archive and the feed stay unified.

One-off setup, in Terminal:

```bash
git clone https://github.com/<your-username>/gov-watch.git
cd gov-watch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
bash scripts/run-local.sh          # confirm it works before scheduling
```

To run it twice a day, edit `local.govwatch.plist` and replace
`REPLACE_WITH_REPO_PATH` with the full path to the cloned folder (run `pwd` to
get it), then:

```bash
cp local.govwatch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/local.govwatch.plist
```

Logs go to `~/Library/Logs/gov-watch.log`. The obvious limitation: these sites
are only checked when the Mac is awake and online. If a run is missed, the next
one still catches the change — it compares against the last snapshot, not
against yesterday.

## Costs and limits

Public repositories get unlimited Actions minutes, so the practical ceiling is
wall-clock time per run, not quota. Text snapshots delta-compress well: this
watchlist should add roughly 15–25MB of git history a year, against a 1GB
comfortable limit. Do not switch to saving screenshots or raw HTML unless you
need visual evidence for a specific story — that is 100× the storage and it
does not compress.

Scheduled workflows are best-effort: GitHub delays or drops crons under load,
and disables them entirely after 60 days of no repository activity. Treat
timings as approximate.

## Files

```
watchlist.yaml            what to watch, and how
scripts/check.py          fetch, extract, diff, snapshot
scripts/discover.py       confirm URLs, find listing pages
scripts/notify.py         immediate alerts and daily digest
scripts/build_site.py     generate the Pages feed
snapshots/<slug>.txt      current text; git history is the archive
data/events.jsonl         append-only change log
data/status.json          per-site state
```

To see what a page said on a given date:

```bash
git log --follow --date=short --pretty="%h %ad %s" snapshots/mospi.txt
git show <hash>:snapshots/mospi.txt
git diff <older> <newer> -- snapshots/mospi.txt
```

That diff, with its commit timestamp, is your evidence if the page is later
edited again.
