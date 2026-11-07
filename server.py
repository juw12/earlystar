"""EarlyStar local server.

Serves index.html and a JSON API, runs the collector every 30 minutes and a
market service that settles calls from the collected data.

    python server.py --dev-login --open   # local: http://127.0.0.1:8765, sign in with any name
    python server.py --public             # hosted: 0.0.0.0:$PORT, GitHub sign-in (see README)
    python server.py --no-collect         # API only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import markets

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
    def __init__(self, interval_minutes: float, enabled: bool, on_collected=None):
        self.interval = interval_minutes * 60
        self.enabled = enabled
        self.on_collected = on_collected
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
            if self.on_collected:
                self.on_collected()


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


# --------------------------------------------------------------------------- market service

class MarketService:
    """Settles calls from radar data every few minutes and auto-creates calls every 6 hours."""

    def __init__(self, auto_markets: bool, interval_seconds: float = 300):
        self.auto_markets = auto_markets
        self.interval = interval_seconds
        self.wake = threading.Event()
        self.state = {"last_run": None, "last_summary": None, "last_generated": None, "last_error": None}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="markets", daemon=True).start()

    def trigger(self) -> None:
        self.wake.set()

    def _loop(self) -> None:
        while True:
            self.run_once()
            self.wake.wait(self.interval)
            self.wake.clear()

    def run_once(self) -> None:
        try:
            conn = markets.connect_app()
            try:
                summary = markets.resolve_due(conn)
                self.state.update(last_run=time.time(), last_summary=summary, last_error=None)
                if summary["resolved"] or summary["void"] or summary["review"]:
                    log(f"markets: {summary}")
                if self.auto_markets:
                    generated = markets.run_job_if_due(conn, "generate", markets.AUTO_INTERVAL, markets.generate_markets)
                    if generated is not None:
                        self.state["last_generated"] = generated
                        log(f"markets: auto-generated {generated['created']}")
            finally:
                conn.close()
        except Exception as error:  # never let the thread die
            self.state["last_error"] = str(error)
            log(f"market service failed: {error!r}")


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


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# Installable web app (PWA) files: an explicit allow-list, never arbitrary paths.
PWA_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/maskable-512.png": ("icons/maskable-512.png", "image/png"),
}
MARKET_ROUTE = re.compile(r"/api/markets/(\d+)")
TRADE_ROUTE = re.compile(r"/api/markets/(\d+)/trade")
QUOTE_ROUTE = re.compile(r"/api/markets/(\d+)/quote")
RESOLVE_ROUTE = re.compile(r"/api/admin/markets/(\d+)/resolve")
USER_ROUTE = re.compile(r"/api/users/([A-Za-z0-9-~]{1,60})")


def int_param(params: dict, name: str, default: int, low: int, high: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{name} must be a whole number")
    return max(low, min(value, high))


def make_handler(service: CollectorService, market_service: MarketService, config: auth.AuthConfig, public: bool):
    stargain_cache = StarGainCache()

    class Handler(BaseHTTPRequestHandler):
        server_version = "EarlyStar/2.0"

        def log_message(self, fmt, *args):  # quiet access log; errors are logged explicitly
            pass

        # ---- response helpers
        def _common_headers(self):
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)

        def send_json(self, payload, status=HTTPStatus.OK, cookies=()):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)

        def send_error_json(self, status, message):
            self.send_json({"ok": False, "error": message}, status)

        def send_redirect(self, location: str, cookies=()):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self._common_headers()
            self.end_headers()

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            return body

        # ---- auth helpers
        def cookies(self) -> dict:
            return auth.parse_cookies(self.headers.get("Cookie"))

        def current_user(self, conn):
            return auth.user_for_token(conn, self.cookies().get(auth.SESSION_COOKIE))

        def require_user(self, conn):
            user = self.current_user(conn)
            if user is None:
                raise HttpError(HTTPStatus.UNAUTHORIZED, "Sign in to do that.")
            return user

        def require_admin(self, conn):
            user = self.require_user(conn)
            if not config.is_admin(user["login"]):
                raise HttpError(HTTPStatus.FORBIDDEN, "Admins only.")
            return user

        def check_csrf(self):
            if self.headers.get("X-EarlyStar") != "1":
                raise HttpError(HTTPStatus.FORBIDDEN, "Missing X-EarlyStar header.")
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urllib.parse.urlparse(origin).netloc
                if origin_host != self.headers.get("Host"):
                    raise HttpError(HTTPStatus.FORBIDDEN, "Cross-site request blocked.")

        def admin_if_public(self, conn=None):
            if not public:
                return
            own = conn is None
            conn = conn or markets.connect_app()
            try:
                self.require_admin(conn)
            finally:
                if own:
                    conn.close()

        def fail(self, error: Exception, method: str, path: str):
            if isinstance(error, HttpError):
                return self.send_error_json(error.status, str(error))
            if isinstance(error, markets.MarketError):
                return self.send_error_json(error.status, str(error))
            if isinstance(error, (ValueError, json.JSONDecodeError)):
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            log(f"{method} {path} failed: {error!r}")
            message = "Internal error — please try again." if public else str(error)
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, message)

        # ---- routing
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            path = parsed.path
            try:
                if path in ("/", "/index.html"):
                    return self.serve_index()
                if path in PWA_FILES:
                    return self.serve_static(PWA_FILES[path])
                if path == "/auth/login":
                    return self.auth_login(params)
                if path == "/auth/callback":
                    return self.auth_callback(params)
                if path == "/healthz":
                    return self.send_json({"ok": True})
                if not path.startswith("/api/"):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                if self.market_get(path, params):
                    return None
                return self.radar_get(path, params)
            except Exception as error:
                self.fail(error, "GET", path)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                self.check_csrf()
                body = self.read_json()
                if path.startswith("/auth/"):
                    return self.auth_post(path, body)
                if self.market_post(path, body):
                    return None
                return self.radar_post(path, body)
            except Exception as error:
                self.fail(error, "POST", path)

        # ---- auth routes
        def auth_login(self, params):
            if not config.github_enabled:
                return self.send_redirect("/#calls")
            state = auth.new_state()
            next_url = auth.safe_next(params.get("next"))
            cookie_value = urllib.parse.quote(state + "|" + next_url, safe="")
            cookie = auth.cookie_header(auth.STATE_COOKIE, cookie_value, 600, config.secure_cookies, "/auth/")
            return self.send_redirect(auth.authorize_url(config, state), [cookie])

        def auth_callback(self, params):
            if not config.github_enabled:
                return self.send_redirect("/")
            stored = urllib.parse.unquote(self.cookies().get(auth.STATE_COOKIE, ""))
            state, _, next_url = stored.partition("|")
            clear_state = auth.cookie_header(auth.STATE_COOKIE, "", 0, config.secure_cookies, "/auth/")
            if not state or not secrets_compare(state, params.get("state", "")):
                return self.send_redirect("/#signin=failed", [clear_state])
            if params.get("error"):
                return self.send_redirect("/#signin=cancelled", [clear_state])
            conn = markets.connect_app()
            try:
                user = auth.github_login(conn, config, params.get("code", ""))
                token = auth.create_session(conn, user["id"])
            finally:
                conn.close()
            session = auth.cookie_header(auth.SESSION_COOKIE, token, auth.SESSION_DAYS * 86400, config.secure_cookies)
            return self.send_redirect(auth.safe_next(next_url), [clear_state, session])

        def auth_post(self, path, body):
            conn = markets.connect_app()
            try:
                if path == "/auth/logout":
                    auth.delete_session(conn, self.cookies().get(auth.SESSION_COOKIE))
                    cookie = auth.cookie_header(auth.SESSION_COOKIE, "", 0, config.secure_cookies)
                    return self.send_json({"ok": True}, cookies=[cookie])
                if path == "/auth/dev":
                    if not config.dev_login:
                        raise HttpError(HTTPStatus.NOT_FOUND, "Dev login is disabled.")
                    user = auth.dev_login(conn, str(body.get("login", "")))
                    token = auth.create_session(conn, user["id"])
                    cookie = auth.cookie_header(auth.SESSION_COOKIE, token, auth.SESSION_DAYS * 86400, config.secure_cookies)
                    return self.send_json({"ok": True, "login": user["login"]}, cookies=[cookie])
                raise HttpError(HTTPStatus.NOT_FOUND, "unknown endpoint")
            finally:
                conn.close()

        # ---- market routes
        def market_get(self, path, params) -> bool:
            routes = ("/api/me", "/api/markets", "/api/markets/suggest", "/api/portfolio",
                      "/api/leaderboard", "/api/odds", "/api/admin/review")
            dynamic = MARKET_ROUTE.fullmatch(path) or QUOTE_ROUTE.fullmatch(path) or USER_ROUTE.fullmatch(path)
            if path not in routes and not dynamic:
                return False
            now = time.time()
            conn = markets.connect_app()
            try:
                user = self.current_user(conn)
                user_id = user["id"] if user else None
                if path == "/api/me":
                    self.send_json(self.me_payload(conn, user, now))
                elif path == "/api/markets":
                    self.send_json(markets.list_markets(
                        conn, now,
                        status=params.get("status", "open"),
                        kind=params.get("kind") or None,
                        repo=params.get("repo") or None,
                        user_id=user_id,
                        mine=params.get("mine") == "1",
                        sort=params.get("sort", "volume"),
                        limit=int_param(params, "limit", 100, 1, 500),
                        query=(params.get("q") or "")[:100] or None,
                    ))
                elif path == "/api/markets/suggest":
                    target = params.get("target")
                    self.send_json(markets.suggest(
                        conn, params.get("repo", ""), now,
                        target=int_param(params, "target", 0, 1, 10 ** 9) if target else None,
                        days=int_param(params, "days", 30, 1, 365),
                    ))
                elif path == "/api/portfolio":
                    if user is None:
                        raise HttpError(HTTPStatus.UNAUTHORIZED, "Sign in to see your portfolio.")
                    self.send_json(markets.portfolio(conn, user_id, now))
                elif path == "/api/leaderboard":
                    self.send_json(markets.leaderboard(conn, now, params.get("period", "all")))
                elif path == "/api/odds":
                    self.send_json({"items": markets.odds_by_repo(conn, now)})
                elif path == "/api/admin/review":
                    self.require_admin(conn)
                    self.send_json({"items": markets.review_queue(conn, now)})
                elif QUOTE_ROUTE.fullmatch(path):
                    market_id = int(QUOTE_ROUTE.fullmatch(path).group(1))
                    self.send_json(markets.quote(conn, market_id, params.get("side"), params.get("action"),
                                                 params.get("amount"), now))
                elif MARKET_ROUTE.fullmatch(path):
                    market_id = int(MARKET_ROUTE.fullmatch(path).group(1))
                    self.send_json(markets.market_detail(conn, market_id, now, user_id))
                else:
                    self.send_json(markets.profile(conn, USER_ROUTE.fullmatch(path).group(1), now))
            finally:
                conn.close()
            return True

        def me_payload(self, conn, user, now):
            payload = {
                "user": None,
                "auth": {"github": config.github_enabled, "dev": config.dev_login},
                "economy": {
                    "starting_balance": markets.STARTING_BALANCE,
                    "daily_bonus": markets.DAILY_BONUS,
                    "create_fee": markets.CREATE_FEE,
                    "max_stake": markets.MAX_STAKE_PER_MARKET,
                    "min_account_age_days": markets.MIN_ACCOUNT_AGE_DAYS,
                },
            }
            if user is not None:
                bonus = markets.claim_daily_bonus(conn, user["id"], now)
                fresh = markets.get_user(conn, user["id"])
                payload["user"] = {
                    "login": fresh["login"],
                    "name": fresh["name"],
                    "avatar_url": fresh["avatar_url"],
                    "balance": round(fresh["balance"], 2),
                    "is_admin": config.is_admin(fresh["login"]),
                    "trade_block_reason": markets.trade_block_reason(fresh, now),
                    "bonus_credited": bonus,
                }
            return payload

        def market_post(self, path, body) -> bool:
            if path not in ("/api/markets",) and not (TRADE_ROUTE.fullmatch(path) or RESOLVE_ROUTE.fullmatch(path)):
                return False
            now = time.time()
            conn = markets.connect_app()
            try:
                if path == "/api/markets":
                    user = self.require_user(conn)
                    market_id = markets.create_user_market(conn, user, body, now)
                    self.send_json({"ok": True, "market": markets.market_detail(conn, market_id, now, user["id"])},
                                   HTTPStatus.CREATED)
                elif TRADE_ROUTE.fullmatch(path):
                    user = self.require_user(conn)
                    market_id = int(TRADE_ROUTE.fullmatch(path).group(1))
                    result = markets.execute_trade(
                        conn, user["id"], market_id, body.get("side"), body.get("action"), body.get("amount"),
                        min_shares=body.get("min_shares"), min_points=body.get("min_points"), now=now,
                    )
                    self.send_json({"ok": True, **result})
                else:
                    self.require_admin(conn)
                    market_id = int(RESOLVE_ROUTE.fullmatch(path).group(1))
                    markets.admin_resolve(conn, market_id, str(body.get("outcome", "")),
                                          str(body.get("note", ""))[:300], now)
                    self.send_json({"ok": True, "market": markets.market_detail(conn, market_id, now)})
            finally:
                conn.close()
            return True

        # ---- radar routes (RepoRadar)
        def radar_get(self, path, params):
            conn = connect()
            try:
                if path == "/api/health":
                    health = query_health(conn, service)
                    health["markets"] = market_service.state
                    health["public"] = public
                    return self.send_json(health)
                if path == "/api/movers":
                    window = params.get("window", "24h")
                    if window not in WINDOWS:
                        return self.send_error_json(HTTPStatus.BAD_REQUEST, "window must be 24h, 7d or 30d")
                    return self.send_json(
                        query_movers(
                            conn,
                            window,
                            params.get("lang") or None,
                            int_param(params, "min_stars", 0, 0, 10 ** 9),
                            int_param(params, "limit", 200, 1, 1000),
                        )
                    )
                if path == "/api/trending":
                    since = params.get("since", "daily")
                    if since not in ("daily", "weekly", "monthly"):
                        return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad since")
                    return self.send_json(query_trending(conn, since, params.get("day"), params.get("language", "")))
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
                    if public:
                        app = markets.connect_app()
                        try:
                            self.require_user(app)
                        finally:
                            app.close()
                    return self.serve_stargains(conn, f"{match.group(1)}/{match.group(2)}")
                return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")
            finally:
                conn.close()

        def radar_post(self, path, body):
            if path == "/api/collect":
                self.admin_if_public()
                started = service.trigger()
                return self.send_json({"ok": True, "started": started, "running": service.state["running"]})
            if path == "/api/watch":
                if public:
                    # The server-side watchlist is shared; on a public server only admins may change it.
                    return self.send_json({"ok": True, "ignored": True})
                name = str(body.get("full_name", "")).strip()
                if not REPO_NAME_RE.fullmatch(name) or ".." in name:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "full_name must look like owner/repo")
                conn = connect()
                try:
                    with conn:
                        if body.get("on", True):
                            conn.execute("INSERT OR IGNORE INTO watch (full_name, added_at) VALUES (?, ?)", (name, time.time()))
                        else:
                            conn.execute("DELETE FROM watch WHERE full_name = ?", (name,))
                finally:
                    conn.close()
                return self.send_json({"ok": True})
            if path == "/api/token":
                self.admin_if_public()
                save_token(str(body.get("token", "")))
                return self.send_json({"ok": True, "token": bool(load_token())})
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")

        # ---- handlers
        def serve_index(self):
            body = (BASE_DIR / "index.html").read_bytes().replace(
                b'<meta name="earlystar-host" content="">', b'<meta name="earlystar-host" content="server">', 1
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)

        def serve_static(self, entry):
            relative, content_type = entry
            body = (BASE_DIR / relative).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if relative == "sw.js" else "public, max-age=86400")
            self._common_headers()
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


def secrets_compare(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def build_server(host: str, port: int, interval: float, collect: bool, auto_markets: bool,
                 config: auth.AuthConfig, public: bool):
    connect().close()               # radar schema (and reporadar.db → radar.db migration)
    markets.connect_app().close()   # app schema
    market_service = MarketService(auto_markets)
    service = CollectorService(interval, enabled=collect, on_collected=market_service.trigger)
    server = ThreadingHTTPServer((host, port), make_handler(service, market_service, config, public))
    return server, service, market_service


def main() -> None:
    parser = argparse.ArgumentParser(description="EarlyStar server")
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1, or 0.0.0.0 with --public)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--interval", type=float, default=30, help="minutes between collections")
    parser.add_argument("--no-collect", action="store_true", help="serve only; do not collect")
    parser.add_argument("--no-auto-markets", action="store_true", help="don't auto-create calls from the radar")
    parser.add_argument("--public", action="store_true", help="hosted mode: lock down single-user endpoints")
    parser.add_argument("--dev-login", action="store_true", help="local testing: sign in with any username")
    parser.add_argument("--open", action="store_true", help="open the app in the default browser")
    args = parser.parse_args()

    if args.public and args.dev_login:
        parser.error("--dev-login can't be combined with --public (anyone could sign in as anyone)")
    config = auth.AuthConfig.from_env(dev_login=args.dev_login)
    host = args.host or ("0.0.0.0" if args.public else "127.0.0.1")
    server, service, market_service = build_server(
        host, args.port, args.interval, not args.no_collect, not args.no_auto_markets, config, args.public
    )
    url = f"http://127.0.0.1:{args.port}/"
    log(f"EarlyStar running at {url}  (radar: {DB_PATH}, app: {markets.APP_DB_PATH})")
    sign_in = []
    if config.github_enabled:
        sign_in.append(f"GitHub ({config.base_url})")
    if config.dev_login:
        sign_in.append("dev login")
    log("sign-in: " + (", ".join(sign_in) or "none configured — set EARLYSTAR_GITHUB_* or use --dev-login"))
    service.start()
    market_service.start()
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
