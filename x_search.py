"""Search X for ASC football transfer-portal posts.

Usage:
  py x_search.py                          # recent search (last 7 days); daily mode
  py x_search.py --archive --start 2024-11-01 --end 2025-06-01 --max-posts 100
  py x_search.py --dry-run                # print the queries, no API calls

Reads X_BEARER_TOKEN from .env (or the environment); the token is never printed.
Results are added to data/x_posts.jsonl (duplicates skipped by post ID) and matched
against data/watchlist.csv. X bills per post returned, so --max-posts caps the spend.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POSTS = DATA / "x_posts.jsonl"
STATE = DATA / "x_state.json"
API = "https://api.x.com/2/tweets/search/"
EST_COST_PER_POST = 0.005  # reported pay-per-use read rate; check the Developer Console

PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(text: str) -> str:
    """Strip phone numbers and emails players put in recruiting posts. The repo may be public."""
    return PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", text or ""))


PORTAL_TERMS = [
    '"transfer portal"', '"entered the portal"', '"entering the portal"',
    '"enter the portal"', '"in the portal"', "#TransferPortal",
]
MAX_QUERY_LEN = 512


def token() -> str:
    tok = os.environ.get("X_BEARER_TOKEN", "")
    env = ROOT / ".env"
    if not tok and env.exists():
        m = re.search(r"^\s*X_BEARER_TOKEN\s*=\s*['\"]?([^'\"\s]+)", env.read_text(encoding="utf-8-sig"), re.M)
        tok = m.group(1) if m else ""
    if not tok:
        sys.exit("No X_BEARER_TOKEN found in .env or the environment.")
    return urllib.parse.unquote(tok)


AGGREGATORS = DATA / "aggregators.csv"


def load_aggregators() -> list[str]:
    """News/edits/agency accounts that post portal news (data/aggregators.csv)."""
    if not AGGREGATORS.exists():
        return []
    with AGGREGATORS.open(newline="", encoding="utf-8") as f:
        return [r["x_handle"].strip().lstrip("@") for r in csv.DictReader(f) if r.get("x_handle", "").strip()]


def build_queries(schools: list[dict], handles: dict[str, list[str]], aggregators: list[str] | None = None) -> list[tuple[str, str]]:
    """Per school: one query on school names / team accounts, one on news-aggregator
    accounts that name the school, plus queries on the players' own accounts. Most
    players never name their school in a portal post; it's only in their bio, which
    X search ignores. So their own accounts must be searched."""
    portal = "(" + " OR ".join(PORTAL_TERMS) + ")"
    out = []
    for s in schools:
        aliases = [f'"{a}"' if " " in a or "-" in a else a for a in s["aliases"]]
        names = list(aliases)
        for h in s.get("team_x", []):
            names += [f"from:{h}", f"@{h}"]
        q = f'{portal} ({" OR ".join(names)}) -is:retweet'
        if len(q) > MAX_QUERY_LEN:
            sys.exit(f"Query for {s['name']} is {len(q)} chars (limit {MAX_QUERY_LEN}); trim its aliases.")
        out.append((s["id"], q))
        # Aggregators: any post with the word "portal" that names this school. Search returns
        # only matching posts, so a busy national account costs only its ASC posts.
        batch: list[str] = []
        for h in (aggregators or []) + [""]:
            trial = batch + [f"from:{h}"] if h else batch
            q = f'portal ({" OR ".join(trial)}) ({" OR ".join(aliases)}) -is:retweet'
            if h and len(q) <= MAX_QUERY_LEN:
                batch = trial
                continue
            if batch:
                out.append((s["id"], f'portal ({" OR ".join(batch)}) ({" OR ".join(aliases)}) -is:retweet'))
            batch = [f"from:{h}"] if h else []
        # Player accounts, packed as many per query as the length limit allows.
        batch: list[str] = []
        for h in handles.get(s["id"], []) + [""]:
            trial = batch + [f"from:{h}"] if h else batch
            q = f'{portal} ({" OR ".join(trial)}) -is:retweet'
            if h and len(q) <= MAX_QUERY_LEN:
                batch = trial
                continue
            if batch:
                out.append((s["id"], f'{portal} ({" OR ".join(batch)}) -is:retweet'))
            batch = [f"from:{h}"] if h else []
    return out


def qkey(sid: str, q: str) -> str:
    """State key for a query: stays the same when other queries are added or removed."""
    return f"{sid}:{hashlib.sha1(q.encode()).hexdigest()[:12]}"


def player_handles(roster: list[dict]) -> dict[str, list[str]]:
    """Known player X handles per school: watchlist plus handles_manual.csv."""
    out: dict[str, set[str]] = {}
    for p in roster:
        if p.get("x_handle"):
            out.setdefault(p["school_id"], set()).add(p["x_handle"])
    manual = DATA / "handles_manual.csv"
    if manual.exists():
        with manual.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("x_handle"):
                    out.setdefault(r["school_id"], set()).add(r["x_handle"].lstrip("@"))
    return {k: sorted(v, key=str.lower) for k, v in out.items()}


def call_api(path: str, params: dict) -> dict:
    """GET any X API v2 path, e.g. 'users/by/username/x'. Token is read from .env."""
    return _get("https://api.x.com/2/" + path, params, token(), path)


def call(endpoint: str, params: dict, tok: str) -> dict:
    return _get(API + endpoint, params, tok, endpoint)


def _get(base: str, params: dict, tok: str, endpoint: str) -> dict:
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "User-Agent": "asc-portal-monitor/0.1"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:400]
            if e.code == 429 and attempt < 2:
                reset = int(e.headers.get("x-rate-limit-reset", time.time() + 60))
                wait = max(5, min(900, reset - int(time.time())))
                print(f"  rate limited; waiting {wait}s")
                time.sleep(wait)
                continue
            raise SystemExit(f"X API error {e.code} on {endpoint}: {body}")
    return {}


def load_watchlist() -> list[dict]:
    path = DATA / "watchlist.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def known_handles(roster: list[dict]) -> dict[str, dict]:
    """lowercased X handle -> player, from the watchlist plus handles_manual.csv
    (manual players who aren't on a roster still count)."""
    out = {p["x_handle"].lower(): p for p in roster if p.get("x_handle")}
    manual = DATA / "handles_manual.csv"
    if manual.exists():
        by_name = {(p["school_id"], p["name"].lower()): p for p in roster}
        with manual.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                h = r.get("x_handle", "").lstrip("@").lower()
                if h:
                    out[h] = by_name.get((r["school_id"], r["name"].lower())) or {
                        "school_id": r["school_id"], "name": r["name"], "position": "?", "x_handle": h}
    return out


def tagged_players(text: str, handles: dict[str, dict]) -> list[str]:
    """Known players @-mentioned in a post, e.g. a news account's "PORTAL NEWS: ... @Porterjr2Jr".
    Returns 'Name (school_id, POS)' strings like match_players."""
    out = []
    for h in re.findall(r"@(\w{1,15})", text or ""):
        p = handles.get(h.lower())
        if p:
            s = f'{p["name"]} ({p["school_id"]}, {p["position"]})'
            if s not in out:
                out.append(s)
    return out


def match_players(text: str, author: dict, school_id: str, roster: list[dict], handles: dict[str, dict] | None = None) -> list[str]:
    """Watchlist players whose full name appears in the post or the author's name/bio.
    If none, known players the post @-mentions (news and edits accounts tag the player)."""
    hay = " ".join([text, author.get("name", ""), author.get("description", "")]).lower()
    words = set(re.findall(r"[a-z]+", re.sub(r"['’`]", "", hay)))
    handle = author.get("username", "").lower()
    hits = []
    for p in roster:
        parts = re.findall(r"[a-z]+", re.sub(r"['’`]", "", p["name"].lower()))
        # Full name in the text, or first + last name as separate words: "Adrian (AP) Porter jr".
        named = p["name"].lower() in hay or (len(parts) > 1 and parts[0] in words and parts[-1] in words)
        if p["name"] and (named or (p["x_handle"] and p["x_handle"].lower() == handle)):
            hits.append(f'{p["name"]} ({p["school_id"]}, {p["position"]}){"" if p["school_id"] == school_id else " *other school*"}')
    if not hits and handles:
        hits = tagged_players(text, handles)
    return hits


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji-safe on Windows consoles
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", action="store_true", help="full-archive search instead of the last 7 days")
    ap.add_argument("--start", help="YYYY-MM-DD (archive mode)")
    ap.add_argument("--end", help="YYYY-MM-DD (archive mode)")
    ap.add_argument("--max-posts", type=int, default=100, help="stop after this many posts in total (cost cap)")
    ap.add_argument("--school", help="only search this school id from schools.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    schools = json.loads((ROOT / "schools.json").read_text(encoding="utf-8"))["schools"]
    if a.school:
        schools = [s for s in schools if s["id"] == a.school]
    roster = load_watchlist()
    handles = known_handles(roster)
    queries = build_queries(schools, player_handles(roster), load_aggregators())
    if a.dry_run:
        for sid, q in queries:
            print(f"[{sid}] ({len(q)} chars) {q}\n")
        return 0

    tok = token()
    DATA.mkdir(exist_ok=True)
    seen = set()
    if POSTS.exists():
        seen = {json.loads(l)["id"] for l in POSTS.read_text(encoding="utf-8").splitlines() if l.strip()}
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if any(re.fullmatch(r"\w+:\d+", k) for k in state):
        # Old files keyed searches by list position, which shifts when handles or
        # aggregators are added. Carry each over to its query text where it still lines up.
        old = build_queries(schools, player_handles(roster))
        state = {qkey(sid, q): state[f"{sid}:{i}"] for i, (sid, q) in enumerate(old) if f"{sid}:{i}" in state}
    endpoint = "all" if a.archive else "recent"
    budget, read, new = a.max_posts, 0, []

    for qi, (sid, q) in enumerate(queries):
        key = qkey(sid, q)
        if budget <= 0:
            print("Post cap reached; stopping.")
            break
        params = {
            "query": q,
            "max_results": max(10, min(100, budget)),
            "tweet.fields": "created_at,author_id,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "username,name,description",
        }
        if a.archive:
            if a.start: params["start_time"] = f"{a.start}T00:00:00Z"
            if a.end: params["end_time"] = f"{a.end}T00:00:00Z"
        elif state.get(key):
            params["since_id"] = state[key]
        school_read, token_next = 0, None
        while budget > 0:
            if token_next:
                params["next_token"] = token_next
            res = call(endpoint, params, tok)
            posts = res.get("data", [])
            users = {u["id"]: u for u in res.get("includes", {}).get("users", [])}
            read += len(posts); school_read += len(posts); budget -= len(posts)
            for p in posts:
                if not a.archive and (not state.get(key) or int(p["id"]) > int(state[key])):
                    state[key] = p["id"]
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                u = users.get(p["author_id"], {})
                rec = {
                    "id": p["id"], "school_id": sid, "created_at": p.get("created_at"),
                    "username": u.get("username"), "author_name": u.get("name"),
                    "text": redact(p.get("text", "")), "url": f'https://x.com/{u.get("username", "i")}/status/{p["id"]}',
                    "players": match_players(p.get("text", ""), u, sid, roster, handles),
                    "found_via": endpoint,
                }
                new.append(rec)
            token_next = res.get("meta", {}).get("next_token")
            if not token_next:
                break
            params["max_results"] = max(10, min(100, budget))
            time.sleep(1.1)  # full-archive allows about one request per second
        print(f"[{sid} q{qi}] {school_read} posts")
        time.sleep(1.1)

    with POSTS.open("a", encoding="utf-8") as f:
        for r in new:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if not a.archive:
        STATE.write_text(json.dumps(state, indent=2))

    print(f"\n{read} posts read (~${read * EST_COST_PER_POST:.2f} at ${EST_COST_PER_POST}/post), {len(new)} new saved to {POSTS.relative_to(ROOT)}")
    for r in new:
        tag = f'  -> {", ".join(r["players"])}' if r["players"] else ""
        print(f'\n[{r["school_id"]}] {r["created_at"][:10]} @{r["username"]}{tag}\n  {r["text"][:220].replace(chr(10), " ")}\n  {r["url"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
