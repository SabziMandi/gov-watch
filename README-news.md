# News layer

Extends gov-watch from page-diffing into a full news monitor. Same repo, same
pattern, same deploy path.

## The split that matters

| | Runs on | Sees your beat? | Committed? |
|---|---|---|---|
| `feeds.yaml` + `scripts/feeds.py` | GitHub Actions | No | Yes, public |
| `watch-terms.local.yaml` + `scripts/match.py` | Your Mac | Yes | **Never** |

GitHub does ingest, dedupe and clustering on public sources. Your Mac does the
matching. Nothing about what you are working on reaches the public repository.

Confirm this before your next push:

    git check-ignore -v watch-terms.local.yaml

Silence means it is **not** ignored. Stop and fix `.gitignore`.

## Setup

1. Copy the files in, keeping paths:
   - repo root — `feeds.yaml`, `.gitignore`, `watch-terms.example.yaml`,
     `requirements.txt` (replaces the existing one, adds `feedparser`)
   - `scripts/` — `feeds.py`, `match.py`
   - `.github/workflows/` — `feeds.yml`
   - `watchlist-additions.yaml` is a fragment: paste its contents into the
     `sites:` list of your existing `watchlist.yaml`, then delete the file
   - `watch-terms.local.yaml` goes on your **Mac only**, never uploaded

   Remember the folder trap from last time: navigate *into* `scripts/` before
   using Add file → Upload files, or they land at the root again.

2. Verify every source. All 102 ship as `verified: false`:

       python scripts/feeds.py --verify

   Fix or delete whatever fails, set `verified: true` on the rest. Unverified
   sources are still ingested but scored down and barred from leading a brief.

3. First ingest, from the Actions tab: run **feeds** manually with cadence
   `all`. It writes a baseline and commits.

4. Daily use, on your Mac:

       git pull && python scripts/match.py --window 12 --open

## Commands

    python scripts/feeds.py --verify              # probe every source
    python scripts/feeds.py --cadence fast        # hourly sources
    python scripts/feeds.py --cadence all         # everything, one pass
    python scripts/feeds.py --only dawn,tolonews  # test two sources

    python scripts/match.py --window 12 --open    # the brief
    python scripts/match.py --mode breaking       # score ≥ 6 only

## Known limits, stated plainly

**Cross-lingual clustering does not work for free.** The clusterer groups by
shared headline tokens, and Devanagari shares none with Latin. Language lag is
therefore computed at *watch* level: put the Hindi and Urdu forms of a term in
the same `any:` list as the English and the watch bridges the scripts. Lag is
only detected on topics you have named in more than one script.

**Bhojpuri is a genuine gap.** Almost no indexed text press, and GDELT does not
cover it as a language. YouTube channel feeds are the real route —
`https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`, free, no key.

**GitHub cron drifts** by 10–30 minutes under load and never retries a missed
run. Fine for this, because the design assumes 15-minute latency on obscure
sources rather than beating the wires. Scheduled workflows are also disabled
after 60 days of repo inactivity; the commits keep it alive.

**Trust is enforced, not advisory.** `trust: unconfirmed` items are scored to a
quarter and can never trigger a breaking alert. This exists because viral
movements attract lookalike domains publishing invented claims with real-looking
branding — at least five sites currently present themselves as the official CJP
home, with conflicting versions of the demands.

## Retention

90 days of items live in `data/items/`, then pruned. Roughly 180MB a year at
800 items a day. Change `retain_days` in `feeds.yaml`.
