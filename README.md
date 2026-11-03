# RepoRadar

**Find GitHub repos before they trend.** Live at **[juw12.github.io/reporadar](https://juw12.github.io/reporadar/)**.

![RepoRadar](docs/screenshot.png)

RepoRadar tracks GitHub Trending and fast-rising new repositories, keeps its own star history, and ranks repos by the stars they actually gained.

- **Movers**: biggest real star gains in 24 h / 7 d / 30 d, with the source of every number shown (tracked by RepoRadar, a brand-new repo, GitHub Trending's own count, or an estimate).
- **Rising**: brand-new repos ranked by stars per day and a momentum score.
- **Trending log**: a day-by-day archive of GitHub Trending with streaks and appearance counts.
- **Repo details**: star-history chart, Trending appearances, language breakdown, README, and exact 24 h / 7 d gains from stargazer timestamps (with your own GitHub token).
- **Discovery tools**: "new to me" and "hide seen", a watchlist, side-by-side compare, CSV export, keyboard shortcuts, light and dark themes.

## How it works

A GitHub Action (`.github/workflows/collect.yml`) runs every 30 minutes:

1. `collector.py` scrapes GitHub Trending and runs a few search queries, saving star snapshots into SQLite.
2. The database is kept on the `data` branch (one commit, overwritten each run, so history doesn't bloat the repo).
3. `export_static.py` turns the history into JSON files, and the site is deployed to GitHub Pages.

`index.html` is a single file with no build step. It reads those JSON files on GitHub Pages, talks to `server.py` when run locally, and still works opened straight from disk (Rising / Established / Watchlist only).

## Run it locally

```bash
python server.py --open      # http://127.0.0.1:8765, collects every 30 min
```

Python 3.10+ standard library only, nothing to install. Set `GITHUB_TOKEN` for higher GitHub API limits.

MIT licensed.
