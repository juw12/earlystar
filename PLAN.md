# RepoRadar v2 — plan

Goal: close the one real gap vs trendshift.io (server-side history) while keeping
RepoRadar's advantages (velocity for brand-new repos, unseen-only discovery, compare,
watchlist, no install beyond Python).

## Findings that shape the design
- Python 3.13 is installed → backend uses the standard library only (http.server, sqlite3,
  urllib). Nothing to pip install.
- github.com/trending HTML is parseable (15–25 `article.Box-row` per page with total stars,
  forks, "N stars today/this week/this month"). This is the dataset trendshift is built on.
- GitHub search API works unauthenticated (10 req/min) → collector paces requests.
- Stargazer timestamps (exact "stars in last 24h") now return 401 without a token →
  offered only when a token is configured.

## Architecture
```
reporadar/
  index.html      frontend (still works standalone via file://, extra tabs when served)
  collector.py    GitHub search + trending scraper → SQLite snapshots (run alone or by server)
  server.py       127.0.0.1:8765 — static file + JSON API + background collector thread
  start.bat       one-click launch (starts server, opens browser)
  data/reporadar.db
```

### Database
- `repos` — latest metadata per repo (id, full_name unique, language, topics, created_at…)
- `snapshots(repo_id, ts, stars, forks)` — time series; compacted to 1/day after 7 days
- `trending(day, since, language, rank, full_name, period_stars)` — GitHub Trending log
- `watch(full_name)` — server-side watchlist so tracked repos get snapshotted
- `runs(ts, ok, detail)` — collector health

### Collector cycle (every 30 min while the server runs)
1. GitHub Trending pages: daily/weekly/monthly (all languages) + daily for top languages.
2. Search slices: new repos (1d / 7d / 30d windows) and active established repos.
3. Resolve repos seen on Trending but unknown to the DB (batched `repo:` search).
4. Refresh watchlist repos.
5. Write snapshots, compact old ones, log the run.

### API
- `GET  /api/health` — DB size, last/next run, token present
- `GET  /api/movers?window=24h|7d|30d&lang=&min_stars=` — real star gains from snapshots
  (repos created inside the window count all their stars; partial coverage is flagged)
- `GET  /api/repo/{id}/history` — snapshot series + trending appearances
- `GET  /api/trending?since=daily&day=` and `/api/trending/days` — Trending log
- `GET  /api/stargains/{owner}/{name}` — exact 24h/7d gains from stargazer timestamps (token)
- `POST /api/collect`, `POST /api/watch`, `POST /api/token`

### Frontend additions (only when served by server.py)
- 📈 Movers tab — ranked by real stars gained in 24h / 7d / 30d
- 🏆 Trending log tab — day-by-day GitHub Trending archive with appearance counts / streaks
- Details drawer: star-history chart, trending appearances, exact recent gains (token)
- Status bar: collector state + "Collect now"; watchlist syncs to the server

## Verification
1. Unit-test movers math on a synthetic DB (window base, new-repo case, partial coverage).
2. Run a real collection, then a second one, and confirm snapshots/deltas/trending rows.
3. Start the server and hit every endpoint.
4. Headless-browser screenshot of the served app (Movers + details drawer).

## Status (2026-11-03) — executed
- [x] collector.py — Trending scraper + search slices + batched repo resolution, pacing and rate-limit retry
- [x] server.py — JSON API, background collector (30 min), watch/token endpoints, input validation
- [x] index.html — Movers + Trending log tabs, star-history chart, Trending appearances, exact gains (token),
      collector status / Collect now, watchlist sync, deep links (#repo=owner/name); still works via file://
- [x] start.bat — one-click launch
- [x] Verified: 9/9 movers-math unit checks, 3 live collections (0 errors), every endpoint, invalid-input
      rejection, screenshots of Movers / Trending log / details drawer / standalone mode
- [ ] Not verified: exact stargazer gains (needs a GitHub token), multi-day behaviour (streaks, 7d/30d tracked deltas,
      compaction on real data) — these fill in as the server keeps running
