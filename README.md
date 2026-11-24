# ASC Football Transfer-Portal Monitor

A personal tracker for American Southwest Conference football players who announce a transfer-portal entry on X. A GitHub Action runs every morning, finds new posts, and publishes a one-row-per-player tracker page on GitHub Pages.

## How it finds players
1. **Rosters** (`build_watchlist.py`): scrapes all six ASC football rosters into `data/watchlist.csv`. The Action refreshes it on Mondays.
2. **X search** (`x_search.py`): portal wording plus school names or team accounts, and portal wording from known player accounts. Most players never name their school in the post; it's only in their bio, which X search ignores.
3. **Teammate reposts** (`teammate_scan.py`): reads recent posts and reposts from every known player account. Keeps portal-style posts whose author is on a roster. This catches image-only posts like "Recruitment 110% open" when a teammate reposts them.
4. **Tracker** (`build_tracker.py`): groups posts by player, writes `data/tracker.csv` and `docs/index.html`.

Status on the tracker:
- **In portal**: the player's post uses portal wording.
- **Likely**: only recruiting wording ("recruitment open", "years of eligibility"). Worth a quick look.

## Your part: `data/overrides.csv`
Edit this file on GitHub to correct the tracker or add players the search can't find:

| status | effect |
|---|---|
| `confirmed` | marks a "Likely" player In portal, or adds a missing player (fill in `url`) |
| `not_portal` | hides a false positive |
| `committed` | marks a player committed elsewhere; put the new school in `note` |

## Daily email
When a scan finds new players, or a Likely player posts with portal wording, the workflow opens an issue titled "New portal entries YYYY-MM-DD" that @-mentions you. GitHub emails it to you. No issue is opened on quiet days. Close each issue once you've read it. If emails don't arrive, check github.com/settings/notifications: "Participating, @mentions and custom" should have Email turned on.

## Adding a player's handle
Handles are what find players who never name their school. Two ways to add one:
- **From your phone or browser:** Issues → New issue → **Add player handle**. Pick the school, enter the name and @handle, and optionally paste their portal post link. A workflow adds the handle to `data/handles_manual.csv`, puts the player on the tracker if you gave a link, rebuilds the page, and closes the issue with a comment. Only issues you open trigger it.
- **Locally:** `py add_handle.py etbu "John Doe" @johndoe [post_url]`, then commit and push.

## Set up on GitHub (one time)
1. Create a new repository on GitHub and push this folder to it. It can be **public**: Actions and Pages are free for public repos, and `.env` is never committed. Phone numbers and emails are removed from saved posts.
2. **Settings → Secrets and variables → Actions → New repository secret**: name `X_BEARER_TOKEN`, value = your bearer token.
3. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, folder = `/docs`.
4. **Actions → Daily portal scan → Run workflow** to test it now. After that it runs every day at 13:00 UTC.

## Cost
X bills per post read (about $0.005 each, as reported). A daily run reads posts newer than the last run, so a quiet day costs pennies. Busy portal weeks cost more. Check usage in the X Developer Console. To spend less in the off-season, change the cron in `.github/workflows/daily.yml` to weekly (`0 13 * * 1`).

## Run locally
Put `X_BEARER_TOKEN=...` in a `.env` file in this folder, then:
```
py build_watchlist.py
py x_search.py --max-posts 100
py teammate_scan.py --max-per-user 30
py build_tracker.py
```
Useful extras:
- `x_search.py --dry-run` prints the queries without making any API calls.
- `x_search.py --archive --start YYYY-MM-DD` searches older posts.
- `discover_handles.py` finds handles through reposts: `--retweeters POST_ID`, `--reposts-of HANDLE`, `--portal-scan POST_ID --since YYYY-MM-DD`, plus `--save` to keep what it finds.

## Files
| File | What it is |
|---|---|
| `schools.json` | ASC football schools: roster URL, site platform, name aliases, official X accounts. Edit when membership changes. |
| `data/watchlist.csv` | One row per rostered player, including X/Instagram handles when known. |
| `data/handles_manual.csv` | Handles found by hand or automatically. Merged on every run. |
| `data/x_posts.jsonl` | Every portal-style post found (phones and emails removed). |
| `data/overrides.csv` | Your corrections and additions. |
| `data/tracker.csv`, `docs/index.html` | The tracker output. |
| `data/x_state.json`, `data/teammate_state.json` | Where each search left off, so runs only read new posts. |

## Known gaps
- Image-only posts are found only if a known teammate reposts them.
- Howard Payne and Schreiner have no known player accounts yet. Add handles to `handles_manual.csv` to cover them.
