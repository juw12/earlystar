"""EarlyStar collector.

Snapshots GitHub Trending pages and GitHub search results into SQLite so that
star gains can be computed from real history.

Standard library only. Run standalone with:
    python collector.py --once
or let server.py run it on a schedule.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("EARLYSTAR_DATA_DIR") or BASE_DIR / "data")
DB_PATH = DATA_DIR / "radar.db"
LEGACY_DB_PATH = DATA_DIR / "reporadar.db"  # RepoRadar-era name, migrated on first connect
TOKEN_PATH = DATA_DIR / "token.txt"

API_ROOT = "https://api.github.com"
TRENDING_ROOT = "https://github.com/trending"
USER_AGENT = "EarlyStar/2.0 (local collector)"

# Trending pages scraped every cycle: (since, language slug). "" = all languages.
TRENDING_PAGES = [
    ("daily", ""),
    ("weekly", ""),
    ("monthly", ""),
    ("daily", "python"),
    ("daily", "typescript"),
    ("daily", "javascript"),
    ("daily", "rust"),
    ("daily", "go"),
]

# Snapshots older than this are thinned to one per repo per day.
FULL_RESOLUTION_DAYS = 7
SEARCH_QUERY_MAX_CHARS = 250
# Engagement (commits / merged PRs / new issues in 7 days, latest release) via GraphQL.
ENGAGEMENT_PER_RUN = 40
ENGAGEMENT_BATCH = 10
ENGAGEMENT_REFRESH_SECONDS = 6 * 3600
PINNED_BY_ID_LIMIT = 40  # by-id lookups per run for pinned repos the search missed

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY,
    full_name    TEXT NOT NULL COLLATE NOCASE,
    owner_login  TEXT,
    avatar_url   TEXT,
    html_url     TEXT,
    description  TEXT,
    language     TEXT,
    topics       TEXT,
    license      TEXT,
    homepage     TEXT,
    created_at   TEXT,
    pushed_at    TEXT,
    stars        INTEGER,
    forks        INTEGER,
    open_issues  INTEGER,
    first_seen   REAL,
    last_seen    REAL
);
CREATE INDEX IF NOT EXISTS idx_repos_name ON repos(full_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_repos_language ON repos(language);

CREATE TABLE IF NOT EXISTS snapshots (
    repo_id  INTEGER NOT NULL,
    ts       REAL NOT NULL,
    stars    INTEGER NOT NULL,
    forks    INTEGER,
    PRIMARY KEY (repo_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS trending (
    day           TEXT NOT NULL,
    since         TEXT NOT NULL,
    language      TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    full_name     TEXT NOT NULL COLLATE NOCASE,
    period_stars  INTEGER,
    stars         INTEGER,
    forks         INTEGER,
    repo_language TEXT,
    description   TEXT,
    captured_at   REAL,
    PRIMARY KEY (day, since, language, full_name)
);
CREATE INDEX IF NOT EXISTS idx_trending_name ON trending(full_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS engagement (
    repo_id        INTEGER PRIMARY KEY,
    updated_at     REAL,
    commits_7d     INTEGER,
    merged_prs_7d  INTEGER,
    new_issues_7d  INTEGER,
    release_tag    TEXT,
    release_at     TEXT
);

CREATE TABLE IF NOT EXISTS watch (
    full_name TEXT PRIMARY KEY COLLATE NOCASE,
    added_at  REAL
);

-- Repos with an open EarlyStar market: always refreshed so markets can settle.
CREATE TABLE IF NOT EXISTS pinned (
    repo_id   INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL COLLATE NOCASE,
    until_ts  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    ts        REAL PRIMARY KEY,
    duration  REAL,
    ok        INTEGER,
    detail    TEXT
);
"""


# --------------------------------------------------------------------------- storage

def migrate_legacy_db(db_path: Path = DB_PATH) -> None:
    """Rename data/reporadar.db (and its WAL files) to data/radar.db once."""
    if db_path != DB_PATH or db_path.exists() or not LEGACY_DB_PATH.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        legacy = Path(str(LEGACY_DB_PATH) + suffix)
        if legacy.exists():
            legacy.rename(Path(str(db_path) + suffix))


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrate_legacy_db(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def load_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    return ""


def save_token(token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if token:
        TOKEN_PATH.write_text(token.strip(), encoding="utf-8")
    elif TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def upsert_repo(conn: sqlite3.Connection, item: dict, ts: float) -> int:
    license_info = item.get("license") or {}
    owner = item.get("owner") or {}
    conn.execute(
        """
        INSERT INTO repos (id, full_name, owner_login, avatar_url, html_url, description, language,
                           topics, license, homepage, created_at, pushed_at, stars, forks,
                           open_issues, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            full_name = excluded.full_name,
            owner_login = excluded.owner_login,
            avatar_url = excluded.avatar_url,
            html_url = excluded.html_url,
            description = excluded.description,
            language = excluded.language,
            topics = excluded.topics,
            license = excluded.license,
            homepage = excluded.homepage,
            pushed_at = excluded.pushed_at,
            stars = excluded.stars,
            forks = excluded.forks,
            open_issues = excluded.open_issues,
            last_seen = excluded.last_seen
        """,
        (
            item["id"],
            item["full_name"],
            owner.get("login"),
            owner.get("avatar_url"),
            item.get("html_url"),
            item.get("description"),
            item.get("language"),
            json.dumps(item.get("topics") or []),
            license_info.get("spdx_id"),
            item.get("homepage"),
            item.get("created_at"),
            item.get("pushed_at"),
            item.get("stargazers_count", 0),
            item.get("forks_count", 0),
            item.get("open_issues_count", 0),
            ts,
            ts,
        ),
    )
    record_snapshot(conn, item["id"], ts, item.get("stargazers_count", 0), item.get("forks_count", 0))
    return item["id"]


def record_snapshot(conn: sqlite3.Connection, repo_id: int, ts: float, stars: int, forks: int | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (repo_id, ts, stars, forks) VALUES (?, ?, ?, ?)",
        (repo_id, ts, stars, forks),
    )


def compact_snapshots(conn: sqlite3.Connection, now: float) -> int:
    """Keep full resolution for recent data; one snapshot per repo per day after that."""
    cutoff = now - FULL_RESOLUTION_DAYS * 86400
    cursor = conn.execute(
        """
        DELETE FROM snapshots
        WHERE ts < :cutoff
          AND (repo_id, ts) NOT IN (
              SELECT repo_id, MAX(ts) FROM snapshots
              WHERE ts < :cutoff
              GROUP BY repo_id, CAST(ts / 86400 AS INTEGER)
          )
        """,
        {"cutoff": cutoff},
    )
    return cursor.rowcount


# --------------------------------------------------------------------------- GitHub access

class RateLimited(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str = "", log=print):
        self.token = token
        self.log = log
        self._last_search = 0.0

    @property
    def search_interval(self) -> float:
        # Search API: 10 requests/min unauthenticated, 30/min with a token.
        return 2.2 if self.token else 6.5

    def _open(self, url: str, accept: str, use_token: bool = True) -> tuple[bytes, dict]:
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if use_token and self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(3):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read(), dict(response.headers)
            except urllib.error.HTTPError as error:
                remaining = error.headers.get("x-ratelimit-remaining")
                if error.code in (403, 429) and (remaining == "0" or error.headers.get("retry-after")):
                    reset = float(error.headers.get("x-ratelimit-reset") or 0)
                    wait = float(error.headers.get("retry-after") or max(reset - time.time(), 1))
                    if wait > 75 or attempt == 2:
                        raise RateLimited(f"rate limited for {wait:.0f}s ({url})") from error
                    self.log(f"  rate limited, waiting {wait:.0f}s")
                    time.sleep(wait + 1)
                    continue
                if error.code >= 500 and attempt < 2:
                    time.sleep(3)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise
        raise RuntimeError("unreachable")

    def api(self, path: str, accept: str = "application/vnd.github+json"):
        body, headers = self._open(API_ROOT + path, accept)
        return json.loads(body), headers

    def graphql(self, query: str) -> dict:
        """Run a GraphQL query (requires a token). Partial results are returned even if some aliases fail."""
        request = urllib.request.Request(
            API_ROOT + "/graphql",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        return payload.get("data") or {}

    def search(self, query: str, page: int = 1, per_page: int = 100) -> list[dict]:
        wait = self._last_search + self.search_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_search = time.time()
        params = urllib.parse.urlencode(
            {"q": query, "sort": "stars", "order": "desc", "per_page": per_page, "page": page}
        )
        data, _ = self.api("/search/repositories?" + params)
        return data.get("items", [])

    def trending(self, since: str, language: str = "") -> list[dict]:
        path = "/" + urllib.parse.quote(language) if language else ""
        url = f"{TRENDING_ROOT}{path}?since={since}"
        body, _ = self._open(url, "text/html", use_token=False)
        return parse_trending(body.decode("utf-8", "replace"))

    def stargazer_gains(self, full_name: str, total_stars: int, max_pages: int = 10) -> dict:
        """Exact recent star gains from stargazer timestamps (requires a token)."""
        if not self.token:
            raise PermissionError("A GitHub token is required for stargazer timestamps.")
        now = time.time()
        day_cut = now - 86400
        week_cut = now - 7 * 86400
        last_page = max(1, math.ceil(total_stars / 100))
        gained_day = 0
        gained_week = 0
        reached_week_boundary = False
        pages_read = 0
        for page in range(last_page, max(last_page - max_pages, 0), -1):
            data, _ = self.api(
                f"/repos/{full_name}/stargazers?per_page=100&page={page}",
                accept="application/vnd.github.star+json",
            )
            pages_read += 1
            for entry in data:
                starred = iso_to_ts(entry.get("starred_at"))
                if starred is None:
                    continue
                if starred >= week_cut:
                    gained_week += 1
                if starred >= day_cut:
                    gained_day += 1
            oldest = iso_to_ts(data[0].get("starred_at")) if data else None
            if oldest is None or oldest < week_cut:
                reached_week_boundary = True
                break
        return {
            "full_name": full_name,
            "gained_24h": gained_day,
            "gained_7d": gained_week,
            "complete_7d": reached_week_boundary or pages_read >= last_page,
            "pages_read": pages_read,
        }


# --------------------------------------------------------------------------- parsing

ARTICLE_RE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
NAME_RE = re.compile(r'<h2[^>]*>.*?href="/([^"/]+/[^"/?#]+)"', re.S)
DESCRIPTION_RE = re.compile(r'<p class="col-9[^"]*">(.*?)</p>', re.S)
LANGUAGE_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
PERIOD_RE = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)")
TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", text))).strip()


def _number(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", _clean(text))
    return int(digits) if digits else None


def parse_trending(page_html: str) -> list[dict]:
    entries = []
    for rank, article in enumerate(ARTICLE_RE.findall(page_html), start=1):
        name_match = NAME_RE.search(article)
        if not name_match:
            continue
        full_name = name_match.group(1)
        escaped = re.escape(full_name)
        stars_match = re.search(rf'href="/{escaped}/stargazers"[^>]*>(.*?)</a>', article, re.S)
        forks_match = re.search(rf'href="/{escaped}/forks"[^>]*>(.*?)</a>', article, re.S)
        description_match = DESCRIPTION_RE.search(article)
        language_match = LANGUAGE_RE.search(article)
        period_match = PERIOD_RE.search(article)
        entries.append(
            {
                "rank": rank,
                "full_name": full_name,
                "description": _clean(description_match.group(1)) if description_match else "",
                "language": language_match.group(1).strip() if language_match else None,
                "stars": _number(stars_match.group(1)) if stars_match else None,
                "forks": _number(forks_match.group(1)) if forks_match else None,
                "period_stars": _number(period_match.group(1)) if period_match else None,
            }
        )
    return entries


def iso_to_ts(value: str | None) -> float | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def engagement_query(names: list[str], since: dt.datetime) -> str:
    """One GraphQL query with three aliases per repo: repository details plus two issue searches."""
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_day = since.strftime("%Y-%m-%d")
    parts = []
    for index, full_name in enumerate(names):
        owner, name = full_name.split("/", 1)
        merged = json.dumps(f"repo:{full_name} is:pr is:merged merged:>={since_day}")
        opened = json.dumps(f"repo:{full_name} is:issue created:>={since_day}")
        parts.append(
            f"r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ "
            f"defaultBranchRef {{ target {{ ... on Commit {{ history(since: \"{since_iso}\") {{ totalCount }} }} }} }} "
            f"latestRelease {{ tagName publishedAt }} }}\n"
            f"p{index}: search(query: {merged}, type: ISSUE, first: 0) {{ issueCount }}\n"
            f"i{index}: search(query: {opened}, type: ISSUE, first: 0) {{ issueCount }}\n"
        )
    return "query {\n" + "".join(parts) + "}"


def parse_engagement(data: dict, names: list[str]) -> dict[str, dict]:
    results = {}
    for index, full_name in enumerate(names):
        repo = data.get(f"r{index}")
        if not repo:
            continue
        target = (repo.get("defaultBranchRef") or {}).get("target") or {}
        release = repo.get("latestRelease") or {}
        results[full_name] = {
            "commits_7d": (target.get("history") or {}).get("totalCount"),
            "merged_prs_7d": (data.get(f"p{index}") or {}).get("issueCount"),
            "new_issues_7d": (data.get(f"i{index}") or {}).get("issueCount"),
            "release_tag": release.get("tagName"),
            "release_at": release.get("publishedAt"),
        }
    return results


def chunk_repo_queries(names: list[str]) -> list[str]:
    """Pack `repo:` qualifiers into search queries under GitHub's length limit."""
    queries = []
    current = ""
    for name in names:
        term = "repo:" + name
        candidate = (current + " " + term).strip()
        if len(candidate) > SEARCH_QUERY_MAX_CHARS and current:
            queries.append(current)
            current = term
        else:
            current = candidate
    if current:
        queries.append(current)
    return queries


# --------------------------------------------------------------------------- collection

class Collector:
    def __init__(self, db_path: Path = DB_PATH, log=print, token: str | None = None):
        self.db_path = db_path
        self.log = log
        self.client = GitHubClient(load_token() if token is None else token, log)

    def search_slices(self, today: dt.date) -> list[tuple[str, str, int]]:
        def days_ago(days: int) -> str:
            return (today - dt.timedelta(days=days)).isoformat()

        return [
            ("new-1d", f"created:>={days_ago(1)} stars:>=5 archived:false", 1),
            ("new-7d", f"created:>={days_ago(7)} stars:>=20 archived:false", 2),
            ("new-30d", f"created:>={days_ago(30)} stars:>=100 archived:false", 2),
            ("active", f"created:<{days_ago(30)} pushed:>={days_ago(1)} stars:>=2000 archived:false", 2),
        ]

    def run_once(self) -> dict:
        started = time.time()
        run_ts = round(started, 3)
        today = dt.datetime.now(dt.timezone.utc).date()
        summary = {"trending_rows": 0, "search_repos": 0, "resolved": 0, "snapshots": 0, "errors": []}
        conn = connect(self.db_path)
        refreshed: set[str] = set()
        trending_totals: dict[str, tuple[int | None, int | None]] = {}

        try:
            # 1. GitHub Trending pages
            for since, language in TRENDING_PAGES:
                try:
                    entries = self.client.trending(since, language)
                except Exception as error:  # keep going on partial failures
                    summary["errors"].append(f"trending {since}/{language or 'all'}: {error}")
                    continue
                with conn:
                    for entry in entries:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO trending
                                (day, since, language, rank, full_name, period_stars, stars, forks,
                                 repo_language, description, captured_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                today.isoformat(), since, language, entry["rank"], entry["full_name"],
                                entry["period_stars"], entry["stars"], entry["forks"],
                                entry["language"], entry["description"], run_ts,
                            ),
                        )
                        trending_totals[entry["full_name"].lower()] = (entry["stars"], entry["forks"])
                summary["trending_rows"] += len(entries)
                self.log(f"  trending {since:<7} {language or 'all':<10} {len(entries)} repos")
                time.sleep(1.2)

            # 2. Search slices
            for label, query, pages in self.search_slices(today):
                for page in range(1, pages + 1):
                    try:
                        items = self.client.search(query, page)
                    except Exception as error:
                        summary["errors"].append(f"search {label} p{page}: {error}")
                        break
                    with conn:
                        for item in items:
                            upsert_repo(conn, item, run_ts)
                            refreshed.add(item["full_name"].lower())
                    summary["search_repos"] += len(items)
                    self.log(f"  search  {label:<8} page {page}: {len(items)} repos")
                    if len(items) < 100:
                        break

            # 3. Trending repos + watchlist + pinned market repos not covered by search
            watch_names = [row[0] for row in conn.execute("SELECT full_name FROM watch")]
            pinned = {
                row[1].lower(): row[0]
                for row in conn.execute("SELECT repo_id, full_name FROM pinned WHERE until_ts > ?", (run_ts,))
            }
            watch_names += [name for name in pinned if name not in {n.lower() for n in watch_names}]
            known = {
                row[0].lower(): row[1]
                for row in conn.execute("SELECT full_name, id FROM repos")
            }
            pending = []
            for name in list({n.lower(): n for n in list(trending_totals) + watch_names}.values()):
                if name.lower() in refreshed:
                    continue
                stars, forks = trending_totals.get(name.lower(), (None, None))
                if name.lower() in known and stars is not None:
                    # Cheap path: total stars straight from the Trending page.
                    with conn:
                        record_snapshot(conn, known[name.lower()], run_ts, stars, forks)
                        conn.execute(
                            "UPDATE repos SET stars = ?, forks = COALESCE(?, forks), last_seen = ? WHERE id = ?",
                            (stars, forks, run_ts, known[name.lower()]),
                        )
                    refreshed.add(name.lower())
                else:
                    pending.append(self._canonical_name(name, conn))
            for query in chunk_repo_queries(pending):
                try:
                    items = self.client.search(query, 1)
                except Exception as error:
                    summary["errors"].append(f"resolve: {error}")
                    continue
                with conn:
                    for item in items:
                        upsert_repo(conn, item, run_ts)
                        refreshed.add(item["full_name"].lower())
                summary["resolved"] += len(items)
            if pending:
                self.log(f"  resolved {summary['resolved']} of {len(pending)} trending/watch repos")

            # 4. Engagement for the hottest repos (token required for GraphQL)
            try:
                self.collect_engagement(conn, run_ts, today, summary)
            except Exception as error:
                summary["errors"].append(f"engagement: {error}")

            # 5. Pinned repos still missing (usually renamed or transferred): look them up by id.
            refreshed_ids = self._refreshed_ids(conn, run_ts)
            missing_pinned = [
                (repo_id, name) for name, repo_id in pinned.items()
                if name not in refreshed and repo_id not in refreshed_ids
            ]
            if missing_pinned and self.client.token:
                for repo_id, name in missing_pinned[:PINNED_BY_ID_LIMIT]:
                    try:
                        item, _ = self.client.api(f"/repositories/{repo_id}")
                    except Exception as error:
                        summary["errors"].append(f"pinned {name}: {error}")
                        continue
                    with conn:
                        upsert_repo(conn, item, run_ts)
                        conn.execute(
                            "UPDATE pinned SET full_name = ? WHERE repo_id = ?", (item["full_name"], repo_id)
                        )
                    refreshed.add(item["full_name"].lower())
                summary["pinned_by_id"] = min(len(missing_pinned), PINNED_BY_ID_LIMIT)
            elif missing_pinned:
                summary["pinned_missing"] = len(missing_pinned)

            with conn:
                removed = compact_snapshots(conn, run_ts)
            summary["snapshots"] = len(refreshed)
            summary["compacted"] = removed
        finally:
            summary["duration"] = round(time.time() - started, 1)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO runs (ts, duration, ok, detail) VALUES (?, ?, ?, ?)",
                    (run_ts, summary["duration"], int(not summary["errors"]), json.dumps(summary)),
                )
            conn.close()
        return summary

    def collect_engagement(self, conn: sqlite3.Connection, run_ts: float, today: dt.date, summary: dict) -> None:
        """Commits, merged PRs and new issues in the last 7 days plus latest release, for today's hottest repos."""
        if not self.client.token:
            return
        # Hotness = biggest trending period gain today (daily list counts most).
        trending_today = {
            row[0].lower(): row[1] or 0
            for row in conn.execute(
                """
                SELECT full_name, MAX(CASE since WHEN 'daily' THEN period_stars * 30
                                                 WHEN 'weekly' THEN period_stars * 4
                                                 ELSE period_stars END)
                FROM trending WHERE day = ? GROUP BY full_name
                """,
                (today.isoformat(),),
            )
        }
        new_since = (today - dt.timedelta(days=7)).isoformat()
        rows = conn.execute(
            """
            SELECT r.id, r.full_name, r.created_at FROM repos r
            LEFT JOIN engagement e ON e.repo_id = r.id
            WHERE e.updated_at IS NULL OR e.updated_at < ?
            ORDER BY r.stars DESC
            """,
            (run_ts - ENGAGEMENT_REFRESH_SECONDS,),
        ).fetchall()
        # Hottest trending repos first, then brand-new repos by stars.
        candidates = sorted(
            (row for row in rows if row[1].lower() in trending_today),
            key=lambda row: trending_today[row[1].lower()],
            reverse=True,
        )
        candidates += [row for row in rows if row[1].lower() not in trending_today and (row[2] or "") >= new_since]
        candidates = candidates[:ENGAGEMENT_PER_RUN]
        ids = {row[1]: row[0] for row in candidates}
        names = list(ids)
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
        updated = 0
        for start in range(0, len(names), ENGAGEMENT_BATCH):
            batch = names[start:start + ENGAGEMENT_BATCH]
            results = parse_engagement(self.client.graphql(engagement_query(batch, since)), batch)
            with conn:
                for full_name, values in results.items():
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO engagement
                            (repo_id, updated_at, commits_7d, merged_prs_7d, new_issues_7d, release_tag, release_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ids[full_name], run_ts, values["commits_7d"], values["merged_prs_7d"],
                         values["new_issues_7d"], values["release_tag"], values["release_at"]),
                    )
            updated += len(results)
        summary["engagement"] = updated
        if names:
            self.log(f"  engagement updated for {updated} of {len(names)} repos")

    @staticmethod
    def _refreshed_ids(conn: sqlite3.Connection, run_ts: float) -> set:
        return {row[0] for row in conn.execute("SELECT repo_id FROM snapshots WHERE ts = ?", (run_ts,))}

    @staticmethod
    def _canonical_name(name: str, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT full_name FROM trending WHERE full_name = ? LIMIT 1", (name,)).fetchone()
        return row[0] if row else name


def main() -> None:
    parser = argparse.ArgumentParser(description="EarlyStar collector")
    parser.add_argument("--once", action="store_true", help="run a single collection and exit")
    parser.add_argument("--interval", type=float, default=30, help="minutes between runs (loop mode)")
    args = parser.parse_args()
    collector = Collector()
    while True:
        print(f"[{dt.datetime.now():%H:%M:%S}] collecting (token: {'yes' if collector.client.token else 'no'})")
        summary = collector.run_once()
        print(f"[{dt.datetime.now():%H:%M:%S}] done: {json.dumps(summary)}")
        if args.once:
            break
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
