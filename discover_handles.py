"""Find player X handles through teammates' reposts.

Teammates repost each other's announcements, so reposts link player accounts together.

  py discover_handles.py --retweeters 2123290991515840778 --school etbu
      Who reposted this post. Usually teammates.
  py discover_handles.py --reposts-of Raythegoat6 --school etbu --max-posts 100
      Whose posts this player reposted. Usually teammates.
  add --save to append roster matches to data/handles_manual.csv

A match means the account's display name contains the player's first and last name.
Treat it as a candidate. --save only adds players who don't already have a handle.
Billing is per post or user returned, so keep --max-posts small.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys

from x_search import DATA, call_api, load_watchlist, player_handles


def name_match(display: str, roster: list[dict]) -> list[dict]:
    words = set(re.findall(r"[a-z]+", re.sub(r"['’`]", "", display.lower())))
    out = []
    for p in roster:
        parts = re.findall(r"[a-z]+", re.sub(r"['’`]", "", p["name"].lower()))
        if len(parts) > 1 and parts[0] in words and parts[-1] in words:
            out.append(p)
    return out


def retweeters(post_id: str, n: int) -> list[dict]:
    res = call_api(f"tweets/{post_id}/retweeted_by", {"max_results": min(100, n), "user.fields": "name,username,description"})
    return res.get("data", [])


def reposted_authors(handle: str, n: int) -> list[dict]:
    uid = call_api(f"users/by/username/{handle}", {})["data"]["id"]
    res = call_api(f"users/{uid}/tweets", {
        "max_results": max(5, min(100, n)),
        "expansions": "referenced_tweets.id.author_id",
        "user.fields": "name,username,description",
        "tweet.fields": "referenced_tweets",
    })
    users = {u["id"]: u for u in res.get("includes", {}).get("users", [])}
    refs = {t["id"]: t for t in res.get("includes", {}).get("tweets", [])}
    authors = {}
    for t in res.get("data", []):
        for r in t.get("referenced_tweets", []):
            if r["type"] in ("retweeted", "quoted") and r["id"] in refs:
                a = users.get(refs[r["id"]].get("author_id"))
                if a and a["username"].lower() != handle.lower():
                    authors[a["id"]] = a
    print(f"{len(res.get('data', []))} posts read from @{handle}, {len(authors)} distinct accounts reposted/quoted")
    return list(authors.values())


PORTAL_RE = re.compile(r"transfer portal|(enter|entered|entering|in) (my name into )?the portal|#transferportal", re.I)


def portal_scan(post_id: str, since: str, per_user: int, school: dict, roster: list[dict]) -> list[dict]:
    """Read posts since `since` from everyone who reposted `post_id`. Return portal posts
    they reposted or quoted, with the author matched to the roster or to the school
    named in the author's bio."""
    aliases = [a.lower() for a in school["aliases"]] + [h.lower() for h in school.get("team_x", [])]
    found: dict[str, dict] = {}
    users = retweeters(post_id, 100)
    print(f"{len(users)} reposters of {post_id}; reading their posts since {since}")
    read = 0
    for u in users:
        res = call_api(f"users/{u['id']}/tweets", {
            "max_results": max(5, min(100, per_user)), "start_time": f"{since}T00:00:00Z",
            "expansions": "referenced_tweets.id.author_id", "tweet.fields": "referenced_tweets,created_at,note_tweet",
            "user.fields": "name,username,description",
        })
        read += len(res.get("data", []))
        authors = {a["id"]: a for a in res.get("includes", {}).get("users", [])}
        for t in res.get("includes", {}).get("tweets", []):
            text = t.get("note_tweet", {}).get("text") or t.get("text", "")
            if t["id"] in found or t["id"] == post_id or not PORTAL_RE.search(text):
                continue
            a = authors.get(t.get("author_id"), {})
            m = name_match(a.get("name", ""), roster)
            bio = (a.get("description", "") + " " + text).lower()
            school_hit = any(x in bio for x in aliases)
            if m or school_hit:
                found[t["id"]] = {"id": t["id"], "author": a, "text": text, "players": m,
                                  "created_at": t.get("created_at", ""), "via": u["username"]}
    print(f"{read} posts read in total\n")
    return list(found.values())


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--retweeters", metavar="POST_ID")
    g.add_argument("--reposts-of", metavar="HANDLE")
    g.add_argument("--portal-scan", metavar="POST_ID",
                   help="find other portal posts reposted by the people who reposted this one")
    ap.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=30)).isoformat(), help="YYYY-MM-DD, for --portal-scan")
    ap.add_argument("--school", required=True, help="school id from schools.json")
    ap.add_argument("--max-posts", type=int, default=100)
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()

    roster = [p for p in load_watchlist() if p["school_id"] == a.school]
    known = {h.lower() for h in player_handles(load_watchlist()).get(a.school, [])}
    have_handle = {p["name"].lower() for p in roster if p["x_handle"]}
    manual_path = DATA / "handles_manual.csv"
    if manual_path.exists():
        with manual_path.open(newline="", encoding="utf-8") as f:
            have_handle |= {r["name"].lower() for r in csv.DictReader(f) if r["school_id"] == a.school and r["x_handle"]}

    if a.portal_scan:
        import json
        school = next(s for s in json.loads((DATA.parent / "schools.json").read_text(encoding="utf-8"))["schools"] if s["id"] == a.school)
        hits = portal_scan(a.portal_scan, a.since, a.max_posts, school, roster)
        rows = []
        for h in sorted(hits, key=lambda h: h["created_at"]):
            au = h["author"]
            who = ", ".join(f'{p["name"]} ({p["position"]})' for p in h["players"]) or "no roster match (school in bio/text)"
            print(f'{h["created_at"][:10]} @{au.get("username")} ({au.get("name")}) -> {who}  [via @{h["via"]}]')
            print(f'  {h["text"][:200]!r}\n  https://x.com/{au.get("username")}/status/{h["id"]}\n')
            if len(h["players"]) == 1 and h["players"][0]["name"].lower() not in have_handle and au.get("username", "").lower() not in known:
                rows.append([a.school, h["players"][0]["name"], au["username"], "", f"auto: portal-scan {dt.date.today()}"])
                have_handle.add(h["players"][0]["name"].lower())
        print(f"{len(hits)} portal posts found, {len(rows)} new handles")
        if a.save and rows:
            with manual_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            print(f"Saved to {manual_path.name}")
        return 0

    users = retweeters(a.retweeters, a.max_posts) if a.retweeters else reposted_authors(a.reposts_of.lstrip("@"), a.max_posts)
    source = f"retweeted {a.retweeters}" if a.retweeters else f"reposted by @{a.reposts_of.lstrip('@')}"
    new_rows = []
    for u in users:
        m = name_match(u["name"], roster)
        status = "known" if u["username"].lower() in known else ("NEW" if m else "")
        label = ", ".join(f'{p["name"]} ({p["position"]})' for p in m) or "-"
        print(f'{status:<6} @{u["username"]:<18} {u["name"][:28]:<28} -> {label}')
        if len(m) == 1 and status == "NEW" and m[0]["name"].lower() not in have_handle:
            new_rows.append([a.school, m[0]["name"], u["username"], "", f"auto: {source} {dt.date.today()}"])
            have_handle.add(m[0]["name"].lower())
    print(f"\n{len(users)} accounts, {len(new_rows)} new roster matches")
    if a.save and new_rows:
        with manual_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(new_rows)
        print(f"Saved to {manual_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
