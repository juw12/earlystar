<!-- HERO:START -->
<p align="center">
  <a href="https://juw12.github.io/earlystar/"><img src="docs/media/banner.jpg" alt="EarlyStar: Find GitHub repos before they trend." width="100%"></a>
</p>

<p align="center">
  <b>Find GitHub repos before they trend.</b><br>
  See the repos gaining stars fastest right now, filter them in seconds and keep a watchlist.
</p>

<p align="center">
  <a href="https://juw12.github.io/earlystar/"><b>Try it live</b></a> ·
  <a href="#see-it-in-action">See it in action</a> ·
  <a href="#highlights">Highlights</a> ·
  <a href="https://github.com/juw12">More by juw12</a>
</p>

<p align="center">
  <a href="https://github.com/juw12/earlystar"><img src="https://img.shields.io/github/last-commit/juw12/earlystar?label=updated&color=2563eb" alt="Last updated"></a>
  <a href="https://juw12.github.io/earlystar/"><img src="https://img.shields.io/badge/demo-live-2ea44f" alt="Live demo"></a>
  <img src="https://img.shields.io/badge/Web-24292f" alt="Web">
  <img src="https://img.shields.io/badge/license-MIT-2563eb" alt="MIT license">
</p>

<a id="highlights"></a>
<table>
  <tr>
    <td width="33%" valign="top">
      <b>Movers and rising</b><br>
      Repos gaining the most stars in the last 24 hours, ranked.
    </td>
    <td width="33%" valign="top">
      <b>Filter fast</b><br>
      Narrow the list by language, stars and age.
    </td>
    <td width="33%" valign="top">
      <b>Watchlist</b><br>
      Keep an eye on repos before everyone else does.
    </td>
  </tr>
</table>

## See it in action

<p align="center">
  <a href="https://juw12.github.io/earlystar/"><img src="docs/media/demo.png" alt="EarlyStar screenshot" width="820"></a><br>
  <sub>Open the interactive demo at <a href="https://juw12.github.io/earlystar/">juw12.github.io/earlystar/</a></sub>
</p>
<!-- HERO:END -->

## About

**Find GitHub repos before they trend.** Live at **[juw12.github.io/earlystar](https://juw12.github.io/earlystar/)**.

![EarlyStar](docs/screenshot.png)

EarlyStar tracks GitHub Trending and fast-rising new repositories, keeps its own star history, and ranks repos by the stars they actually gained.

- **Movers**: biggest real star gains in 24 h / 7 d / 30 d, with the source of every number shown (tracked by EarlyStar, a brand-new repo, GitHub Trending's own count, or an estimate).
- **Rising**: brand-new repos ranked by stars per day and a momentum score.
- **Trending log**: a day-by-day archive of GitHub Trending with streaks and appearance counts.
- **Repo details**: star-history chart, Trending appearances, language breakdown, README, and exact 24 h / 7 d gains from stargazer timestamps (with your own GitHub token).
- **Activity, not just stars**: commits, merged PRs, new issues (last 7 days) and the latest release for the hottest repos, so hype can be told apart from a project that's actually shipping.
- **README badges + open JSON API**: every tracked repo gets a live badge and all data is plain JSON (see below).
- **Discovery tools**: "new to me" and "hide seen", a watchlist, side-by-side compare, CSV export, keyboard shortcuts, light and dark themes.

## Apps

### Desktop (Windows & macOS)

Prefer a real app window? Download it (about 5–10 MB; it opens the live site, so data is always current):

| Platform | Download |
| --- | --- |
| Windows 10/11 | [EarlyStar-Setup-x64.exe](https://github.com/juw12/earlystar/releases/latest/download/EarlyStar-Setup-x64.exe) · [portable .exe](https://github.com/juw12/earlystar/releases/latest/download/EarlyStar-portable-x64.exe) |
| macOS 10.15+ (Apple Silicon + Intel) | [EarlyStar-macOS.dmg](https://github.com/juw12/earlystar/releases/latest/download/EarlyStar-macOS.dmg) |

The apps aren't code-signed yet. On Windows, if SmartScreen appears click **More info → Run anyway**. On macOS, drag EarlyStar to Applications, then right-click it → **Open** the first time.

### Phone (iPhone & Android)

No app store needed: EarlyStar installs straight from the website as an app with its own icon, full screen.

- **iPhone / iPad**: open [juw12.github.io/earlystar](https://juw12.github.io/earlystar/) in **Safari** → Share button → **Add to Home Screen**.
- **Android**: open it in **Chrome** → tap **Install app** (or ⋮ menu → **Install app / Add to Home screen**).

## Badge for your README

Open any repo on EarlyStar and copy the snippet from its details panel, or build it yourself:

```markdown
[![EarlyStar](https://img.shields.io/endpoint?url=https%3A%2F%2Fjuw12.github.io%2Fearlystar%2Fdata%2Fbadge%2FOWNER%2FNAME.json)](https://juw12.github.io/earlystar/#repo=OWNER/NAME)
```

It shows `#3 today · +3.8k★` while the repo is a top-100 mover, `+X★ this week` otherwise, and falls back to the total stars.

## JSON API

Static files on GitHub Pages, refreshed every 30 minutes, CORS-friendly, no key needed. Base URL: `https://juw12.github.io/earlystar/data/`

| File | What's in it |
| --- | --- |
| `health.json` | Last collection time and counts |
| `movers-24h.json`, `movers-7d.json`, `movers-30d.json` | Ranked star gains (`items[]` with `gain`, `gain_source`, `engagement`, …) |
| `trending/days.json` | Days with a recorded GitHub Trending snapshot |
| `trending/YYYY-MM-DD.json` | That day's Trending lists, keyed `since\|language` |
| `repos.json` | `owner/name` → repo id |
| `history/{id}.json` | Star/fork snapshots, Trending appearances and activity for one repo |
| `badge/{owner}/{name}.json` | shields.io endpoint payload |

## How it works

A GitHub Action (`.github/workflows/collect.yml`) runs every 30 minutes (at :07 and :37, away from the busy top of the hour):

1. `collector.py` scrapes GitHub Trending and runs a few search queries, saving star snapshots into SQLite, then fetches 7-day activity for the hottest repos via one batched GraphQL query.
2. The database is kept on the `data` branch (one commit, overwritten each run, so history doesn't bloat the repo).
3. `export_static.py` turns the history into JSON files and badges, and the site is deployed to GitHub Pages.

`index.html` is a single file with no build step. It reads those JSON files on GitHub Pages, talks to `server.py` when run locally, and still works opened straight from disk (Rising / Established / Watchlist only).

## Calls: play-money predictions on repos

Spot a repo early, then **call it**: "Will `owner/repo` reach 10k★ by Dec 31?", "Will A out-star B this week?", "Will it trend 4 of the next 7 days?" Calls **settle themselves** from EarlyStar's own snapshots, so there's no human judge and nothing to dispute.

> Points are play money. They have no cash value and can't be bought, sold, transferred or withdrawn.

### What you get

- **Calls list** with live odds, 24 h change, volume, traders and progress toward the target.
- **Market drawer**: odds chart, stars-vs-target chart (or a versus/Trending view), trade panel with a live quote, your position and P&L, the settlement rules, recent trades and top holders.
- **Trading** against an LMSR automated market maker, so every call has a price from the first second. Slippage protection is built in.
- **New call**: pick a repo and EarlyStar suggests a target and opening odds from its momentum model. Creating a call costs 50 points.
- **Auto calls** generated from the radar every 6 hours: star targets for top movers, Trending streaks, and versus pairs.
- **Portfolio, Leaderboard** (profit, accuracy, best early calls) and **public profiles**.
- **Radar integration**: every repo card shows its live odds and has a "Call it" button.
- **Integrity**: owners can't trade their own repos, accounts must be at least 14 days old, at most 500 points at risk per call, rate limits, and a star-spike review before paying out a suspicious YES.


### How calls settle

| Call | Settles YES when… | Otherwise |
|---|---|---|
| **Star target** | any snapshot taken before the deadline shows ≥ target (early) | uses the first snapshot taken at or after the deadline. If there is none within 6 h, it falls back to the last snapshot in the 3 h before the deadline; if that's missing too, the call is **voided** and everyone is refunded |
| **Versus** | A gained strictly more stars than B since the call opened | a tie or missing data voids the call |
| **Trending** | the repo is logged on daily Trending (all languages) on N distinct days (early) | NO at the end of the window. If EarlyStar missed days and those days could have changed the answer, the call is voided |

A YES that follows a star spike (≥ 200★ in 48 h and more than 5× the prior week's rate) goes to **review**, and an admin settles or voids it.


## Run it locally

```bash
python3 server.py --dev-login --open     # http://127.0.0.1:8765
```

- `--dev-login` lets you sign in with any username so you can try everything locally. It is refused together with `--public`.
- The first collection takes 1–2 minutes. Auto calls appear once fresh data exists.
- Optional: `export GITHUB_TOKEN=ghp_...` (a token with no scopes is fine) for higher GitHub rate limits.
- Make yourself an admin: `export EARLYSTAR_ADMINS=juw12`. Admins see the review queue and can settle or void calls.
- On Windows, double-click `start.bat`. On macOS/Linux, run `./start.sh`.

Needs Python 3.9 or newer. It uses only the standard library, so there's nothing to install.

## Deploy with accounts (public server)

1. Create a GitHub OAuth app at <https://github.com/settings/developers> → **New OAuth App**:
   - Homepage URL: `https://your-domain`
   - Authorization callback URL: `https://your-domain/auth/callback`
2. Set these environment variables:

   | Variable | Value |
   |---|---|
   | `EARLYSTAR_GITHUB_CLIENT_ID` / `EARLYSTAR_GITHUB_CLIENT_SECRET` | from the OAuth app |
   | `EARLYSTAR_BASE_URL` | `https://your-domain` (https turns on Secure cookies) |
   | `EARLYSTAR_ADMINS` | comma-separated GitHub logins |
   | `GITHUB_TOKEN` | a token with no scopes, for the collector's rate limits |
   | `EARLYSTAR_DATA_DIR` | a **persistent volume** path, e.g. `/data` |

3. Run `python3 server.py --public` (binds `0.0.0.0:$PORT`), or use the Dockerfile:

   ```bash
   docker build -t earlystar .
   docker run -p 8765:8765 -v earlystar-data:/data --env-file .env earlystar
   ```

   This works on Fly.io, Render, Railway or any small VPS. **Use a persistent volume.** The app database holds every account and balance.

`--public` restricts the old single-user endpoints (`/api/token`, `/api/collect`, `POST /api/watch`) to admins, requires sign-in for `/api/stargains`, and hides internal error text.

## Architecture

```
collector.py   GitHub Trending + search → data/radar.db (public, rebuildable). Also refreshes repos with open calls
markets.py     schema, LMSR pricing, trading, creation, settlement, auto-generation, scoring
auth.py        GitHub OAuth (no scopes), dev login, hashed cookie sessions
server.py      JSON API, static index, collector thread, market service thread
index.html     single-file UI, no build step
tests/         python3 -m unittest discover -s tests -t .
```

- **Two databases.** `data/radar.db` is public and the GitHub Action pushes it to the `data` branch for the static Pages mirror. `data/earlystar.db` holds users, sessions, markets, trades and the ledger, and **must stay private**. An old `data/reporadar.db` is renamed to `radar.db` automatically.
- **Ledger invariant.** Every balance change is a ledger row, and the tests check that `users.balance == SUM(ledger.delta)`.
- **Static mirror.** The GitHub Pages site (`export_static.py`) still serves the read-only radar. Calls need the server.

See [`PLAN.md`](PLAN.md) for the full design: pricing math, scoring and the security model.

## License

Released under the [MIT License](LICENSE) © 2026 Juwen Lie. Contributions are welcome under the same license; see the [contributing guide](https://github.com/juw12/.github/blob/main/CONTRIBUTING.md) and [Code of Conduct](https://github.com/juw12/.github/blob/main/CODE_OF_CONDUCT.md).
