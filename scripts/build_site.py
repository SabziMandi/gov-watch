#!/usr/bin/env python3
"""Build docs/index.html -- the reading surface, published on GitHub Pages.

Reads data/status.json and data/events.jsonl. No database, no build tools.
"""

import datetime as dt
import html
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ist(iso):
    try:
        return dt.datetime.fromisoformat(iso).astimezone(IST)
    except Exception:  # noqa: BLE001
        return None


def ago(when):
    if not when:
        return "—"
    mins = (dt.datetime.now(IST) - when).total_seconds() / 60
    if mins < 60:
        return f"{int(mins)} min ago"
    if mins < 1440:
        return f"{int(mins // 60)} h ago"
    return f"{int(mins // 1440)} d ago"


def load_events(days=14):
    path = DATA / "events.jsonl"
    if not path.exists():
        return []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            when = dt.datetime.fromisoformat(e["ts"])
        except Exception:  # noqa: BLE001
            continue
        if when >= cutoff:
            out.append(e)
    return sorted(out, key=lambda e: e["ts"], reverse=True)


def diff_block(e):
    rows = []
    for line in e.get("sample_removed", [])[:5]:
        rows.append(f'<div class="d out">− {html.escape(line)}</div>')
    for line in e.get("sample_added", [])[:5]:
        rows.append(f'<div class="d in">+ {html.escape(line)}</div>')
    return f'<div class="diff">{"".join(rows)}</div>' if rows else ""


def build():
    status = json.loads((DATA / "status.json").read_text()) if (DATA / "status.json").exists() else {}
    events = load_events()
    changes = [e for e in events if e["type"] == "change"]
    week = [e for e in changes
            if dt.datetime.fromisoformat(e["ts"]) >
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)]
    failing = [s for s in status.values() if s.get("fails", 0) >= 2]
    unverified = [s for s in status.values() if not s.get("checked_url", True)]

    feed = []
    for e in changes[:80]:
        when = ist(e["ts"])
        feed.append(f"""
      <article class="row">
        <div class="rail"><time datetime="{html.escape(e['ts'])}">{when.strftime('%d %b · %H:%M') if when else ''}</time></div>
        <div class="body">
          <h2><a href="{html.escape(e['url'])}">{html.escape(e['name'])}</a></h2>
          <p class="meta">{e.get('added', 0)} lines in · {e.get('removed', 0)} out{
            ' · ' + html.escape(e['notes']) if e.get('notes') else ''}</p>
          {diff_block(e)}
        </div>
      </article>""")

    watch = []
    for slug, s in sorted(status.items(), key=lambda kv: kv[1].get("name", kv[0])):
        state = s.get("state", "—")
        cls = {"changed": "s-change", "error": "s-fail", "baseline": "s-new"}.get(state, "s-ok")
        last = ago(ist(s.get("last_check", "")))
        note = html.escape(s.get("error") or "")
        watch.append(f"""
        <tr>
          <td><a href="{html.escape(s.get('url', '#'))}">{html.escape(s.get('name', slug))}</a></td>
          <td class="mono">{html.escape(s.get('tier', ''))}</td>
          <td class="mono {cls}">{html.escape(state)}</td>
          <td class="mono">{last}</td>
          <td class="mono note">{note}</td>
        </tr>""")

    built = dt.datetime.now(IST).strftime("%d %B %Y, %H:%M IST")

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Gov watch — change log</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #16181d; --dim: #5d6167; --faint: #8b9096;
    --paper: #fbfbf9; --line: #e3e2dd; --panel: #f4f3ef;
    --signal: #a76a12; --fail: #9d2f2f; --ok: #3f6b46;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#e9e8e4; --dim:#a3a29d; --faint:#7c7b77; --paper:#131417;
             --line:#2a2b2f; --panel:#1b1c20; --signal:#d9a441; --fail:#e07b7b; --ok:#7fb387; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink);
    font-family:"Space Grotesk",system-ui,sans-serif; font-size:16px; line-height:1.55; }}
  .wrap {{ max-width: 940px; margin:0 auto; padding: 40px 20px 80px; }}
  header {{ border-bottom:2px solid var(--ink); padding-bottom:14px; margin-bottom:8px; }}
  h1 {{ font-size:26px; font-weight:500; margin:0; letter-spacing:-0.01em; }}
  .built {{ font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--faint); margin:6px 0 0; }}
  .tallies {{ display:flex; gap:28px; flex-wrap:wrap; padding:16px 0 28px;
    border-bottom:1px solid var(--line); font-family:"IBM Plex Mono",monospace; font-size:13px; }}
  .tallies b {{ display:block; font-size:24px; font-weight:500; font-family:"Space Grotesk",sans-serif; }}
  .tallies span {{ color:var(--dim); }}
  h2.sec {{ font-size:13px; font-weight:500; text-transform:none; letter-spacing:0.02em;
    color:var(--dim); font-family:"IBM Plex Mono",monospace; margin:36px 0 4px; }}
  .row {{ display:grid; grid-template-columns:112px 1fr; gap:18px;
    padding:18px 0; border-bottom:1px solid var(--line); }}
  .rail {{ font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--faint);
    padding-top:4px; border-left:2px solid var(--signal); padding-left:10px; }}
  .body h2 {{ font-size:17px; font-weight:500; margin:0 0 2px; }}
  .body h2 a {{ color:inherit; text-decoration:none; border-bottom:1px solid var(--line); }}
  .body h2 a:hover {{ border-color:var(--ink); }}
  .meta {{ font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--dim); margin:0 0 10px; }}
  .diff {{ font-family:"IBM Plex Mono",monospace; font-size:13px; line-height:1.5; }}
  .d {{ padding:2px 8px; white-space:pre-wrap; word-break:break-word; }}
  .d.in {{ color:var(--ok); }}
  .d.out {{ color:var(--fail); }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; margin-top:8px; }}
  th {{ text-align:left; font-weight:500; font-size:12px; color:var(--dim);
    font-family:"IBM Plex Mono",monospace; border-bottom:1px solid var(--line); padding:8px 10px 8px 0; }}
  td {{ padding:8px 10px 8px 0; border-bottom:1px solid var(--line); vertical-align:top; }}
  td a {{ color:inherit; }}
  .mono {{ font-family:"IBM Plex Mono",monospace; font-size:12px; }}
  .note {{ color:var(--dim); max-width:260px; }}
  .s-change {{ color:var(--signal); }} .s-fail {{ color:var(--fail); }}
  .s-ok {{ color:var(--faint); }} .s-new {{ color:var(--dim); }}
  .warn {{ background:var(--panel); padding:14px 16px; margin:20px 0 0; font-size:14px; }}
  .empty {{ color:var(--dim); padding:24px 0; }}
  a:focus-visible, h2 a:focus-visible {{ outline:2px solid var(--signal); outline-offset:2px; }}
  @media (max-width:620px) {{ .row {{ grid-template-columns:1fr; gap:6px; }}
    .rail {{ border-left:none; border-top:2px solid var(--signal); padding:6px 0 0; }}
    .note {{ display:none; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Gov watch</h1>
  </header>
  <p class="built">Built {built}. Snapshots and full history live in the repository.</p>

  <div class="tallies">
    <div><b>{len(status)}</b><span>pages watched</span></div>
    <div><b>{len(week)}</b><span>changes, 7 days</span></div>
    <div><b>{len(failing)}</b><span>failing fetches</span></div>
    <div><b>{len(unverified)}</b><span>URLs unconfirmed</span></div>
  </div>

  {'<div class="warn">Some pages have failed repeatedly. A failing fetch is not the same as an unchanged page — check those by hand before relying on silence.</div>' if failing else ''}

  <h2 class="sec">Changes, last 14 days</h2>
  {"".join(feed) if feed else '<p class="empty">Nothing yet. The first run only records baselines.</p>'}

  <h2 class="sec">Watchlist</h2>
  <table>
    <thead><tr><th>Page</th><th>Tier</th><th>State</th><th>Last check</th><th>Note</th></tr></thead>
    <tbody>{"".join(watch)}</tbody>
  </table>
</div>
</body>
</html>"""

    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(doc, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"wrote docs/index.html ({len(doc):,} bytes)")


if __name__ == "__main__":
    build()
