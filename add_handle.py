"""Add a player handle from an "Add player handle" GitHub issue (or by hand).

  py add_handle.py --issue-body-file body.md      # what the workflow runs
  py add_handle.py etbu "John Doe" @johndoe [https://x.com/johndoe/status/123]

Appends to data/handles_manual.csv, so every later search reads that account.
If a portal post link is given, also adds the player to data/overrides.csv as
"confirmed", so they show on the tracker right away even if the post is old or
image-only. Prints one line describing what happened (the workflow posts it as
an issue comment). Exits 1 on bad input.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HANDLES = ROOT / "data" / "handles_manual.csv"
OVERRIDES = ROOT / "data" / "overrides.csv"
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
POST_RE = re.compile(r"^https://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")


def parse_issue(body: str) -> dict[str, str]:
    """Issue forms render as '### Label\\n\\nvalue' sections."""
    out = {}
    for part in re.split(r"^### ", body, flags=re.M)[1:]:
        label, _, value = part.partition("\n")
        value = value.strip()
        out[label.strip().lower()] = "" if value == "_No response_" else value
    return {
        "school": out.get("school", ""),
        "name": out.get("player name", ""),
        "handle": out.get("x handle", ""),
        "url": out.get("portal post link (optional)", ""),
    }


def add(school: str, name: str, handle: str, url: str = "") -> str:
    schools = {s["id"]: s["name"] for s in json.loads((ROOT / "schools.json").read_text(encoding="utf-8"))["schools"]}
    sid = school.split()[0].strip().lower() if school else ""  # dropdown values start with the id: "etbu - East Texas Baptist"
    name = " ".join(name.split())
    handle = handle.strip().lstrip("@")
    m = re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", handle)
    if m:  # a profile link pasted instead of a handle
        handle = m.group(1)
    url = url.strip().split("?")[0]

    if sid not in schools:
        raise ValueError(f"Unknown school {school!r}; expected one of: {', '.join(schools)}")
    if not name or len(name) > 60 or not re.search(r"[A-Za-z]", name):
        raise ValueError("Player name is missing or doesn't look like a name.")
    if not HANDLE_RE.match(handle):
        raise ValueError(f"{handle!r} isn't a valid X handle (letters, numbers, underscores, max 15).")
    if url and not POST_RE.match(url):
        raise ValueError(f"{url!r} isn't an X post link like https://x.com/user/status/123.")

    msgs = []
    with HANDLES.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    dup = next((r for r in rows if r["x_handle"].lstrip("@").lower() == handle.lower()), None)
    if dup:
        msgs.append(f"@{handle} was already listed ({dup['name']}, {dup['school_id']}); no change.")
    else:
        with HANDLES.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([sid, name, handle, "", "added via issue"])
        msgs.append(f"Added @{handle} for {name} ({schools[sid]}). Searches will read this account from the next run.")

    if url:
        with OVERRIDES.open(newline="", encoding="utf-8") as f:
            ov = list(csv.DictReader(f))
        if any(r["school_id"] == sid and r["name"].lower() == name.lower() for r in ov):
            msgs.append(f"{name} was already in overrides.csv; left as is.")
        else:
            with OVERRIDES.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([sid, name, "confirmed", "added via issue", url])
            msgs.append(f"Marked {name} as In portal on the tracker, linked to {url}.")
    return " ".join(msgs)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-body-file")
    ap.add_argument("args", nargs="*", help="school_id name handle [post_url]")
    a = ap.parse_args()
    try:
        if a.issue_body_file:
            f = parse_issue(Path(a.issue_body_file).read_text(encoding="utf-8"))
            print(add(f["school"], f["name"], f["handle"], f["url"]))
        elif len(a.args) in (3, 4):
            print(add(*a.args))
        else:
            ap.error("give --issue-body-file, or: school_id name handle [post_url]")
    except ValueError as e:
        print(f"Couldn't add this: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
