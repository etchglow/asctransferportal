"""Build the ASC football watchlist from each school's roster page.

Usage:  python build_watchlist.py
Output: data/watchlist.csv

Standard library only. Reads schools.json and writes one row per player.
Hand-found X/Instagram handles go in data/handles_manual.csv. They are merged
in on every run, so a re-scrape never wipes them out.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (compatible; asc-portal-monitor/0.1; personal fan project)"

FIELDS = [
    "school_id", "school", "name", "jersey", "position", "class_year",
    "hometown", "high_school", "x_handle", "instagram", "handle_source", "bio_url",
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def clean(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return clean(m.group(1)) if m else ""


def socials(block: str) -> tuple[str, str]:
    """Return (x_handle, instagram) found in a chunk of HTML."""
    x = re.search(r"(?:twitter|x)\.com/(?!intent|share|home|widgets)@?([A-Za-z0-9_]{1,15})\b", block)
    ig = re.search(r"instagram\.com/([A-Za-z0-9_.]{1,30})", block)
    return (x.group(1) if x else "", ig.group(1).rstrip(".") if ig else "")


def parse_sidearm(page: str, base: str) -> list[dict]:
    players = []
    # Each player is an <li class="sidearm-roster-player ..."> block; coaches are in a separate list.
    chunks = re.split(r'<li class="sidearm-roster-player[ "]', page)[1:]
    for c in chunks:
        c = c.split('<li class="sidearm-roster-coach', 1)[0]
        name = first(r'aria-label="[^"]*View Full Bio"[^>]*>(.*?)</a>', c) or first(
            r'sidearm-roster-player-name.*?<a[^>]*>(.*?)</a>', c)
        if not name:
            continue
        x, ig = socials(first_raw(r'sidearm-roster-player-social[^>]*>(.*?)</div>', c))
        href = re.search(r'href="(/sports/football/roster/[^"]+)"', c)
        players.append({
            "name": name,
            "jersey": first(r'sidearm-roster-player-jersey-number">(.*?)</span>', c),
            "position": first(r'sidearm-roster-player-position">\s*<span[^>]*>(.*?)</span>', c),
            "class_year": first(r'sidearm-roster-player-academic-year[^"]*">(.*?)</span>', c),
            "hometown": first(r'sidearm-roster-player-hometown">(.*?)</span>', c),
            "high_school": first(r'sidearm-roster-player-highschool">(.*?)</span>', c),
            "x_handle": x,
            "instagram": ig,
            "bio_url": urljoin(base, href.group(1)) if href else "",
        })
    return players


def first_raw(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else ""


def parse_presto(page: str, base: str) -> list[dict]:
    players = []
    body = first_raw(r"<tbody>(.*?)</tbody>", page)
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        a = re.search(r'<a[^>]*href="([^"]*/bios/[^"]+)"[^>]*>(.*?)</a>', row, re.S)
        if not a:
            continue
        cells = [clean(re.sub(r'<span class="label">.*?</span>', "", td, flags=re.S))
                 for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        # cells: [number, position, class, hometown/high school]
        home, _, hs = (cells[3] if len(cells) > 3 else "").partition(" / ")
        x, ig = socials(first_raw(r'social-links-inline(.*?)</ul>', row))
        players.append({
            "name": clean(a.group(2)),
            "jersey": cells[0] if cells else "",
            "position": cells[1] if len(cells) > 1 else "",
            "class_year": cells[2] if len(cells) > 2 else "",
            "hometown": home,
            "high_school": hs,
            "x_handle": x,
            "instagram": ig,
            "bio_url": urljoin(base, a.group(1)),
        })
    return players


PARSERS = {"sidearm": parse_sidearm, "presto": parse_presto}

CLASS_MAP = {"fr": "Fr.", "so": "So.", "jr": "Jr.", "sr": "Sr.", "gr": "Gr."}


def norm_class(raw: str) -> str:
    """'FR' / 'Fr' / 'Fr.' -> 'Fr.'; keeps a redshirt prefix ('R-Jr.')."""
    m = re.match(r"(?i)(r-?|rs-?)?\s*(fr|so|jr|sr|gr)", raw.strip())
    if not m:
        return raw
    return ("R-" if m.group(1) else "") + CLASS_MAP[m.group(2).lower()]


def load_manual() -> dict[tuple[str, str], dict]:
    path = DATA / "handles_manual.csv"
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["school_id", "name", "x_handle", "instagram", "note"])
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {(r["school_id"], r["name"].lower()): r for r in csv.DictReader(f)}


def main() -> int:
    DATA.mkdir(exist_ok=True)
    schools = json.loads((ROOT / "schools.json").read_text(encoding="utf-8"))["schools"]
    manual = load_manual()
    rows: list[dict] = []
    for s in schools:
        try:
            page = fetch(s["roster_url"])
        except Exception as e:  # keep going if one school's site is down
            print(f"  ! {s['name']}: {e}", file=sys.stderr)
            continue
        players = PARSERS[s["platform"]](page, s["roster_url"])
        for p in players:
            p["class_year"] = norm_class(p["class_year"])
            p["handle_source"] = "roster" if (p["x_handle"] or p["instagram"]) else ""
            m = manual.get((s["id"], p["name"].lower()))
            if m:
                p["x_handle"] = m.get("x_handle") or p["x_handle"]
                p["instagram"] = m.get("instagram") or p["instagram"]
                p["handle_source"] = "manual"
            rows.append({"school_id": s["id"], "school": s["name"], **p})
        nx = sum(1 for p in players if p["x_handle"])
        nig = sum(1 for p in players if p["instagram"])
        print(f"{s['name']:<20} {len(players):>4} players  {nx:>3} X  {nig:>3} IG")
        time.sleep(1)  # be polite to the athletics sites

    out = DATA / "watchlist.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} players to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
