"""Find portal announcements through teammates' reposts.

Reads recent posts from every known player account at a school: their own posts plus
what they reposted or quoted. A post counts when it looks like a portal announcement
and its author is on an ASC roster. This catches posts text search misses: no school
name, or only an image with "Recruitment 110% open".

  py teammate_scan.py                         # all schools; each account since its last scan
  py teammate_scan.py --school hsu --since 2026-11-01 --max-per-user 30
  py teammate_scan.py --dry-run               # list the accounts it would read

Signal strength:
  strong  - portal wording ("transfer portal", "entering the portal")
  weak    - recruiting wording ("recruitment open", "years of eligibility", "NCAA ID",
            "DMs open"). Counted only when the author is on a roster, since high-school
            recruits post the same phrases.
New hits go to data/x_posts.jsonl. Handles of roster-matched authors go to handles_manual.csv.

Image OCR: many announcements are a graphic with no wording in the post itself. When
Tesseract is installed (the GitHub Action installs it), photos are read with OCR for
posts with no portal wording whose author is a rostered player or names the school in
their bio. Without Tesseract this step is skipped.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from x_search import DATA, POSTS, call_api, load_watchlist, redact

STATE = DATA / "teammate_state.json"
STRONG = re.compile(
    r"transfer portal|#transferportal|\b(enter(ed|ing)?|in|into) (my name )?(in(to)? )?the portal\b", re.I)
WEAK = re.compile(
    r"recruitment\s+(is\s+)?(\d+\s*%\s*)?(officially\s+)?open|recruiting\s+is\s+open"
    r"|\d\s*(\+\s*)?(yrs?|years?)\s+(of\s+)?eligibility|eligibility\s+(remaining|left)"
    r"|ncaa\s*(id|#)|dms?\s+(are\s+)?open|uncommitted|continue (my|playing).{0,40}(career|next level)",
    re.I)


def find_tesseract() -> str | None:
    return shutil.which("tesseract") or next(
        (p for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",) if os.path.exists(p)), None)


def ocr_image(url: str, exe: str) -> str:
    """Download an X photo and return its text. Only pbs.twimg.com images are fetched."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != "pbs.twimg.com":
        return ""
    fd, path = tempfile.mkstemp(suffix=os.path.splitext(u.path)[1] or ".jpg")
    os.close(fd)
    try:
        req = urllib.request.Request(f"{url}?name=large", headers={"User-Agent": "asc-portal-monitor/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read(10_000_000))
        out = subprocess.run([exe, path, "stdout"], capture_output=True, text=True, timeout=60)
        return " ".join(out.stdout.split())
    except Exception as e:  # a failed image shouldn't stop the scan
        print(f"  OCR failed for {url}: {e}")
        return ""
    finally:
        os.remove(path)


def norm_words(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", re.sub(r"['’`]", "", s.lower())))


class Rosters:
    """Matches an X account to an ASC player: by known handle first, then by name."""

    def __init__(self) -> None:
        self.players = load_watchlist()
        self.by_handle: dict[str, dict] = {}
        for p in self.players:
            if p["x_handle"]:
                self.by_handle[p["x_handle"].lower()] = p
        manual = DATA / "handles_manual.csv"
        if manual.exists():
            with manual.open(newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if not r.get("x_handle"):
                        continue
                    hit = next((p for p in self.players if p["school_id"] == r["school_id"]
                                and p["name"].lower() == r["name"].lower()), None)
                    # Players you added who aren't on a roster (e.g. Davian Arthur) still count.
                    self.by_handle[r["x_handle"].lower().lstrip("@")] = hit or {
                        "school_id": r["school_id"], "name": r["name"], "position": "?", "x_handle": r["x_handle"]}

    def match(self, user: dict) -> list[dict]:
        p = self.by_handle.get(user.get("username", "").lower())
        if p:
            return [p]
        words = norm_words(user.get("name", ""))
        out = []
        for p in self.players:
            parts = re.findall(r"[a-z]+", re.sub(r"['’`]", "", p["name"].lower()))
            if len(parts) > 1 and parts[0] in words and parts[-1] in words:
                out.append(p)
        return out

    def handles_for(self, school_id: str) -> list[str]:
        return sorted({h for h, p in self.by_handle.items() if p["school_id"] == school_id})


def lookup_ids(handles: list[str], cache: dict) -> dict[str, str]:
    missing = [h for h in handles if h not in cache]
    for i in range(0, len(missing), 100):
        res = call_api("users/by", {"usernames": ",".join(missing[i:i + 100])})
        for u in res.get("data", []):
            cache[u["username"].lower()] = u["id"]
    return {h: cache[h] for h in handles if h in cache}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--school", help="school id; default all schools")
    ap.add_argument("--since", help="YYYY-MM-DD for accounts never scanned (default: 14 days ago)")
    ap.add_argument("--max-per-user", type=int, default=30, help="posts per account per run (cost cap)")
    ap.add_argument("--rescan", action="store_true", help="read every account from --since again, ignoring where the last scan stopped")
    ap.add_argument("--no-ocr", action="store_true", help="skip reading text from images")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tess = None if a.no_ocr else find_tesseract()
    print(f"Image OCR: {'on' if tess else 'off (Tesseract not installed)' if not a.no_ocr else 'off'}")

    schools = json.loads((DATA.parent / "schools.json").read_text(encoding="utf-8"))["schools"]
    if a.school:
        schools = [s for s in schools if s["id"] == a.school]
    since = a.since or (dt.date.today() - dt.timedelta(days=14)).isoformat()
    rosters = Rosters()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"ids": {}, "since_id": {}}
    seen = set()
    if POSTS.exists():
        seen = {json.loads(l)["id"] for l in POSTS.read_text(encoding="utf-8").splitlines() if l.strip()}

    plan = {s["id"]: rosters.handles_for(s["id"]) for s in schools}
    for sid, hs in plan.items():
        print(f"[{sid}] {len(hs)} known player accounts")
    if a.dry_run:
        return 0

    found: dict[str, dict] = {}
    read = ocr_count = 0
    for s in schools:
        ids = lookup_ids(plan[s["id"]], state["ids"])
        for handle, uid in ids.items():
            params = {
                "max_results": max(5, min(100, a.max_per_user)),
                "expansions": "author_id,referenced_tweets.id.author_id,attachments.media_keys,"
                              "referenced_tweets.id.attachments.media_keys",
                "tweet.fields": "created_at,referenced_tweets,note_tweet,author_id,attachments",
                "user.fields": "name,username,description",
                "media.fields": "type,url",
            }
            if state["since_id"].get(handle) and not a.rescan:
                params["since_id"] = state["since_id"][handle]
            else:
                params["start_time"] = f"{since}T00:00:00Z"
            res = call_api(f"users/{uid}/tweets", params)
            own = res.get("data", [])
            read += len(own)
            if own:
                newest = max((t["id"] for t in own), key=int)
                if int(newest) > int(state["since_id"].get(handle) or 0):
                    state["since_id"][handle] = newest
            users = {u["id"]: u for u in res.get("includes", {}).get("users", [])}
            media = {m["media_key"]: m for m in res.get("includes", {}).get("media", [])}
            # Candidates: the player's own original posts, plus every post they reposted or quoted.
            cands = [t for t in own if not t.get("referenced_tweets")] + res.get("includes", {}).get("tweets", [])
            for t in cands:
                if t["id"] in seen or t["id"] in found:
                    continue
                text = t.get("note_tweet", {}).get("text") or t.get("text", "")
                author = users.get(t.get("author_id"), {})
                players = rosters.match(author)
                tagged = False
                if not players:  # a news/edits account: credit the known players it @-mentions
                    players = [rosters.by_handle[h.lower()] for h in dict.fromkeys(re.findall(r"@(\w{1,15})", text))
                               if h.lower() in rosters.by_handle]
                    tagged = bool(players)
                if len(players) > 1 and not tagged:  # same name at two schools: prefer this school
                    players = [p for p in players if p["school_id"] == s["id"]] or players
                bio = (author.get("description", "") + " " + text).lower()
                tied = bool(players) or any(x.lower() in bio for x in s["aliases"])
                strong, weak = bool(STRONG.search(text)), bool(WEAK.search(text))
                via_ocr = False
                if not (strong or weak) and tess and tied:
                    # No wording in the post: read any photos, which is where the graphic says "portal".
                    photos = [media[k]["url"] for k in t.get("attachments", {}).get("media_keys", [])
                              if k in media and media[k].get("type") == "photo" and media[k].get("url")]
                    img_text = " ".join(ocr_image(u, tess) for u in photos[:4])
                    ocr_count += len(photos[:4])
                    if img_text and (STRONG.search(img_text) or WEAK.search(img_text)):
                        strong, weak = bool(STRONG.search(img_text)), bool(WEAK.search(img_text))
                        text = f"{text}\n[image text] {img_text[:400]}"
                        via_ocr = True
                if not (strong or weak):
                    continue
                if not players and not strong:
                    continue  # weak wording from an unknown account: probably a recruit
                if not tied:
                    continue  # portal post from someone with no visible tie to this school
                found[t["id"]] = {
                    "id": t["id"],
                    "school_id": players[0]["school_id"] if players else s["id"],
                    "created_at": t.get("created_at", ""),
                    "username": author.get("username"), "author_name": author.get("name"),
                    "text": redact(text), "url": f'https://x.com/{author.get("username", "i")}/status/{t["id"]}',
                    "players": [f'{p["name"]} ({p["school_id"]}, {p["position"]})' for p in players],
                    "found_via": (f"teammate:@{handle}" if t.get("author_id") != uid else "timeline") + (" +ocr" if via_ocr else ""),
                    "signal": "strong" if strong else "weak",
                    **({"tagged": True} if tagged else {}),  # author isn't the player; don't save their handle
                }

    # Save new posts, and the handles of roster-matched authors.
    new_handles = []
    known = set(rosters.by_handle)
    for r in found.values():
        u = (r["username"] or "").lower()
        if r["players"] and u and u not in known and len(r["players"]) == 1 and not r.get("tagged"):
            name = r["players"][0].split(" (")[0]
            new_handles.append([r["school_id"], name, r["username"], "", f"auto: teammate_scan {dt.date.today()}"])
            known.add(u)
    with POSTS.open("a", encoding="utf-8") as f:
        for r in found.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if new_handles:
        with (DATA / "handles_manual.csv").open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(new_handles)
    STATE.write_text(json.dumps(state, indent=1))

    print(f"\n{read} posts read, {ocr_count} images OCR'd, {len(found)} new portal-like posts, {len(new_handles)} new handles saved")
    for r in sorted(found.values(), key=lambda r: (r["school_id"], r["created_at"])):
        who = ", ".join(r["players"]) or "not on roster (school named in bio/post)"
        print(f'\n[{r["school_id"]}] {r["created_at"][:10]} {r["signal"]:<6} @{r["username"]} -> {who}  ({r["found_via"]})')
        print(f'  {r["text"][:180]!r}\n  {r["url"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
