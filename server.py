"""EarlyStar local server.

Serves index.html plus a small JSON API backed by the SQLite history that
collector.py builds, and runs the collector on a schedule in the background.

    python server.py               # http://127.0.0.1:8765, collects every 30 min
    python server.py --open        # also opens the browser
    python server.py --no-collect  # API only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collector import (
    BASE_DIR,
    DB_PATH,
    Collector,
    GitHubClient,
    connect,
    iso_to_ts,
    load_token,
    save_token,
)

WINDOWS = {
    # window: (days, matching GitHub Trending period, minimum history to project from)
    "24h": (1, "daily", 3 * 3600),
    "7d": (7, "weekly", 12 * 3600),
    "30d": (30, "monthly", 2 * 86400),
}
BASE_SLACK_SECONDS = 3600  # a snapshot up to 1h after the window start still counts as the baseline
MAX_BODY_BYTES = 16 * 1024
REPO_NAME_RE = re.compile(r"[A-Za-z0-9][\w-]*/[A-Za-z0-9_.][\w.-]*")


def log(message: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {message}", flush=True)


# --------------------------------------------------------------------------- background collector

class CollectorService:
    def __init__(self, interval_minutes: float, enabled: bool):
        self.interval = interval_minutes * 60
        self.enabled = enabled
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.state = {
            "enabled": enabled,
            "running": False,
            "last_started": None,
            "last_finished": None,
            "last_summary": None,
            "last_error": None,
            "next_run": None,
            "log": [],
        }

    def _log(self, message: str) -> None:
        log(message)
        with self.lock:
            self.state["log"] = (self.state["log"] + [f"{dt.datetime.now():%H:%M:%S} {message}"])[-30:]

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._loop, name="collector", daemon=True).start()

    def trigger(self) -> bool:
        if not self.enabled or self.state["running"]:
            return False
        self.wake.set()
        return True

    def _loop(self) -> None:
        while True:
            self._collect()
            self.state["next_run"] = time.time() + self.interval
            self.wake.wait(self.interval)
            self.wake.clear()

    def _collect(self) -> None:
        self.state.update(running=True, last_started=time.time(), last_error=None)
        self._log("collection started")
        try:
            summary = Collector(log=self._log).run_once()
            self.state["last_summary"] = summary
            self._log(
                f"collection done in {summary['duration']}s: {summary['snapshots']} repos snapshotted, "
                f"{summary['trending_rows']} trending rows, {len(summary['errors'])} errors"
            )
        except Exception as error:  # never let the thread die
            self.state["last_error"] = str(error)
            self._log(f"collection failed: {error}")
        finally:
            self.state.update(running=False, last_finished=time.time())


# --------------------------------------------------------------------------- queries

def repo_to_dict(row) -> dict:
    keys = row.keys()

    def get(name, default=None):
        return row[name] if name in keys and row[name] is not None else default

    full_name = get("full_name", "")
    engagement = None
    if get("commits_7d") is not None or get("merged_prs_7d") is not None:
        engagement = {
            "commits_7d": get("commits_7d"),
            "merged_prs_7d": get("merged_prs_7d"),
            "new_issues_7d": get("new_issues_7d"),
            "release_tag": get("release_tag"),
            "release_at": get("release_at"),
        }
    return {
        "engagement": engagement,
        "id": get("id") or get("repo_id"),
        "full_name": full_name,
        "html_url": get("html_url") or f"https://github.com/{full_name}",
        "description": get("description"),
        "language": get("language"),
        "stargazers_count": get("stars", 0),
        "forks_count": get("forks", 0),
        "open_issues_count": get("open_issues", 0),
        "created_at": get("created_at"),
        "pushed_at": get("pushed_at") or get("created_at"),
        "topics": json.loads(get("topics", "[]")),
        "license": {"spdx_id": get("license")} if get("license") else None,
        "homepage": get("homepage"),
        "owner": {
            "login": get("owner_login") or full_name.split("/")[0],
            "avatar_url": get("avatar_url") or f"https://github.com/{full_name.split('/')[0]}.png?size=44",
        },
    }


MOVERS_SQL = """
WITH latest AS (SELECT repo_id, MAX(ts) AS ts FROM snapshots GROUP BY repo_id),
     first  AS (SELECT repo_id, MIN(ts) AS ts FROM snapshots GROUP BY repo_id),
     base   AS (SELECT repo_id, MAX(ts) AS ts FROM snapshots WHERE ts <= :base_limit GROUP BY repo_id)
SELECT r.*,
       cs.stars AS cur_stars, cs.ts AS cur_ts,
       fs.stars AS first_stars, fs.ts AS first_ts,
       bs.stars AS base_stars, bs.ts AS base_ts,
       e.commits_7d, e.merged_prs_7d, e.new_issues_7d, e.release_tag, e.release_at
FROM repos r
LEFT JOIN engagement e ON e.repo_id = r.id
JOIN latest l     ON l.repo_id = r.id
JOIN snapshots cs ON cs.repo_id = l.repo_id AND cs.ts = l.ts
JOIN first f      ON f.repo_id = r.id
JOIN snapshots fs ON fs.repo_id = f.repo_id AND fs.ts = f.ts
LEFT JOIN base b       ON b.repo_id = r.id
LEFT JOIN snapshots bs ON bs.repo_id = b.repo_id AND bs.ts = b.ts
WHERE (:language IS NULL OR r.language = :language)
  AND cs.stars >= :min_stars
"""


def latest_trending_gains(conn, since: str) -> dict[str, int]:
    """Most recent 'stars today / this week / this month' figure per repo from GitHub Trending."""
    rows = conn.execute(
        """
        SELECT full_name, period_stars FROM trending t
        WHERE since = ? AND period_stars IS NOT NULL
          AND day = (SELECT MAX(day) FROM trending t2 WHERE t2.since = t.since AND t2.full_name = t.full_name)
        """,
        (since,),
    )
    gains: dict[str, int] = {}
    for name, stars in rows:
        gains[name.lower()] = max(gains.get(name.lower(), 0), stars)
    return gains


def compute_mover(row, now: float, days: int, trending_gain: int | None, min_projection_seconds: float):
    """Return (gain, source, coverage_seconds) or None when there is not enough evidence."""
    cut = now - days * 86400
    created = iso_to_ts(row["created_at"]) or 0
    current = row["cur_stars"]
    if created >= cut:
        # Born inside the window: every star it has was gained inside the window.
        return current, "exact-new", max(row["cur_ts"] - created, 3600)
    if row["base_ts"] is not None:
        return current - row["base_stars"], "tracked", max(row["cur_ts"] - row["base_ts"], 1)
    if trending_gain is not None:
        return trending_gain, "github-trending", days * 86400
    observed_span = row["cur_ts"] - row["first_ts"]
    if observed_span >= min_projection_seconds:
        rate = (current - row["first_stars"]) / observed_span
        return round(rate * days * 86400), "projected", observed_span
    return None


def query_movers(conn, window: str, language: str | None, min_stars: int, limit: int) -> dict:
    days, since, min_projection = WINDOWS[window]
    now = time.time()
    cut = now - days * 86400
    rows = conn.execute(
        MOVERS_SQL,
        {"base_limit": cut + BASE_SLACK_SECONDS, "language": language or None, "min_stars": min_stars},
    ).fetchall()
    trending_gains = latest_trending_gains(conn, since)
    appearances = trending_appearance_counts(conn)
    movers = []
    for row in rows:
        result = compute_mover(row, now, days, trending_gains.get(row["full_name"].lower()), min_projection)
        if result is None:
            continue
        gain, source, coverage = result
        repo = repo_to_dict(row)
        repo["stargazers_count"] = row["cur_stars"]
        repo.update(
            gain=gain,
            gain_per_day=gain / max(min(coverage, days * 86400) / 86400, 1 / 24),
            gain_source=source,
            coverage_hours=round(coverage / 3600, 1),
            trending_days=appearances.get(row["full_name"].lower(), 0),
        )
        movers.append(repo)
    movers.sort(key=lambda repo: repo["gain"], reverse=True)
    return {"window": window, "generated_at": now, "count": len(movers), "items": movers[:limit]}


def trending_appearance_counts(conn) -> dict[str, int]:
    rows = conn.execute("SELECT full_name, COUNT(DISTINCT day) FROM trending WHERE since = 'daily' GROUP BY full_name COLLATE NOCASE")
    return {name.lower(): count for name, count in rows}


def trending_streak(days_present: set[str], end_day: str) -> int:
    streak = 0
    current = dt.date.fromisoformat(end_day)
    while current.isoformat() in days_present:
        streak += 1
        current -= dt.timedelta(days=1)
    return streak


def query_trending(conn, since: str, day: str | None, language: str) -> dict:
    if not day:
        row = conn.execute("SELECT MAX(day) FROM trending WHERE since = ? AND language = ?", (since, language)).fetchone()
        day = row[0]
    if not day:
        return {"day": None, "since": since, "language": language, "items": []}
    rows = conn.execute(
        """
        SELECT t.rank, t.full_name, t.period_stars, t.stars AS t_stars, t.forks AS t_forks,
               t.repo_language, t.description AS t_description,
               r.id, r.owner_login, r.avatar_url, r.html_url, r.description, r.language, r.topics, r.license,
               r.homepage, r.created_at, r.pushed_at, r.stars, r.forks, r.open_issues,
               e.commits_7d, e.merged_prs_7d, e.new_issues_7d, e.release_tag, e.release_at
        FROM trending t
        LEFT JOIN repos r ON r.id = (
            SELECT id FROM repos WHERE full_name = t.full_name ORDER BY last_seen DESC LIMIT 1)
        LEFT JOIN engagement e ON e.repo_id = r.id
        WHERE t.day = ? AND t.since = ? AND t.language = ?
        ORDER BY t.rank
        """,
        (day, since, language),
    ).fetchall()
    daily_days: dict[str, set[str]] = {}
    for name, seen_day in conn.execute("SELECT full_name, day FROM trending WHERE since = 'daily'"):
        daily_days.setdefault(name.lower(), set()).add(seen_day)
    items = []
    for row in rows:
        repo = repo_to_dict(row)
        repo["id"] = row["id"] or f"t:{row['full_name']}"
        repo["full_name"] = row["full_name"]
        repo["description"] = row["description"] or row["t_description"]
        repo["language"] = row["language"] or row["repo_language"]
        repo["stargazers_count"] = row["t_stars"] or row["stars"] or 0
        repo["forks_count"] = row["t_forks"] or row["forks"] or 0
        present = daily_days.get(row["full_name"].lower(), set())
        repo.update(
            trending_rank=row["rank"],
            period_stars=row["period_stars"],
            trending_days=len(present),
            trending_streak=trending_streak(present, day),
            first_trended=min(present) if present else day,
        )
        items.append(repo)
    return {"day": day, "since": since, "language": language, "items": items}


def query_history(conn, repo_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT r.*, e.commits_7d, e.merged_prs_7d, e.new_issues_7d, e.release_tag, e.release_at
        FROM repos r LEFT JOIN engagement e ON e.repo_id = r.id
        WHERE r.id = ?
        """,
        (repo_id,),
    ).fetchone()
    if row is None:
        return None
    points = conn.execute(
        "SELECT ts, stars, forks FROM snapshots WHERE repo_id = ? ORDER BY ts", (repo_id,)
    ).fetchall()
    trending = conn.execute(
        "SELECT day, since, language, rank, period_stars FROM trending WHERE full_name = ? ORDER BY day DESC, since",
        (row["full_name"],),
    ).fetchall()
    return {
        "repo": repo_to_dict(row),
        "points": [[round(p["ts"] * 1000), p["stars"], p["forks"]] for p in points],
        "trending": [dict(t) for t in trending],
    }


def query_health(conn, service: CollectorService) -> dict:
    counts = {
        "repos": conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
        "snapshots": conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
        "trending_rows": conn.execute("SELECT COUNT(*) FROM trending").fetchone()[0],
        "trending_days": conn.execute("SELECT COUNT(DISTINCT day) FROM trending").fetchone()[0],
        "watch": conn.execute("SELECT COUNT(*) FROM watch").fetchone()[0],
    }
    first = conn.execute("SELECT MIN(ts) FROM snapshots").fetchone()[0]
    last_run = conn.execute("SELECT ts, duration, ok, detail FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return {
        "ok": True,
        "counts": counts,
        "runs": runs,
        "history_since": first,
        "last_run": dict(last_run) if last_run else None,
        "collector": {key: value for key, value in service.state.items()},
        "token": bool(load_token()),
        "db_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
    }


# --------------------------------------------------------------------------- HTTP

class StarGainCache:
    def __init__(self):
        self.entries: dict[str, tuple[float, dict]] = {}
        self.lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self.lock:
            hit = self.entries.get(key)
        return hit[1] if hit and time.time() - hit[0] < 900 else None

    def put(self, key: str, value: dict) -> None:
        with self.lock:
            self.entries[key] = (time.time(), value)


def make_handler(service: CollectorService):
    stargain_cache = StarGainCache()

    class Handler(BaseHTTPRequestHandler):
        server_version = "EarlyStar/2.0"

        def log_message(self, fmt, *args):  # quiet access log; errors are logged explicitly
            pass

        # ---- helpers
        def send_json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_error_json(self, status, message):
            self.send_json({"ok": False, "error": message}, status)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        # ---- routing
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            path = parsed.path
            try:
                if path in ("/", "/index.html"):
                    return self.serve_index()
                if not path.startswith("/api/"):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                conn = connect()
                try:
                    if path == "/api/health":
                        return self.send_json(query_health(conn, service))
                    if path == "/api/movers":
                        window = params.get("window", "24h")
                        if window not in WINDOWS:
                            return self.send_error_json(HTTPStatus.BAD_REQUEST, "window must be 24h, 7d or 30d")
                        return self.send_json(
                            query_movers(
                                conn,
                                window,
                                params.get("lang") or None,
                                int(params.get("min_stars") or 0),
                                min(int(params.get("limit") or 200), 1000),
                            )
                        )
                    if path == "/api/trending":
                        since = params.get("since", "daily")
                        if since not in ("daily", "weekly", "monthly"):
                            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad since")
                        return self.send_json(
                            query_trending(conn, since, params.get("day"), params.get("language", ""))
                        )
                    if path == "/api/trending/days":
                        days = [row[0] for row in conn.execute("SELECT DISTINCT day FROM trending ORDER BY day DESC")]
                        languages = [row[0] for row in conn.execute("SELECT DISTINCT language FROM trending ORDER BY language")]
                        return self.send_json({"days": days, "languages": languages})
                    if path == "/api/watch":
                        names = [row[0] for row in conn.execute("SELECT full_name FROM watch ORDER BY added_at")]
                        return self.send_json({"items": names})
                    match = re.fullmatch(r"/api/repo/(\d+)/history", path)
                    if match:
                        history = query_history(conn, int(match.group(1)))
                        if history is None:
                            return self.send_error_json(HTTPStatus.NOT_FOUND, "repo not tracked yet")
                        return self.send_json(history)
                    match = re.fullmatch(r"/api/stargains/([\w.-]+)/([\w.-]+)", path)
                    if match and REPO_NAME_RE.fullmatch(f"{match.group(1)}/{match.group(2)}") and ".." not in path:
                        return self.serve_stargains(conn, f"{match.group(1)}/{match.group(2)}")
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")
                finally:
                    conn.close()
            except Exception as error:
                log(f"GET {path} failed: {error!r}")
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                body = self.read_json()
                if path == "/api/collect":
                    started = service.trigger()
                    return self.send_json({"ok": True, "started": started, "running": service.state["running"]})
                if path == "/api/watch":
                    name = str(body.get("full_name", "")).strip()
                    if not REPO_NAME_RE.fullmatch(name) or ".." in name:
                        return self.send_error_json(HTTPStatus.BAD_REQUEST, "full_name must look like owner/repo")
                    conn = connect()
                    try:
                        with conn:
                            if body.get("on", True):
                                conn.execute(
                                    "INSERT OR IGNORE INTO watch (full_name, added_at) VALUES (?, ?)", (name, time.time())
                                )
                            else:
                                conn.execute("DELETE FROM watch WHERE full_name = ?", (name,))
                    finally:
                        conn.close()
                    return self.send_json({"ok": True})
                if path == "/api/token":
                    save_token(str(body.get("token", "")))
                    return self.send_json({"ok": True, "token": bool(load_token())})
                return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")
            except (ValueError, json.JSONDecodeError) as error:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            except Exception as error:
                log(f"POST {path} failed: {error!r}")
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        # ---- handlers
        def serve_index(self):
            body = (BASE_DIR / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def serve_stargains(self, conn, full_name: str):
            cached = stargain_cache.get(full_name.lower())
            if cached:
                return self.send_json(cached)
            client = GitHubClient(load_token(), log)
            if not client.token:
                return self.send_error_json(HTTPStatus.FORBIDDEN, "Add a GitHub token to see exact star gains.")
            row = conn.execute("SELECT stars FROM repos WHERE full_name = ? ORDER BY last_seen DESC", (full_name,)).fetchone()
            if row is None:
                data, _ = client.api("/repos/" + full_name)
                stars = data["stargazers_count"]
            else:
                stars = row[0]
            result = client.stargazer_gains(full_name, stars)
            stargain_cache.put(full_name.lower(), result)
            return self.send_json(result)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="EarlyStar local server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=30, help="minutes between collections")
    parser.add_argument("--no-collect", action="store_true", help="serve only; do not collect")
    parser.add_argument("--open", action="store_true", help="open the app in the default browser")
    args = parser.parse_args()

    connect().close()  # create the database/schema up front
    service = CollectorService(args.interval, enabled=not args.no_collect)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(service))
    url = f"http://127.0.0.1:{args.port}/"
    log(f"EarlyStar running at {url}  (database: {DB_PATH})")
    service.start()
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
