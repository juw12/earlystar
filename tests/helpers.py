"""Shared fixtures: a throwaway radar DB + app DB with helpers to fake GitHub data."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path

import markets

DAY = 86400
NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.radar_path = root / "radar.db"
        self.app_path = root / "earlystar.db"
        self.conn = markets.connect_app(self.app_path, self.radar_path)
        self.conn.execute("INSERT INTO radar.runs (ts, duration, ok, detail) VALUES (?, 1, 1, '{}')", (NOW - 600,))
        self._next_github_id = 1000

    def close(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---- radar data
    def add_repo(self, repo_id: int, full_name: str, stars: int, ts: float = NOW,
                 created_days_ago: float = 400, language: str = "Python") -> None:
        owner = full_name.split("/")[0]
        self.conn.execute(
            """INSERT OR REPLACE INTO radar.repos (id, full_name, owner_login, avatar_url, html_url, description,
                   language, topics, created_at, pushed_at, stars, forks, open_issues, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, 0, 0, ?, ?)""",
            (repo_id, full_name, owner, f"https://avatars.example/{owner}", f"https://github.com/{full_name}",
             "test repo", language, iso(ts - created_days_ago * DAY), iso(ts), stars, ts, ts),
        )
        self.snap(repo_id, ts, stars)

    def snap(self, repo_id: int, ts: float, stars: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO radar.snapshots (repo_id, ts, stars, forks) VALUES (?, ?, ?, 0)",
            (repo_id, ts, stars),
        )
        self.conn.execute(
            "UPDATE radar.repos SET stars = ?, last_seen = MAX(last_seen, ?) WHERE id = ? AND last_seen <= ?",
            (stars, ts, repo_id, ts),
        )

    def trending(self, day: dt.date, full_name: str, rank: int = 1, language: str = "") -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO radar.trending (day, since, language, rank, full_name, period_stars, stars,
                   forks, repo_language, description, captured_at)
               VALUES (?, 'daily', ?, ?, ?, 100, 1000, 10, 'Python', '', ?)""",
            (day.isoformat(), language, rank, full_name, NOW),
        )

    # ---- users
    def user(self, login: str, age_days: float = 400, now: float = NOW):
        self._next_github_id += 1
        return markets.upsert_user(
            self.conn, github_id=self._next_github_id, login=login, name=login,
            avatar_url=f"https://avatars.example/{login}", github_created_at=iso(now - age_days * DAY), now=now,
        )

    def balance(self, user) -> float:
        return markets.get_user(self.conn, user["id"])["balance"]

    # ---- markets
    def stars_market(self, repo_id: int = 1, target: int = 1200, days: int = 10, prob: float = 0.5,
                     now: float = NOW, liquidity: float = 200.0) -> int:
        repo = markets.repo_state(self.conn, repo_id=repo_id, now=now)
        with markets.transaction(self.conn):
            return markets._insert_market(
                self.conn, kind="stars_reach", repo=repo, repo_b=None, target=target, window_start=None,
                window_days=None, open_ts=now, close_ts=markets.deadline_ts(now, days), created_by=None,
                liquidity=liquidity, initial_prob=prob,
            )

    def versus_market(self, repo_a: int, repo_b: int, days: int = 7, now: float = NOW) -> int:
        a = markets.repo_state(self.conn, repo_id=repo_a, now=now)
        b = markets.repo_state(self.conn, repo_id=repo_b, now=now)
        with markets.transaction(self.conn):
            return markets._insert_market(
                self.conn, kind="versus", repo=a, repo_b=b, target=None, window_start=None, window_days=None,
                open_ts=now, close_ts=markets.deadline_ts(now, days), created_by=None,
                liquidity=200.0, initial_prob=0.5,
            )

    def trending_market(self, repo_id: int, needed: int = 4, days: int = 7, now: float = NOW) -> int:
        repo = markets.repo_state(self.conn, repo_id=repo_id, now=now)
        start = markets.utc_day(now) + dt.timedelta(days=1)
        with markets.transaction(self.conn):
            return markets._insert_market(
                self.conn, kind="trending_days", repo=repo, repo_b=None, target=needed, window_start=start,
                window_days=days, open_ts=now, close_ts=markets.day_start_ts(start + dt.timedelta(days=days)),
                created_by=None, liquidity=200.0, initial_prob=0.5,
            )

    def market(self, market_id: int):
        return markets.market_row(self.conn, market_id)


def fake_github_repo(repo_id: int, full_name: str, stars: int, created: str = "2024-01-01T00:00:00Z") -> dict:
    owner = full_name.split("/")[0]
    return json.loads(json.dumps({
        "id": repo_id,
        "full_name": full_name,
        "owner": {"login": owner, "avatar_url": f"https://avatars.example/{owner}"},
        "html_url": f"https://github.com/{full_name}",
        "description": "fetched",
        "language": "Rust",
        "topics": [],
        "license": None,
        "homepage": None,
        "created_at": created,
        "pushed_at": created,
        "stargazers_count": stars,
        "forks_count": 1,
        "open_issues_count": 0,
        "private": False,
        "archived": False,
    }))
