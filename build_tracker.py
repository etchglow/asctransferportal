"""Turn saved posts into the tracker: one row per player.

  py build_tracker.py
Writes data/tracker.csv and docs/index.html (the GitHub Pages site). No API calls.

Status per player:
  In portal   at least one post uses portal wording ("strong")
  Likely      only recruiting wording ("Recruitment 100% open", "years of eligibility")
data/overrides.csv lets you change a status or add players the search can't find.
  status = confirmed | not_portal | committed     (note = new school for committed)
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
from pathlib import Path

from x_search import DATA, POSTS, load_watchlist, redact

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
OVERRIDES = DATA / "overrides.csv"
FIELDS = ["status", "school", "name", "position", "class_year", "first_seen", "x_handle", "evidence", "posts", "note"]


def parse_player(s: str) -> tuple[str, str]:
    """'Kai Bonner (etbu, FS)' -> ('Kai Bonner', 'etbu')."""
    m = re.match(r"(.+?) \((\w+),", s)
    return (m.group(1), m.group(2)) if m else (s, "")


def load_overrides() -> list[dict]:
    if not OVERRIDES.exists():
        with OVERRIDES.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["school_id", "name", "status", "note", "url"])
        return []
    with OVERRIDES.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("name")]


def build() -> list[dict]:
    schools = {s["id"]: s["name"] for s in json.loads((ROOT / "schools.json").read_text(encoding="utf-8"))["schools"]}
    roster = {(p["school_id"], p["name"].lower()): p for p in load_watchlist()}
    posts = [json.loads(l) for l in POSTS.read_text(encoding="utf-8").splitlines() if l.strip()] if POSTS.exists() else []

    players: dict[tuple[str, str], dict] = {}
    for post in posts:
        names = [parse_player(p) for p in post.get("players", [])]
        if not names:  # portal post from someone not on a roster: keep under their X name
            names = [(post.get("author_name") or post.get("username") or "?", post["school_id"])]
        signal = post.get("signal", "strong")  # x_search only saves posts with portal wording
        for name, sid in names:
            sid = sid or post["school_id"]
            key = (sid, name.lower())
            row = players.setdefault(key, {"school_id": sid, "name": name, "strong": False, "posts": [], "handles": set()})
            row["strong"] |= signal == "strong"
            row["posts"].append(post)
            # Only the player's own account counts as their handle, not agencies or teammates.
            if post.get("username") and name.lower().split()[-1] in (post.get("author_name") or "").lower():
                row["handles"].add(post["username"])

    for o in load_overrides():
        key = (o["school_id"], o["name"].lower())
        row = players.setdefault(key, {"school_id": o["school_id"], "name": o["name"], "strong": False, "posts": [], "handles": set()})
        row["override"] = o

    out = []
    for (sid, lname), r in players.items():
        o = r.get("override", {})
        status = {"confirmed": "In portal", "not_portal": "", "committed": "Committed"}.get(o.get("status", ""), None)
        if status is None:
            status = "In portal" if r["strong"] else "Likely"
        if not status:
            continue  # you marked it as not a portal entry
        ps = sorted(r["posts"], key=lambda p: p.get("created_at", ""))
        p = roster.get((sid, lname), {})
        own = [x for x in ps if x.get("username") in r["handles"]] or ps
        out.append({
            "status": status,
            "school": schools.get(sid, sid),
            "name": r["name"],
            "position": p.get("position", ""),
            "class_year": p.get("class_year", "") or ("not on roster" if not p else ""),
            "first_seen": (ps[0].get("created_at", "")[:10] if ps else ""),
            "x_handle": ", ".join(sorted(r["handles"])) or p.get("x_handle", ""),
            "evidence": own[0]["url"] if own else o.get("url", ""),
            "evidence_text": redact(own[0]["text"])[:220] if own else "",
            "posts": len(ps),
            "note": o.get("note", ""),
        })
    out.sort(key=lambda r: r["first_seen"] or "0000", reverse=True)  # newest first
    return out


def write_csv(rows: list[dict]) -> None:
    with (DATA / "tracker.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_html(rows: list[dict]) -> None:
    DOCS.mkdir(exist_ok=True)
    e = html.escape
    schools = sorted({r["school"] for r in rows})
    counts = {s: sum(1 for r in rows if r["school"] == s) for s in schools}
    chips = "".join(f'<button data-s="{e(s)}">{e(s)} <span>{counts[s]}</span></button>' for s in schools)
    body = []
    for r in rows:
        cls = {"In portal": "in", "Likely": "likely", "Committed": "done"}.get(r["status"], "")
        handle = " ".join(f'<a href="https://x.com/{e(h.strip())}">@{e(h.strip())}</a>' for h in r["x_handle"].split(",") if h.strip())
        quote = f'<div class="q">{e(r["evidence_text"])}</div>' if r["evidence_text"] else ""
        link = f'<a href="{e(r["evidence"])}">post</a>' if r["evidence"] else ""
        body.append(
            f'<tr data-s="{e(r["school"])}"><td><span class="st {cls}">{e(r["status"])}</span></td>'
            f'<td><b>{e(r["name"])}</b><div class="sub">{e(r["position"])} · {e(r["class_year"])} {handle}</div>{quote}</td>'
            f'<td>{e(r["school"])}</td><td class="num">{e(r["first_seen"])}</td>'
            f'<td>{link}{(" · " + e(r["note"])) if r["note"] else ""}</td></tr>')
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_in = sum(r["status"] == "In portal" for r in rows)
    n_likely = sum(r["status"] == "Likely" for r in rows)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASC Football Transfer Portal Tracker</title>
<style>
:root{{--bg:#fbfaf8;--fg:#1d1c1a;--mut:#6b6862;--line:#e6e3dd;--card:#fff;--in:#b4441c;--likely:#a07a12;--done:#2f6f4f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161514;--fg:#ecebe8;--mut:#9c9891;--line:#2c2a27;--card:#1e1d1b;--in:#f08a5d;--likely:#e3c15a;--done:#7cc4a0}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1040px;margin:0 auto;padding:32px 20px 60px}}h1{{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}}
.lede{{color:var(--mut);margin:0 0 20px}}.stats{{display:flex;gap:28px;margin:0 0 20px}}.stats b{{display:block;font-size:28px;font-variant-numeric:tabular-nums}}
.stats span{{color:var(--mut);font-size:13px}}.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}}
button{{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:5px 12px;cursor:pointer}}
button span{{color:var(--mut)}}button.on{{border-color:var(--fg)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:12px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
tr:last-child td{{border-bottom:0}}.sub{{color:var(--mut);font-size:13px}}.q{{color:var(--mut);font-size:13px;margin-top:6px;max-width:520px}}
.num{{font-variant-numeric:tabular-nums;white-space:nowrap}}a{{color:inherit}}
.st{{font-size:12px;font-weight:600;white-space:nowrap}}.st.in{{color:var(--in)}}.st.likely{{color:var(--likely)}}.st.done{{color:var(--done)}}
footer{{color:var(--mut);font-size:13px;margin-top:20px}}
@media (max-width:640px){{th:nth-child(3),td:nth-child(3),th:nth-child(4),td:nth-child(4){{display:none}}}}
</style></head><body><main>
<h1>ASC Football Transfer Portal Tracker</h1>
<p class="lede">American Southwest Conference football players who have announced a transfer-portal entry on X.</p>
<div class="stats"><div><b>{n_in}</b><span>In portal</span></div><div><b>{n_likely}</b><span>Likely (recruiting post)</span></div><div><b>{len(rows)}</b><span>Total</span></div></div>
<div class="chips"><button class="on" data-s="">All <span>{len(rows)}</span></button>{chips}</div>
<table><thead><tr><th>Status</th><th>Player</th><th>School</th><th>Seen</th><th>Source</th></tr></thead>
<tbody>{"".join(body)}</tbody></table>
<footer>Updated {updated}. "Likely" means the player's post used recruiting language (e.g. "recruitment open") but didn't say "portal". Phone numbers and emails are removed from quoted posts.</footer>
</main>
<script>
document.querySelectorAll('.chips button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('.chips button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('tbody tr').forEach(r=>r.hidden=b.dataset.s&&r.dataset.s!==b.dataset.s);
}});
</script></body></html>"""
    (DOCS / "index.html").write_text(page, encoding="utf-8")


def main() -> None:
    rows = build()
    write_csv(rows)
    write_html(rows)
    by = {}
    for r in rows:
        by.setdefault(r["status"], 0)
        by[r["status"]] += 1
    print(f"{len(rows)} players: " + ", ".join(f"{v} {k}" for k, v in by.items()))


if __name__ == "__main__":
    main()
