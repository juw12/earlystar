# EarlyStar v2 — plan

Goal: close the one real gap vs trendshift.io (server-side history) while keeping
EarlyStar's advantages (velocity for brand-new repos, unseen-only discovery, compare,
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
earlystar/
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

---

# EarlyStar Calls — plan

**Call the next big repo before it blows up.**

EarlyStar merges RepoRadar (find GitHub repos before they trend) with a play-money
prediction market. The radar is the free discovery layer; *Calls* are markets on
repo outcomes ("Will owner/repo reach 10k★ by Dec 31?") that **settle themselves**
from EarlyStar's own star snapshots and GitHub Trending log — no human oracle,
no disputes.

Points have **no monetary value**: they cannot be bought, sold, transferred or
withdrawn. This keeps EarlyStar a skill game / reputation system, not gambling.

---

## 1. Product

| Area | What it does |
|---|---|
| **Radar** (existing) | Movers · Rising · Established · Trending log · Watchlist. Every repo card gets a **Call** button and shows live odds if a call exists. |
| **Calls** | List of markets (open / closing soon / resolved / mine), market drawer with odds chart, star chart vs target, trade panel, rules, recent trades, top holders. |
| **New call** | Users create *stars-reach* or *versus* markets for any public repo (fee 50 pts, 3/day). EarlyStar also auto-creates calls from the radar. |
| **Portfolio** | Balance, net worth, trading profit, open & settled positions, ledger. |
| **Leaderboard** | Trading profit (all-time / last 30 days), accuracy, best early calls. Public profiles. |

### Market types (all binary YES/NO)
1. **stars_reach** — "Will `repo` reach **T★** by **deadline**?"
   * YES as soon as any snapshot taken before the deadline shows ≥ T (early resolution).
   * Otherwise settles on the first snapshot taken **at or after** the deadline (normally ≤ 30 min later).
   * If none within 6 h after the deadline, the last snapshot within 3 h before it is used; else **void** (refund).
2. **versus** — "Will `A` gain more stars than `B` between open and deadline?"
   * Gains = closing snapshot (same rule as above) − stars recorded at market creation. Tie → void.
3. **trending_days** — "Will `repo` be on GitHub Trending (daily, all languages) on at least **N** of the **D** days *d₁…d_D*?"
   * YES as soon as N distinct days are logged. NO at the end of the window.
   * If the collector missed days and the missing days could have changed the answer → void.

### Integrity rules
* **Owners can't trade their own repo** (GitHub login == repo owner).
* **Account age** ≥ 14 days on GitHub to trade (sybil resistance). Configurable.
* **Stake cap**: at most 500 pts net at risk per user per market.
* **Rate limit**: 120 trades/hour/user; 3 created markets/day/user.
* **Star-spike review**: before a YES that benefits a repo is paid, EarlyStar compares the repo's
  last-48 h gain against its prior 7-day baseline. A spike (≥ 200★ and > 5× baseline) parks the market
  in **review**; an admin resolves or voids it. (Bought stars are the obvious attack.)
* Slippage protection on every trade (`min_shares` / `min_points`).
* Questions are generated from structured fields — no free text, nothing to moderate.

---

## 2. Economy & pricing

* New account: **1,000 pts**. Daily bonus: **+25 pts** (first visit each UTC day).
* Market maker: **LMSR** (logarithmic market scoring rule), so every market has a price from the
  first second, even with one trader.

  \[ C(q_y, q_n) = b \ln\left(e^{q_y/b} + e^{q_n/b}\right), \qquad p_{yes} = \frac{e^{q_y/b}}{e^{q_y/b}+e^{q_n/b}} \]

  Buying with \(S\) points on YES gives \(\Delta\) shares where
  \( q_y + \Delta = b \ln\left(e^{(C+S)/b} - e^{q_n/b}\right) \).
  Selling \(\Delta\) YES shares pays \( C(q_y, q_n) - C(q_y - \Delta, q_n) \).
  A winning share pays 1 point.
* Liquidity \(b\): 250 for auto calls, 150 for user calls. House subsidy (max loss) is
  \( b \ln \frac{1}{\min(p_0, 1-p_0)} \) — fine for play money.
* **Opening odds** for stars-reach come from a momentum model:
  required rate \( r_{req} = (T - s_0)/\text{days} \), effective observed rate \( r_{eff} = 0.6\, r_{7d} \),
  \( p_0 = \sigma\!\left(2 \ln \frac{r_{eff}}{r_{req}}\right) \) clamped to [0.03, 0.97].
* **Void** refunds each user's net cost (spent − received); profits from selling are clawed back
  (capped at the current balance).

### Scoring
* **Trading profit** = balance + mark-to-market value of open positions − grants (signup, daily bonus)
  + market-creation fees paid. Creation fees don't count against you.
* **30-day profit** = realised P&L of markets settled in the last 30 days.
* **Accuracy** = share of settled markets where your P&L was positive.
* **Best calls** = winning buys made at ≤ 30 % odds.

---

## 3. Architecture

```
earlystar/
  collector.py   GitHub Trending + search → radar.db (snapshots). Now also refreshes "pinned" repos
                 (every repo with an open market), with a by-id fallback that survives renames.
  markets.py     NEW  schema, LMSR, trading engine, market creation/validation, auto-generation,
                 resolution, payouts, portfolio, leaderboard, profiles.
  auth.py        NEW  GitHub OAuth (no scopes), dev login for local testing, sessions, cookies.
  server.py      JSON API + static index + background collector + market service thread.
  index.html     Single-file UI (no build step): radar + Calls / Portfolio / Leaderboard.
  tests/         unittest suite (stdlib only).
  Dockerfile     One-container deploy (Fly.io / Render / any VPS).
  data/radar.db      public, regenerable radar history (was reporadar.db — auto-renamed)
  data/earlystar.db   private: users, sessions, markets, trades, ledger
```

Two databases on purpose: radar data is public and can be rebuilt; user data is private and must
never end up on the public `data` branch the GitHub Action pushes. The app DB `ATTACH`es the radar DB
read-mostly so resolution can join markets with snapshots.

### Database (earlystar.db)
* `users(id, github_id, login, name, avatar_url, github_created_at, balance, created_ts, last_bonus_day, is_banned)`
* `sessions(token_hash, user_id, created_ts, expires_ts)` — only SHA-256 of the cookie is stored
* `markets(id, kind, repo, repo_id, repo_b, repo_b_id, target, window_start_day, window_days, question,
  open_ts, close_ts, created_by, liquidity, q_yes, q_no, initial_prob, start_stars, start_stars_b,
  status[open|review|resolved|void], outcome, resolved_ts, resolution_note, flag_note, volume, trade_count)`
* `positions(user_id, market_id, yes_shares, no_shares, spent, received, payout)`
* `trades(id, market_id, user_id, ts, side, action, shares, points, prob_before, prob_after)`
* `ledger(id, user_id, ts, delta, kind, market_id, note)` — every balance change; invariant
  `users.balance == SUM(ledger.delta)` is tested.
* `jobs(name, last_ts, detail)` — scheduler bookkeeping
* radar.db gains `pinned(repo_id, full_name, until_ts)`.

### Background work
* Collector: every 30 min (unchanged), then runs the resolver.
* Market service thread: resolver every 5 min; auto-generator every 6 h
  (≤ 3 stars-reach, 2 trending, 1 versus per run; ≤ 8 auto calls per 24 h; ≤ 80 open auto calls).
  * stars-reach target ≈ current + 50 % of the 30-day projection at the 7-day rate, rounded to a nice
    number (snapped to a milestone like 10k if within +15 %), kept only if opening odds are 15–85 %.
  * trending: today's top-10 Trending repos → "≥ 4 of the next 7 days".
  * versus: two top 7-day movers whose daily gain is within ±25 %.

### API
| Method | Path | Notes |
|---|---|---|
| GET | `/api/me` | user + balance (credits daily bonus) or sign-in options |
| GET | `/api/markets?status=&kind=&repo=&mine=&sort=&limit=` | list |
| GET | `/api/markets/{id}` | detail, odds history, star history, holders, trades, my position, rules |
| GET | `/api/markets/{id}/quote?side=&action=&amount=` | trade preview |
| POST | `/api/markets/{id}/trade` | `{side, action, amount, min_shares?, min_points?}` |
| GET | `/api/markets/suggest?repo=` | current stars, 7-day rate, suggested target + odds |
| POST | `/api/markets` | create `{kind, repo, repo_b?, target?, days}` |
| GET | `/api/portfolio` | positions, ledger, P&L |
| GET | `/api/leaderboard?period=all|30d` | |
| GET | `/api/users/{login}` | public profile |
| GET | `/api/admin/review` · POST `/api/admin/markets/{id}/resolve` | admins only |
| GET | `/auth/login` · `/auth/callback` · POST `/auth/logout` · POST `/auth/dev` | |
| — | existing radar endpoints | `/api/movers`, `/api/trending`, `/api/repo/{id}/history`, … |

### Security
* Session cookie `HttpOnly; SameSite=Lax; Secure` (when served over https).
* CSRF: every POST must carry `X-EarlyStar: 1` (cross-site forms can't set it without a CORS preflight,
  which EarlyStar never grants) and, if present, a same-host `Origin`.
* OAuth `state` cookie checked on callback; GitHub token is used once to read the profile and discarded.
* `--public` mode: binds 0.0.0.0, locks the old single-user endpoints (`/api/token`, `/api/collect`,
  `POST /api/watch`) to admins, `/api/stargains` to signed-in users, hides internal error text.
* Body limit 16 KB, strict validation of every field, security headers (nosniff, frame deny, referrer).

---

## 4. Build steps
1. collector: radar.db rename + legacy migration, `pinned` table, refresh pinned repos (+ by-id fallback).
2. markets.py: schema → LMSR → trading → creation/validation → resolution → generator → read models.
3. auth.py + server.py routes, market service thread, public-mode lockdown.
4. index.html: brand, account menu, Calls / Portfolio / Leaderboard views, market drawer + trade panel,
   create-call modal, Call buttons + odds badges on radar cards and the details drawer.
5. Tests, Dockerfile, README, GitHub Action update.

## 5. Verification
1. Unit tests: LMSR identities (prices sum to 1, buy→sell round trip, path independence, closed-form
   vs numeric), trading limits (stake cap, owner ban, closed market, slippage, rate limit),
   every resolution branch for all three kinds (early YES, NO at close, fallbacks, void, tie,
   coverage gaps, spike review), payout conservation, void refunds, ledger invariant, leaderboard math.
2. HTTP integration test on a random port: dev login, CSRF rejection, create → quote → trade → sell,
   portfolio, leaderboard, admin resolve, public-mode lockdown.
3. Live run on real GitHub data: collect, auto-generate calls, trade through the API.
4. Headless-browser screenshots of Calls, a market drawer, Portfolio, Leaderboard and a radar card with odds.

## 6. Later (not in v1)
* Telegram / email alerts when a call you hold moves ±10 %.
* Comments per call, follow users, share cards (OG images) for "I called this at 12 %".
* Org-membership check for the owner ban (needs `read:org`).
* Seasons (monthly resets of the leaderboard), badges.
* Postgres when one SQLite file stops being enough (thousands of concurrent traders).

---

## Status (2026-11-04): built and verified
- [x] collector: radar.db (auto-migrates reporadar.db), `pinned` table, pinned refresh with a by-id fallback for renamed repos
- [x] markets.py: LMSR, trading engine, creation and validation, settlement for all 3 kinds, spike review, void and clawback,
      auto-generation (waits for fresh data), portfolio, leaderboard, profiles, admin resolve
- [x] Momentum model with a youth discount, so viral launch-week repos aren't over-projected (found on live data:
      a 4-day-old repo got a 6× target at 50 %; it's now 2.3× at 49 %)
- [x] auth.py: GitHub OAuth, dev login, hashed sessions, CSRF header + Origin check, public-mode lockdown
- [x] index.html: Calls / Portfolio / Leaderboard, market drawer with live quotes, New call modal, profiles, odds badges
      and Call buttons on radar cards, mobile layout
- [x] 41 automated tests (unit + end-to-end HTTP) passing
- [x] Live run on real GitHub data: 740 repos collected, 6 auto calls, 19 trades by 5 users through the API and UI,
      ledger consistent
- [x] Headless-Chrome QA of every view at desktop and phone widths with no JavaScript errors (fixed along the way: nav overflow,
      relative times, a drawer race left over from RepoRadar, the "sell with no shares" message)
- [ ] Not verified yet: real GitHub OAuth (needs your OAuth app), settlement on real deadlines (first auto calls close Nov 11),
      multi-day behaviour of the spike detector on real data
