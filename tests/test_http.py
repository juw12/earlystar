"""End-to-end tests against a real EarlyStar HTTP server on a random port."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

import auth
import markets
import server

DAY = 86400


class Client:
    """Tiny HTTP client that keeps cookies and sends the CSRF header like the web app."""

    def __init__(self, base: str):
        self.base = base
        self.cookies = {}

    def call(self, method: str, path: str, body=None, csrf: bool = True, headers=None, raw_body: bytes = None):
        data = raw_body if raw_body is not None else (json.dumps(body or {}).encode() if method == "POST" else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if csrf and method == "POST":
            request.add_header("X-EarlyStar", "1")
        if self.cookies:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            response = urllib.request.urlopen(request, timeout=15)
            status, payload, response_headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            status, payload, response_headers = error.code, error.read(), error.headers
        for cookie in response_headers.get_all("Set-Cookie") or []:
            name, _, rest = cookie.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in cookie:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        if "application/json" in (response_headers.get("Content-Type") or ""):
            payload = json.loads(payload or b"null")
        return status, payload, response_headers

    def get(self, path, **kwargs):
        return self.call("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.call("POST", path, body, **kwargs)

    def login(self, name: str):
        status, payload, _ = self.post("/auth/dev", {"login": name})
        assert status == 200, payload
        return payload


def start(public: bool):
    config = auth.AuthConfig(dev_login=True, admins={"boss"})
    httpd, _, _ = server.build_server("127.0.0.1", 0, 30, collect=False, auto_markets=False, config=config, public=public)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def seed_repo(repo_id: int, full_name: str, stars: int) -> None:
    conn = markets.connect_app()
    now = time.time()
    try:
        owner = full_name.split("/")[0]
        conn.execute(
            """INSERT OR REPLACE INTO radar.repos (id, full_name, owner_login, avatar_url, html_url, description, language,
                   topics, created_at, pushed_at, stars, forks, open_issues, first_seen, last_seen)
               VALUES (?, ?, ?, '', '', 'seeded', 'Go', '[]', '2023-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?, 0, 0, ?, ?)""",
            (repo_id, full_name, owner, stars, now - 8 * DAY, now),
        )
        conn.execute("INSERT OR REPLACE INTO radar.snapshots (repo_id, ts, stars, forks) VALUES (?, ?, ?, 0)",
                     (repo_id, now - 7 * DAY, stars - 350))
        conn.execute("INSERT OR REPLACE INTO radar.snapshots (repo_id, ts, stars, forks) VALUES (?, ?, ?, 0)",
                     (repo_id, now - 60, stars))
    finally:
        conn.close()


class HttpFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = start(public=False)
        seed_repo(101, "acme/rocket", 1000)
        seed_repo(102, "beta/boat", 3000)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_index_and_health(self):
        client = Client(self.base)
        status, body, headers = client.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"EarlyStar", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        for path, kind in (("/manifest.webmanifest", "manifest"), ("/sw.js", "javascript"), ("/icons/icon-192.png", "png")):
            status, _, headers = client.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(kind, headers["Content-Type"])
        self.assertEqual(client.get("/icons/../server.py")[0], 404)
        status, health, _ = client.get("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("markets", health)

    def test_full_trading_flow(self):
        client = Client(self.base)
        status, me, _ = client.get("/api/me")
        self.assertIsNone(me["user"])
        self.assertTrue(me["auth"]["dev"])

        status, body, _ = client.post("/auth/dev", {"login": "carol"}, csrf=False)
        self.assertEqual(status, 403, "POST without X-EarlyStar must be rejected")
        client.login("carol")
        status, me, _ = client.get("/api/me")
        self.assertEqual(me["user"]["login"], "carol")
        self.assertEqual(me["user"]["balance"], markets.STARTING_BALANCE)
        self.assertIsNone(me["user"]["trade_block_reason"])

        status, suggestion, _ = client.get("/api/markets/suggest?repo=acme/rocket&days=30")
        self.assertEqual(status, 200, suggestion)
        self.assertAlmostEqual(suggestion["repo"]["rate_per_day"], 50, delta=1)

        status, created, _ = client.post("/api/markets", {"kind": "stars_reach", "repo": "acme/rocket", "target": 1600, "days": 21})
        self.assertEqual(status, 201, created)
        market_id = created["market"]["id"]
        self.assertTrue(created["market"]["rules"])
        status, me, _ = client.get("/api/me")
        self.assertEqual(me["user"]["balance"], markets.STARTING_BALANCE - markets.CREATE_FEE)

        status, listing, _ = client.get("/api/markets?status=open&sort=new")
        self.assertIn(market_id, [item["id"] for item in listing["items"]])

        status, quote, _ = client.get(f"/api/markets/{market_id}/quote?side=yes&action=buy&amount=100")
        self.assertEqual(status, 200, quote)
        status, trade, _ = client.post(f"/api/markets/{market_id}/trade",
                                       {"side": "yes", "action": "buy", "amount": 100, "min_shares": quote["shares"]})
        self.assertEqual(status, 200, trade)
        self.assertAlmostEqual(trade["shares"], quote["shares"], places=6)
        self.assertAlmostEqual(trade["balance"], 850, places=4)

        status, detail, _ = client.get(f"/api/markets/{market_id}")
        self.assertAlmostEqual(detail["my_position"]["yes_shares"], trade["shares"], places=4)
        self.assertEqual(detail["trades"][0]["login"], "carol")
        self.assertEqual(len(detail["history"]), 3)

        status, folio, _ = client.get("/api/portfolio")
        self.assertEqual(len(folio["positions"]), 1)
        status, sold, _ = client.post(f"/api/markets/{market_id}/trade",
                                      {"side": "yes", "action": "sell", "amount": trade["shares"]})
        self.assertEqual(status, 200, sold)
        self.assertAlmostEqual(sold["balance"], 950, places=3)

        status, board, _ = client.get("/api/leaderboard?period=all")
        self.assertIn("carol", [row["login"] for row in board["items"]])
        status, odds, _ = client.get("/api/odds")
        self.assertIn("acme/rocket", odds["items"])
        status, prof, _ = client.get("/api/users/carol")
        self.assertEqual(prof["stats"]["trades"], 2)

        status, body, _ = client.post("/auth/logout")
        self.assertEqual(status, 200)
        status, me, _ = client.get("/api/me")
        self.assertIsNone(me["user"])

    def test_csrf_origin_and_bad_inputs(self):
        client = Client(self.base)
        client.login("dave")
        status, body, _ = client.post("/auth/logout", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        checks = [
            ("GET", "/api/markets?status=bogus", None, 400),
            ("GET", "/api/markets?limit=abc", None, 400),
            ("GET", "/api/markets/999999", None, 404),
            ("GET", "/api/users/nobody-here", None, 404),
            ("POST", "/api/markets/999999/trade", {"side": "yes", "action": "buy", "amount": 5}, 404),
            ("POST", "/api/markets", {"kind": "stars_reach", "repo": "../etc/passwd", "target": 5, "days": 5}, 400),
            ("GET", "/api/admin/review", None, 403),
        ]
        for method, path, body, expected in checks:
            with self.subTest(path=path):
                status, payload, _ = client.call(method, path, body)
                self.assertEqual(status, expected, payload)
        status, payload, _ = client.post("/api/markets", raw_body=b"{not json")
        self.assertEqual(status, 400)
        status, payload, _ = client.post("/api/markets", raw_body=b"[1, 2]")
        self.assertEqual(status, 400)
        anonymous = Client(self.base)
        status, payload, _ = anonymous.post("/api/markets/1/trade", {"side": "yes", "action": "buy", "amount": 5})
        self.assertEqual(status, 401)
        status, payload, _ = anonymous.get("/api/portfolio")
        self.assertEqual(status, 401)

    def test_admin_void_refunds(self):
        trader = Client(self.base)
        trader.login("erin")
        boss = Client(self.base)
        boss.login("boss")
        conn = markets.connect_app()
        try:
            repo = markets.repo_state(conn, repo_id=102)
            with markets.transaction(conn):
                market_id = markets._insert_market(
                    conn, kind="stars_reach", repo=repo, repo_b=None, target=5000, window_start=None, window_days=None,
                    open_ts=time.time() - 10, close_ts=time.time() + 5 * DAY, created_by=None, liquidity=200, initial_prob=0.4,
                )
        finally:
            conn.close()
        status, trade, _ = trader.post(f"/api/markets/{market_id}/trade", {"side": "no", "action": "buy", "amount": 120})
        self.assertEqual(status, 200, trade)
        status, payload, _ = trader.post(f"/api/admin/markets/{market_id}/resolve", {"outcome": "void"})
        self.assertEqual(status, 403)
        status, payload, _ = boss.get("/api/admin/review")
        self.assertEqual(status, 200)
        status, payload, _ = boss.post(f"/api/admin/markets/{market_id}/resolve", {"outcome": "void", "note": "test"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["market"]["status"], "void")
        status, me, _ = trader.get("/api/me")
        self.assertAlmostEqual(me["user"]["balance"], markets.STARTING_BALANCE, places=4)
        conn = markets.connect_app()
        try:
            self.assertTrue(markets.ledger_consistent(conn))
        finally:
            conn.close()


class PublicModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = start(public=True)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_single_user_endpoints_are_locked(self):
        anonymous = Client(self.base)
        self.assertEqual(anonymous.post("/api/token", {"token": "x"})[0], 401)
        self.assertEqual(anonymous.post("/api/collect")[0], 401)
        self.assertEqual(anonymous.get("/api/stargains/acme/rocket")[0], 401)
        status, payload, _ = anonymous.post("/api/watch", {"full_name": "acme/rocket"})
        self.assertEqual((status, payload.get("ignored")), (200, True))
        user = Client(self.base)
        user.login("frank")
        self.assertEqual(user.post("/api/token", {"token": "x"})[0], 403)
        self.assertEqual(user.post("/api/collect")[0], 403)
        boss = Client(self.base)
        boss.login("boss")
        self.assertEqual(boss.post("/api/collect")[0], 200)


if __name__ == "__main__":
    unittest.main()
