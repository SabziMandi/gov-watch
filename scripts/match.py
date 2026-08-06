#!/usr/bin/env python3
"""Match the ingested item store against your private watch terms.

Runs ONLY on your Mac. Reads watch-terms.local.yaml, which is gitignored, so
nothing about what you are working on ever reaches the public repository.

Reads   data/items/*.jsonl, data/clusters.json   (public, pulled via git)
Writes  local/brief.html, local/state.json       (gitignored, never committed)

Usage:
  git pull && python scripts/match.py --window 12 --open
  python scripts/match.py --mode breaking       # tighter, alert-only
"""

import argparse
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import webbrowser

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ITEMS = DATA / "items"
LOCAL = ROOT / "local"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

TERMS_FILE = ROOT / "watch-terms.local.yaml"


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_terms():
    if not TERMS_FILE.exists():
        sys.exit(
            f"{TERMS_FILE.name} not found.\n\n"
            "This file is deliberately absent from the repository — it holds "
            "your beat.\nCopy watch-terms.example.yaml to watch-terms.local.yaml "
            "and edit it.\nCheck .gitignore lists it before you commit anything."
        )
    with open(TERMS_FILE, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg.get("settings", {}), cfg.get("watches", []), cfg.get("diary", [])


def load_items(days):
    floor = (now_utc() - dt.timedelta(days=days)).date()
    out = []
    for path in sorted(ITEMS.glob("*.jsonl")):
        try:
            if dt.date.fromisoformat(path.stem) < floor:
                continue
        except ValueError:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def load_clusters():
    path = DATA / "clusters.json"
    if not path.exists():
        return {}
    try:
        return {c["id"]: c for c in json.loads(path.read_text())}
    except (json.JSONDecodeError, KeyError):
        return {}


# --------------------------------------------------------------- matching --

def compile_terms(strings):
    r"""Whole-word, case-insensitive, Unicode-safe. Substring matching turns
    'AI' into a match on 'said' and makes the whole tool untrustworthy.

    Returns (label, pattern) pairs so the brief can report the term you wrote
    rather than the regex it compiled to — nobody wants to read
    (?<!\w)Air\ India(?!\w) in a morning brief.
    """
    out = []
    for s in strings or []:
        s = s.strip()
        if not s:
            continue
        if s.startswith("/") and s.endswith("/") and len(s) > 2:
            out.append((s, re.compile(s[1:-1], re.I | re.U)))
        else:
            out.append((s, re.compile(rf"(?<!\w){re.escape(s)}(?!\w)", re.I | re.U)))
    return out


def hits(patterns, text):
    return [label for label, p in patterns if p.search(text)]


def matches(watch, item):
    blob = f"{item.get('title','')} {item.get('summary','')}"
    if watch["_none"] and hits(watch["_none"], blob):
        return None
    got_any = hits(watch["_any"], blob) if watch["_any"] else []
    if watch["_any"] and not got_any:
        return None
    if watch["_all"]:
        got_all = hits(watch["_all"], blob)
        if len(got_all) < len(watch["_all"]):
            return None
        got_any += got_all
    return sorted(set(got_any))


TIER_WEIGHT = {1: 3.0, 2: 2.0, 3: 2.5}       # vernacular scores high on purpose
TRUST_WEIGHT = {"primary": 2.5, "reported": 1.0,
                "commentary": 0.5, "unconfirmed": 0.0}


def score(item, clusters):
    s = TIER_WEIGHT.get(item.get("tier", 2), 1.0)
    s += TRUST_WEIGHT.get(item.get("trust", "reported"), 1.0)
    c = clusters.get(item.get("cluster") or "")
    if c:
        s += min(len(c.get("domains", [])), 8) * 0.6   # corroboration
    if not item.get("verified", False):
        s *= 0.4          # unverified sources are visible but never lead
    if item.get("trust") == "unconfirmed":
        s *= 0.25
    return round(s, 2)


# ------------------------------------------------------------ derived views -

def language_lag(tagged, min_hours):
    """A story that ran in Urdu or Hindi well before it ran in English. The gap
    is often the story, and nothing off-the-shelf will tell you about it.

    This works at WATCH level, not cluster level, and the reason matters. The
    clusterer groups by shared headline tokens, and a Devanagari headline shares
    no tokens with a Latin one — so the same event in Hindi and English never
    lands in one cluster. Genuine cross-lingual clustering needs translation,
    which is not free. Watch level sidesteps it entirely: if you supply the
    Hindi and Urdu forms of a term alongside the English, the watch itself is
    the bridge, and the earliest match per language gives the lag directly.

    The cost is honest and worth stating: lag is only detected on topics you
    have named in more than one script.
    """
    by_watch = {}
    for it in tagged:
        first = by_watch.setdefault(it["_watch"], {})
        lang = it.get("lang", "en")
        try:
            when = dt.datetime.fromisoformat(it["ingested"])
        except (ValueError, KeyError):
            continue
        if lang not in first or when < first[lang][0]:
            first[lang] = (when, it)

    out = []
    for slug, langs in by_watch.items():
        if "en" not in langs or len(langs) < 2:
            continue
        eng_when, eng_item = langs["en"]
        for lang, (when, item) in langs.items():
            if lang == "en":
                continue
            gap = (eng_when - when).total_seconds() / 3600
            if gap >= min_hours:
                out.append({"watch": slug, "lang": lang, "hours": round(gap, 1),
                            "first": item, "english": eng_item})
    return sorted(out, key=lambda r: -r["hours"])


def silence(watch, items, state, floor_days):
    """A watch that was busy and has gone quiet. Absence is a weak signal on
    its own, so this only fires for terms that had real traffic before."""
    key = watch["slug"]
    recent = sum(1 for i in items if i.get("_watch") == key)
    prior = state.get("counts", {}).get(key, [])
    prior = (prior + [recent])[-14:]
    state.setdefault("counts", {})[key] = prior
    if len(prior) < floor_days + 1:
        return None
    baseline = sum(prior[:-1]) / max(len(prior) - 1, 1)
    if baseline >= 3 and recent == 0:
        return {"slug": key, "name": watch["name"], "baseline": round(baseline, 1)}
    return None


def diary_due(diary, days_ahead):
    today = dt.datetime.now(IST).date()
    horizon = today + dt.timedelta(days=days_ahead)
    out = []
    for e in diary or []:
        try:
            when = dt.date.fromisoformat(str(e["date"]))
        except (ValueError, KeyError, TypeError):
            continue
        if today <= when <= horizon:
            out.append({**e, "_date": when, "_in": (when - today).days})
    return sorted(out, key=lambda e: e["_date"])


# ----------------------------------------------------------------- render --

CSS = """
:root{--ink:#16181d;--dim:#5d6167;--faint:#8b9096;--paper:#fbfbf9;
--line:#e3e2dd;--panel:#f4f3ef;--signal:#a76a12;--fail:#9d2f2f;--ok:#3f6b46}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--faint);font-size:13px;margin:0 0 28px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;
color:var(--dim);margin:36px 0 10px;padding-bottom:6px;
border-bottom:1px solid var(--line)}
.item{padding:11px 0;border-bottom:1px solid var(--line)}
.item a{color:var(--ink);text-decoration:none;font-weight:500}
.item a:hover{text-decoration:underline}
.meta{color:var(--faint);font-size:12.5px;margin-top:3px}
.also{color:var(--dim);font-size:12.5px;margin-top:3px;padding-left:9px;
border-left:2px solid var(--line)}
.sc{display:inline-block;min-width:34px;color:var(--signal);
font:500 12px/1 ui-monospace,monospace}
.tag{display:inline-block;background:var(--panel);border:1px solid var(--line);
border-radius:3px;padding:1px 6px;font-size:11px;color:var(--dim);margin-left:5px}
.warn{background:#fdf6e9;border-left:3px solid var(--signal);
padding:10px 14px;margin:8px 0;font-size:14px}
.quiet{background:var(--panel);padding:10px 14px;margin:8px 0;font-size:14px}
.diary{display:flex;gap:14px;padding:9px 0;border-bottom:1px solid var(--line)}
.when{font:500 12px/1.5 ui-monospace,monospace;color:var(--signal);
min-width:96px;white-space:nowrap}
.empty{color:var(--faint);font-size:14px;padding:8px 0}
"""


def render(groups, lag, quiet, diary, window, unverified):
    esc = html.escape
    built = dt.datetime.now(IST).strftime("%d %B %Y, %H:%M IST")
    p = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
         f"<meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>Brief — {built}</title><style>{CSS}</style></head><body><div class=wrap>",
         f"<h1>Brief</h1><p class=sub>{esc(built)} · last {window}h · "
         f"{sum(len(v) for v in groups.values())} matched items</p>"]

    if diary:
        p.append("<h2>Coming up</h2>")
        for e in diary:
            when = "today" if e["_in"] == 0 else \
                   "tomorrow" if e["_in"] == 1 else f"in {e['_in']} days"
            p.append(f"<div class=diary><div class=when>"
                     f"{e['_date'].strftime('%d %b')} · {when}</div><div>"
                     f"<strong>{esc(str(e.get('event','')))}</strong>"
                     f"{'<div class=meta>' + esc(str(e.get('note',''))) + '</div>' if e.get('note') else ''}"
                     f"</div></div>")

    if quiet:
        p.append("<h2>Gone quiet</h2>")
        for q in quiet:
            p.append(f"<div class=quiet><strong>{esc(q['name'])}</strong> — "
                     f"nothing in this window, against a running average of "
                     f"{q['baseline']} items. Worth a look at why.</div>")

    if lag:
        p.append("<h2>Ran first in another language</h2>")
        for r in lag[:12]:
            f, e = r["first"], r["english"]
            p.append(f"<div class=warn>"
                     f"<a href='{esc(f['url'])}'><strong>{esc(f['title'])}</strong></a>"
                     f"<div class=meta>{esc(f.get('source_name',''))} "
                     f"({esc(r['lang'])}) ran {r['hours']}h before English</div>"
                     f"<div class=meta>English: "
                     f"<a href='{esc(e['url'])}'>{esc(e['title'][:90])}</a> — "
                     f"{esc(e.get('source_name',''))}</div></div>")

    for name, items in groups.items():
        p.append(f"<h2>{esc(name)}</h2>")
        if not items:
            p.append("<div class=empty>Nothing in this window.</div>")
        for it in items:
            flags = ""
            if not it.get("verified"):
                flags += "<span class=tag>unverified source</span>"
            if it.get("trust") == "unconfirmed":
                flags += "<span class=tag>unconfirmed</span>"
            also = ""
            outlets = it.get("_outlets", [])
            if outlets:
                shown = ", ".join(outlets[:4])
                more = f" +{len(outlets) - 4}" if len(outlets) > 4 else ""
                also = (f"<div class=also><strong>also in {len(outlets)} "
                        f"other{'s' if len(outlets) > 1 else ''}:</strong> "
                        f"{esc(shown)}{more}</div>")
            p.append(
                f"<div class=item><span class=sc>{it['_score']}</span> "
                f"<a href='{esc(it['url'])}'>{esc(it['title'])}</a>{flags}"
                f"<div class=meta>{esc(it.get('source_name',''))} · "
                f"{esc(it.get('lang',''))} · matched "
                f"{esc(', '.join(it.get('_hits', [])[:4]))}</div>{also}</div>")

    if unverified and unverified[0]:
        n_items, n_src = unverified
        p.append(f"<h2>Housekeeping</h2><div class=quiet>{n_items} item(s) in "
                 f"this window came from {n_src} source(s) still marked "
                 f"<code>verified: false</code> — scored down and barred from "
                 f"leading. Run <code>python scripts/feeds.py --verify "
                 f"--promote</code> to clear them.</div>")

    p.append("</div></body></html>")
    return "".join(p)


# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=12, help="hours to look back")
    ap.add_argument("--mode", choices=["brief", "breaking"], default="brief")
    ap.add_argument("--open", action="store_true", help="open the brief after building")
    args = ap.parse_args()

    settings, watches, diary = load_terms()
    LOCAL.mkdir(exist_ok=True)

    state_path = LOCAL / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    clusters = load_clusters()
    items = load_items(days=max(2, args.window // 24 + 2))
    cutoff = now_utc() - dt.timedelta(hours=args.window)
    window_items = []
    for it in items:
        try:
            if dt.datetime.fromisoformat(it["ingested"]) >= cutoff:
                window_items.append(it)
        except (ValueError, KeyError):
            continue

    for w in watches:
        w["_any"] = compile_terms(w.get("any"))
        w["_all"] = compile_terms(w.get("all"))
        w["_none"] = compile_terms(w.get("none"))
        w.setdefault("slug", re.sub(r"\W+", "-", w["name"].lower()).strip("-"))

    floor = float(settings.get("breaking_score", 6.0)) if args.mode == "breaking" \
        else float(settings.get("brief_score", 0))

    groups, tagged = {}, []
    for w in watches:
        picked = []
        for it in window_items:
            got = matches(w, it)
            if got is None:
                continue
            row = dict(it)
            row["_hits"] = got
            row["_watch"] = w["slug"]
            row["_score"] = score(it, clusters)
            tagged.append(row)
            if row["_score"] >= floor:
                picked.append(row)
        # Collapse: one line per story, not one per outlet. The clusterer has
        # already worked out which reports describe the same event, so show the
        # best-scoring one and name the rest. Ten outlets on the Air India CEO
        # story is one thing you need to know, not ten.
        by_cluster = {}
        for r in picked:
            key = r.get("cluster") or "solo:" + r["fingerprint"]
            by_cluster.setdefault(key, []).append(r)
        collapsed = []
        for rows in by_cluster.values():
            rows.sort(key=lambda r: -r["_score"])
            lead = rows[0]
            lead["_dupes"] = len(rows) - 1
            lead["_outlets"] = []
            for r in rows[1:]:
                name = r.get("source_name", "")
                if name and name != lead.get("source_name") and name not in lead["_outlets"]:
                    lead["_outlets"].append(name)
            collapsed.append(lead)
        # Sort on score alone. score() already adds a corroboration bonus for
        # distinct outlets, so sorting by duplicate count first would count it
        # twice — and that pushes every heavily-syndicated English wire story
        # above a vernacular exclusive, which is backwards for this beat.
        collapsed.sort(key=lambda r: -r["_score"])
        groups[w["name"]] = collapsed[:int(w.get("limit", 15))]

    quiet = []
    if args.mode == "brief":
        for w in watches:
            if not w.get("watch_silence", True):
                continue
            q = silence(w, tagged, state, int(settings.get("silence_days", 3)))
            if q:
                quiet.append(q)

    lag = language_lag(tagged, float(settings.get("lag_min_hours", 6)))
    unver_items = [it for it in window_items if not it.get("verified")]
    unverified = (len(unver_items),
                  len({it.get("source", "?") for it in unver_items}))

    out = LOCAL / ("breaking.html" if args.mode == "breaking" else "brief.html")
    out.write_text(render(groups, lag, quiet,
                          diary_due(diary, int(settings.get("diary_days", 14))),
                          args.window, unverified), encoding="utf-8")
    state["last_run"] = now_utc().isoformat()
    state_path.write_text(json.dumps(state, indent=2))

    total = sum(len(v) for v in groups.values())
    print(f"{total} item(s) across {len(groups)} watch(es) · "
          f"{len(lag)} language-lag · {len(quiet)} gone quiet")
    print(f"written: {out}")
    if args.open:
        webbrowser.open(f"file://{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
