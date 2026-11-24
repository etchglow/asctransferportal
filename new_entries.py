"""Compare two tracker.csv files and write a summary of what's new.

  py new_entries.py OLD_TRACKER.csv data/tracker.csv OUT.md

Writes OUT.md only when there's something to report: players new to the tracker,
or players who moved from Likely to In portal. The daily workflow posts OUT.md as
a GitHub issue, which GitHub emails to you.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path


def load(path: str) -> dict[tuple[str, str], dict]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(newline="", encoding="utf-8") as f:
        return {(r["school"], r["name"].lower()): r for r in csv.DictReader(f)}


def line(r: dict) -> str:
    bits = [b for b in (r["position"], r["class_year"]) if b]
    handle = ", ".join(f"[@{h.strip()}](https://x.com/{h.strip()})" for h in r["x_handle"].split(",") if h.strip())
    post = f"[post]({r['evidence']})" if r["evidence"] else ""
    extra = " · ".join(x for x in (" ".join(bits), handle, post) if x)
    return f"- **{r['name']}**, {r['school']} — {r['status']}" + (f" · {extra}" if extra else "")


def main() -> int:
    old_path, new_path, out_path = sys.argv[1:4]
    old, new = load(old_path), load(new_path)
    added = [r for k, r in new.items() if k not in old]
    upgraded = [r for k, r in new.items()
                if k in old and old[k]["status"] == "Likely" and r["status"] == "In portal"]
    if not added and not upgraded:
        print("Nothing new.")
        return 0
    parts = []
    if added:
        parts.append(f"### New on the tracker ({len(added)})\n" + "\n".join(line(r) for r in added))
    if upgraded:
        parts.append(f"### Now confirmed In portal ({len(upgraded)})\n" + "\n".join(line(r) for r in upgraded))
    Path(out_path).write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    print(f"{len(added)} new, {len(upgraded)} upgraded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
