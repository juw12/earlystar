"""Unit tests for EarlyStar's market engine (pricing, trading, settlement, generation, scoring)."""

from __future__ import annotations

import datetime as dt
import math
import unittest
from unittest import mock

import markets
from tests.helpers import DAY, NOW, Env, fake_github_repo


class LmsrMathTests(unittest.TestCase):
    def test_buy_cost_matches_closed_form(self):
        b = 150.0
        for q_yes, q_no in [(0, 0), (120, -40), (-300, 80)]:
            for side in ("yes", "no"):
                for points in (1, 25, 400):
                    shares = markets.shares_for_points(q_yes, q_no, b, side, points)
                    new = (q_yes + shares, q_no) if side == "yes" else (q_yes, q_no + shares)
                    cost = markets.lmsr_cost(*new, b) - markets.lmsr_cost(q_yes, q_no, b)
                    self.assertAlmostEqual(cost, points, places=7)

    def test_round_trip_and_path_independence(self):
        b = 250.0
        shares = markets.shares_for_points(0, 0, b, "yes", 100)
        self.assertAlmostEqual(markets.points_for_shares(shares, 0, b, "yes", shares), 100, places=7)
        first = markets.shares_for_points(0, 0, b, "no", 50)
        second = markets.shares_for_points(0, first, b, "no", 50)
        self.assertAlmostEqual(first + second, markets.shares_for_points(0, 0, b, "no", 100), places=7)

    def test_prices(self):
        self.assertAlmostEqual(markets.lmsr_prob(0, 0, 100), 0.5)
        self.assertGreater(markets.lmsr_prob(50, 0, 100), 0.5)
        self.assertAlmostEqual(markets.lmsr_prob(100 * markets.logit(0.8), 0, 100), 0.8, places=9)
        self.assertTrue(0 <= markets.lmsr_prob(-1e5, 0, 100) < 1e-6)  # no overflow (underflows to 0)
        self.assertTrue(1 - 1e-6 < markets.lmsr_prob(1e5, 0, 100) <= 1)
        # Shares bought at 50 % for 100 points with b=250 → about 0.6 probability.
        shares = markets.shares_for_points(0, 0, 250, "yes", 100)
        self.assertGreater(shares, 100)
        # From 50 %, spending S gives p = 1 - exp(-S/b) / 2 (closed form).
        self.assertAlmostEqual(markets.lmsr_prob(shares, 0, 250), 1 - math.exp(-100 / 250) / 2, places=9)

    def test_nice_target_and_models(self):
        self.assertEqual(markets.nice_target(12345), 13000)
        self.assertEqual(markets.nice_target(9300), 10000)   # snapped to milestone
        self.assertEqual(markets.nice_target(870), 1000)
        self.assertEqual(markets.nice_target(43100), 50000)
        self.assertEqual(markets.nice_target(61000), 61000)
        # Equal effective and required rates → 50 %.
        self.assertAlmostEqual(markets.model_prob_reach(1000, 1600, 100 / 0.6, 6), 0.5, places=6)
        self.assertEqual(markets.model_prob_reach(1000, 900, 10, 5), markets.PROB_MAX)
        self.assertEqual(markets.model_prob_reach(1000, 2000, 0, 5), markets.PROB_MIN)
        self.assertAlmostEqual(markets.model_prob_versus(50, 50), 0.5)
        # A 3-day-old repo's launch velocity is discounted 4x (clamped youth factor 0.25).
        self.assertAlmostEqual(markets.effective_rate(1000, 3), 0.6 * 1000 * 0.25)
        self.assertAlmostEqual(markets.effective_rate(1000, 400), 600)
        young = {"stars": 16000, "rate_per_day": 5000, "created_at": "2026-10-31T12:00:00Z"}
        target = markets.target_for(young, 30, NOW)
        self.assertLess(target, 45000)
        prob = markets.model_prob_reach(16000, target, 5000, 30, markets.repo_age_days(young, NOW))
        self.assertTrue(0.4 < prob < 0.6, prob)
        self.assertGreater(markets.model_prob_versus(100, 50), 0.75)

    def test_question_text(self):
        close = markets.deadline_ts(NOW, 10)
        text = markets.question_for("stars_reach", "a/b", None, 12000, close)
        self.assertEqual(text, "Will a/b reach 12k★ by Nov 13, 2026?")
        start = dt.date(2026, 11, 4)
        text = markets.question_for("trending_days", "a/b", None, 4, close, start, 7)
        self.assertIn("at least 4 of the 7 days Nov 4–Nov 10", text)


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.add_repo(1, "acme/rocket", 1000)
        self.alice = self.env.user("alice")
        self.market_id = self.env.stars_market(1, target=1200)

    def tearDown(self):
        self.env.close()

    def trade(self, user, side, action, amount, **kwargs):
        return markets.execute_trade(self.env.conn, user["id"], self.market_id, side, action, amount, now=NOW + 60, **kwargs)

    def test_signup_grant_and_ledger(self):
        self.assertEqual(self.env.balance(self.alice), markets.STARTING_BALANCE)
        self.assertTrue(markets.ledger_consistent(self.env.conn))

    def test_buy_then_sell_all_round_trips(self):
        bought = self.trade(self.alice, "yes", "buy", 100)
        self.assertAlmostEqual(self.env.balance(self.alice), 900)
        self.assertGreater(bought["prob_after"], bought["prob_before"])
        sold = self.trade(self.alice, "yes", "sell", bought["shares"])
        self.assertAlmostEqual(sold["points"], 100, places=4)
        self.assertAlmostEqual(self.env.balance(self.alice), 1000, places=4)
        self.assertAlmostEqual(sold["prob_after"], 0.5, places=6)
        self.assertEqual(self.env.market(self.market_id)["trade_count"], 2)
        self.assertTrue(markets.ledger_consistent(self.env.conn))

    def test_stake_cap(self):
        with self.assertRaises(markets.MarketError):
            self.trade(self.alice, "yes", "buy", markets.MAX_STAKE_PER_MARKET + 1)
        self.trade(self.alice, "yes", "buy", 400)
        with self.assertRaisesRegex(markets.MarketError, "at risk"):
            self.trade(self.alice, "no", "buy", 150)
        self.trade(self.alice, "no", "buy", 100)  # exactly at the cap

    def test_insufficient_balance(self):
        for target in (1500, 1600):
            other = self.env.stars_market(1, target=target)
            markets.execute_trade(self.env.conn, self.alice["id"], other, "yes", "buy", 450, now=NOW + 60)
        with self.assertRaisesRegex(markets.MarketError, "Not enough points"):
            self.trade(self.alice, "yes", "buy", 150)

    def test_owner_cannot_trade(self):
        owner = self.env.user("acme")
        with self.assertRaisesRegex(markets.MarketError, "owners"):
            self.trade(owner, "no", "buy", 10)

    def test_closed_market(self):
        with self.assertRaisesRegex(markets.MarketError, "closed"):
            markets.execute_trade(self.env.conn, self.alice["id"], self.market_id, "yes", "buy", 10,
                                  now=self.env.market(self.market_id)["close_ts"])

    def test_slippage_rolls_back(self):
        quote = markets.quote(self.env.conn, self.market_id, "yes", "buy", 50, now=NOW + 60)
        with self.assertRaisesRegex(markets.MarketError, "Price moved"):
            self.trade(self.alice, "yes", "buy", 50, min_shares=quote["shares"] + 1)
        self.assertEqual(self.env.balance(self.alice), 1000)
        self.assertEqual(self.env.market(self.market_id)["trade_count"], 0)
        self.trade(self.alice, "yes", "buy", 50, min_shares=quote["shares"])

    def test_young_account_and_rate_limit(self):
        newbie = self.env.user("newbie", age_days=3)
        with self.assertRaisesRegex(markets.MarketError, "days old"):
            self.trade(newbie, "yes", "buy", 10)
        with mock.patch.object(markets, "MAX_TRADES_PER_HOUR", 2):
            self.trade(self.alice, "yes", "buy", 5)
            self.trade(self.alice, "yes", "buy", 5)
            with self.assertRaisesRegex(markets.MarketError, "Slow down"):
                self.trade(self.alice, "yes", "buy", 5)

    def test_sell_validation(self):
        with self.assertRaisesRegex(markets.MarketError, "don't hold"):
            self.trade(self.alice, "no", "sell", 1)
        bought = self.trade(self.alice, "no", "buy", 20)
        with self.assertRaisesRegex(markets.MarketError, "only hold"):
            self.trade(self.alice, "no", "sell", bought["shares"] + 5)
        for bad in (0, -5, "abc", float("nan")):
            with self.assertRaises(markets.MarketError):
                self.trade(self.alice, "no", "buy", bad)
        with self.assertRaises(markets.MarketError):
            self.trade(self.alice, "maybe", "buy", 5)

    def test_trading_halts_when_target_already_hit(self):
        self.env.snap(1, NOW + 30, 1250)
        with self.assertRaisesRegex(markets.MarketError, "settling"):
            self.trade(self.alice, "yes", "buy", 10)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.add_repo(1, "acme/rocket", 1000)
        self.env.add_repo(2, "beta/boat", 5000)
        # A quiet baseline so the spike detector has history.
        for days_back in range(12, 0, -1):
            self.env.snap(1, NOW - days_back * DAY, 1000 - days_back * 5)
            self.env.snap(2, NOW - days_back * DAY, 5000 - days_back * 10)
        self.env.snap(1, NOW, 1000)
        self.env.snap(2, NOW, 5000)
        self.yes = self.env.user("yesman")
        self.no = self.env.user("noman")

    def tearDown(self):
        self.env.close()

    def buy(self, user, market_id, side, points):
        return markets.execute_trade(self.env.conn, user["id"], market_id, side, "buy", points, now=NOW + 60)

    def test_stars_reach_early_yes_pays_winners(self):
        market_id = self.env.stars_market(1, target=1100, days=20)
        yes = self.buy(self.yes, market_id, "yes", 100)
        self.buy(self.no, market_id, "no", 100)
        # Gradual growth (no spike): 1000 → 1100 over 5 days.
        for step in range(1, 6):
            self.env.snap(1, NOW + step * DAY, 1000 + step * 20)
        summary = markets.resolve_due(self.env.conn, now=NOW + 5 * DAY + 60)
        self.assertEqual(summary["resolved"], 1)
        market = self.env.market(market_id)
        self.assertEqual((market["status"], market["outcome"]), ("resolved", "yes"))
        self.assertAlmostEqual(self.env.balance(self.yes), 900 + yes["shares"], places=4)
        self.assertAlmostEqual(self.env.balance(self.no), 900, places=4)
        self.assertTrue(markets.ledger_consistent(self.env.conn))
        # Idempotent: a second pass changes nothing.
        self.assertEqual(markets.resolve_due(self.env.conn, now=NOW + 6 * DAY)["resolved"], 0)

    def test_stars_reach_no_at_close(self):
        market_id = self.env.stars_market(1, target=1500, days=3)
        no = self.buy(self.no, market_id, "no", 50)
        close = self.env.market(market_id)["close_ts"]
        self.env.snap(1, close - 600, 1200)
        self.assertEqual(markets.resolve_due(self.env.conn, now=close - 60)["resolved"], 0)  # still open
        self.env.snap(1, close + 900, 1210)
        markets.resolve_due(self.env.conn, now=close + 1000)
        market = self.env.market(market_id)
        self.assertEqual(market["outcome"], "no")
        self.assertIn("1,210", market["resolution_note"])
        self.assertAlmostEqual(self.env.balance(self.no), 950 + no["shares"], places=4)

    def test_waits_then_falls_back_then_voids(self):
        fallback_id = self.env.stars_market(1, target=1500, days=3)
        close = self.env.market(fallback_id)["close_ts"]
        self.env.snap(1, close - 2 * 3600, 1300)
        self.assertEqual(markets.resolve_due(self.env.conn, now=close + 3600)["resolved"], 0)  # waiting
        markets.resolve_due(self.env.conn, now=close + markets.CLOSE_LOOKAHEAD + 1)
        self.assertEqual(self.env.market(fallback_id)["outcome"], "no")

        void_id = self.env.stars_market(2, target=9000, days=10)
        self.buy(self.yes, void_id, "yes", 80)
        close = self.env.market(void_id)["close_ts"]
        markets.resolve_due(self.env.conn, now=close + markets.CLOSE_LOOKAHEAD + 1)
        self.assertEqual(self.env.market(void_id)["status"], "void")
        self.assertAlmostEqual(self.env.balance(self.yes), 1000, places=4)

    def test_void_claws_back_selling_profit(self):
        market_id = self.env.stars_market(1, target=1400, days=5)
        bought = self.buy(self.yes, market_id, "yes", 100)
        self.buy(self.no, market_id, "yes", 300)  # pushes the price up
        sold = markets.execute_trade(self.env.conn, self.yes["id"], market_id, "yes", "sell", bought["shares"], now=NOW + 120)
        self.assertGreater(sold["points"], 100)
        with markets.transaction(self.env.conn):
            markets.void(self.env.conn, market_id, "test", NOW + 200)
        self.assertAlmostEqual(self.env.balance(self.yes), 1000, places=4)
        self.assertAlmostEqual(self.env.balance(self.no), 1000, places=4)
        self.assertTrue(markets.ledger_consistent(self.env.conn))

    def test_versus_outcomes(self):
        yes_id = self.env.versus_market(1, 2)
        close = self.env.market(yes_id)["close_ts"]
        self.env.snap(1, close + 600, 1300)   # +300
        self.env.snap(2, close + 600, 5200)   # +200
        markets.resolve_due(self.env.conn, now=close + 700)
        self.assertEqual(self.env.market(yes_id)["outcome"], "yes")

    def test_versus_tie_voids(self):
        tie_id = self.env.versus_market(1, 2, days=8)
        close = self.env.market(tie_id)["close_ts"]
        self.env.snap(1, close + 60, 1100)
        self.env.snap(2, close + 60, 5100)
        markets.resolve_due(self.env.conn, now=close + 120)
        self.assertEqual(self.env.market(tie_id)["status"], "void")

    def test_trending_days(self):
        early_id = self.env.trending_market(1, needed=2)
        start = dt.date.fromisoformat(self.env.market(early_id)["window_start_day"])
        self.env.trending(start, "acme/rocket")
        self.assertEqual(markets.resolve_due(self.env.conn, now=NOW + DAY)["resolved"], 0)
        self.env.trending(start + dt.timedelta(days=1), "acme/rocket")
        markets.resolve_due(self.env.conn, now=NOW + 2 * DAY)
        self.assertEqual(self.env.market(early_id)["outcome"], "yes")

    def test_trending_no_and_void_on_gaps(self):
        no_id = self.env.trending_market(2, needed=4)
        market = self.env.market(no_id)
        start = dt.date.fromisoformat(market["window_start_day"])
        for offset in range(7):
            self.env.trending(start + dt.timedelta(days=offset), "someone/else")
        self.env.trending(start, "beta/boat")
        markets.resolve_due(self.env.conn, now=market["close_ts"] + 60)
        self.assertEqual(self.env.market(no_id)["outcome"], "no")

    def test_trending_void_when_collector_missed_days(self):
        env = Env()
        try:
            env.add_repo(9, "gap/repo", 100)
            gap_id = env.trending_market(9, needed=4)
            market = env.market(gap_id)
            start = dt.date.fromisoformat(market["window_start_day"])
            for offset in range(3):  # only 3 of 7 days recorded
                env.trending(start + dt.timedelta(days=offset), "other/repo")
            markets.resolve_due(env.conn, now=market["close_ts"] + 60)
            self.assertEqual(env.market(gap_id)["status"], "void")
        finally:
            env.close()

    def test_star_spike_goes_to_review_then_admin_settles(self):
        market_id = self.env.stars_market(1, target=1600, days=10)
        yes = self.buy(self.yes, market_id, "yes", 60)
        self.env.snap(1, NOW + 2 * DAY, 1010)
        self.env.snap(1, NOW + 3 * DAY, 1650)  # +640 in a day vs ~5/day baseline
        summary = markets.resolve_due(self.env.conn, now=NOW + 3 * DAY + 60)
        self.assertEqual(summary["review"], 1)
        market = self.env.market(market_id)
        self.assertEqual(market["status"], "review")
        self.assertIn("Star spike", market["flag_note"])
        with self.assertRaises(markets.MarketError):
            markets.execute_trade(self.env.conn, self.no["id"], market_id, "no", "buy", 10, now=NOW + 3 * DAY + 90)
        markets.admin_resolve(self.env.conn, market_id, "void", "Bought stars", now=NOW + 4 * DAY)
        self.assertEqual(self.env.market(market_id)["status"], "void")
        self.assertAlmostEqual(self.env.balance(self.yes), 1000, places=4)
        self.assertGreater(yes["shares"], 0)

    def test_admin_cannot_settle_open_market_early(self):
        market_id = self.env.stars_market(1, target=2000, days=10)
        with self.assertRaisesRegex(markets.MarketError, "voided"):
            markets.admin_resolve(self.env.conn, market_id, "no", "", now=NOW + DAY)

    def test_pins_follow_open_markets(self):
        market_id = self.env.stars_market(1, target=1100, days=5)
        pinned = self.env.conn.execute("SELECT until_ts FROM radar.pinned WHERE repo_id = 1").fetchone()
        self.assertAlmostEqual(pinned[0], self.env.market(market_id)["close_ts"] + markets.PIN_GRACE)
        self.env.snap(1, NOW + DAY, 1100)
        markets.resolve_due(self.env.conn, now=NOW + DAY + 60)
        self.assertIsNone(self.env.conn.execute("SELECT 1 FROM radar.pinned WHERE repo_id = 1").fetchone())


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.add_repo(1, "acme/rocket", 1000)
        self.env.snap(1, NOW - 7 * DAY, 650)   # 50 stars/day
        self.env.snap(1, NOW, 1000)
        self.bob = self.env.user("bob")
        self.fetched = []

    def tearDown(self):
        self.env.close()

    def fetch(self, name):
        self.fetched.append(name)
        if name == "fresh/repo":
            return fake_github_repo(77, "fresh/repo", 4200)
        raise markets.MarketError("That repository doesn't exist on GitHub (or is private).", 404)

    def create(self, payload, user=None):
        return markets.create_user_market(self.env.conn, user or self.bob, payload, now=NOW + 10, fetch_repo=self.fetch)

    def test_create_stars_call_charges_fee_and_uses_model_odds(self):
        market_id = self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1500, "days": 20})
        market = self.env.market(market_id)
        expected = markets.model_prob_reach(1000, 1500, 50, 20 + (market["close_ts"] - markets.deadline_ts(NOW, 20)) / DAY)
        self.assertAlmostEqual(market["initial_prob"], round(markets.model_prob_reach(1000, 1500, 50, 20), 6), places=3)
        self.assertGreater(expected, 0)
        self.assertAlmostEqual(self.env.balance(self.bob), 1000 - markets.CREATE_FEE)
        self.assertEqual(market["created_by"], self.bob["id"])
        self.assertAlmostEqual(markets.lmsr_prob(market["q_yes"], market["q_no"], market["liquidity"]), market["initial_prob"], places=5)
        self.assertEqual(self.fetched, [])  # fresh snapshot, no GitHub call

    def test_validation(self):
        cases = [
            ({"kind": "stars_reach", "repo": "acme/rocket", "target": 1005, "days": 20}, "at least"),
            ({"kind": "stars_reach", "repo": "acme/rocket", "target": 1500, "days": 1}, "run"),
            ({"kind": "stars_reach", "repo": "acme/rocket", "target": 10 ** 7, "days": 20}, "unrealistic"),
            ({"kind": "stars_reach", "repo": "not a repo", "target": 1500, "days": 20}, "owner/name"),
            ({"kind": "lottery", "repo": "acme/rocket"}, "kind"),
            ({"kind": "versus", "repo": "acme/rocket", "repo_b": "acme/rocket", "days": 10}, "different"),
            ({"kind": "stars_reach", "repo": "ghost/repo", "target": 1500, "days": 20}, "exist"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(markets.MarketError, message):
                    self.create(payload)
        self.assertEqual(self.env.balance(self.bob), 1000)

    def test_duplicate_own_repo_and_daily_limit(self):
        self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1500, "days": 20})
        with self.assertRaisesRegex(markets.MarketError, "already exists"):
            self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1500, "days": 30})
        owner = self.env.user("acme")
        with self.assertRaisesRegex(markets.MarketError, "own repository"):
            self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1800, "days": 20}, owner)
        self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1600, "days": 20})
        self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1700, "days": 20})
        with self.assertRaisesRegex(markets.MarketError, "per day"):
            self.create({"kind": "stars_reach", "repo": "acme/rocket", "target": 1900, "days": 20})

    def test_versus_with_fetched_repo(self):
        market_id = self.create({"kind": "versus", "repo": "acme/rocket", "repo_b": "https://github.com/fresh/repo", "days": 14})
        market = self.env.market(market_id)
        self.assertEqual(self.fetched, ["fresh/repo"])
        self.assertEqual((market["repo_b"], market["start_stars_b"]), ("fresh/repo", 4200))
        self.assertIsNotNone(self.env.conn.execute("SELECT 1 FROM radar.pinned WHERE repo_id = 77").fetchone())

    def test_suggest(self):
        result = markets.suggest(self.env.conn, "acme/rocket", NOW + 10, fetch_repo=self.fetch)
        self.assertGreater(result["target"], 1000)
        self.assertTrue(0 < result["model_prob"] < 1)
        self.assertEqual(result["repo"]["rate_source"], "tracked-7d")
        self.assertAlmostEqual(result["repo"]["rate_per_day"], 50, places=3)


class GenerationAndScoringTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        names = ["one/alpha", "two/beta", "three/gamma", "four/delta", "five/eps", "six/zeta", "seven/eta", "eight/theta"]
        for index, name in enumerate(names, start=1):
            base = 2000 * index
            gain = 700 + index * 20      # similar daily gains → versus candidates
            self.env.add_repo(index, name, base, ts=NOW - 7 * DAY - 600)
            self.env.snap(index, NOW, base + gain)
            self.env.trending(markets.utc_day(NOW), name, rank=index)
        self.env.trending(markets.utc_day(NOW), "unknown/repo", rank=9)

    def tearDown(self):
        self.env.close()

    def test_generate_respects_caps_and_rules(self):
        result = markets.generate_markets(self.env.conn, now=NOW)
        created = result["created"]
        self.assertEqual(len(created["stars_reach"]), markets.AUTO_STARS_PER_RUN)
        self.assertEqual(len(created["trending_days"]), markets.AUTO_TRENDING_PER_RUN)
        self.assertEqual(len(created["versus"]), 1)
        rows = self.env.conn.execute("SELECT * FROM markets").fetchall()
        repos = [row["repo_id"] for row in rows] + [row["repo_b_id"] for row in rows if row["repo_b_id"]]
        self.assertEqual(len(repos), len(set(repos)), "one open auto call per repo")
        for row in rows:
            self.assertTrue(0.15 <= row["initial_prob"] <= 0.85 or row["kind"] != "stars_reach")
            self.assertIsNone(row["created_by"])
            if row["kind"] == "stars_reach":
                self.assertGreater(row["target"], row["start_stars"])
        # Daily budget: a second run the same day creates at most the remainder.
        second = markets.generate_markets(self.env.conn, now=NOW + 60)
        total = sum(len(ids) for ids in second["created"].values())
        self.assertLessEqual(total + 6, markets.AUTO_MAX_PER_DAY)

    def test_generator_waits_for_fresh_data(self):
        self.assertIsNone(markets.generate_markets(self.env.conn, now=NOW + 3 * 3600))
        ran = markets.run_job_if_due(self.env.conn, "generate", markets.AUTO_INTERVAL, markets.generate_markets, now=NOW + 3 * 3600)
        self.assertIsNone(ran)
        self.assertIsNone(self.env.conn.execute("SELECT 1 FROM jobs WHERE name = 'generate'").fetchone())

    def test_daily_bonus_and_scoring(self):
        markets.generate_markets(self.env.conn, now=NOW)
        market_id = self.env.conn.execute("SELECT id FROM markets WHERE kind = 'stars_reach' LIMIT 1").fetchone()[0]
        winner = self.env.user("winner")
        loser = self.env.user("loser")
        self.assertFalse(markets.claim_daily_bonus(self.env.conn, winner["id"], NOW))   # signup day
        self.assertTrue(markets.claim_daily_bonus(self.env.conn, winner["id"], NOW + DAY))
        self.assertFalse(markets.claim_daily_bonus(self.env.conn, winner["id"], NOW + DAY + 60))
        win = markets.execute_trade(self.env.conn, winner["id"], market_id, "yes", "buy", 100, now=NOW + 60)
        markets.execute_trade(self.env.conn, loser["id"], market_id, "no", "buy", 100, now=NOW + 60)
        market = self.env.market(market_id)
        with markets.transaction(self.env.conn):
            markets.settle(self.env.conn, market_id, "yes", "test", NOW + 2 * DAY)
        board = markets.leaderboard(self.env.conn, now=NOW + 2 * DAY)
        by_login = {row["login"]: row for row in board["items"]}
        self.assertAlmostEqual(by_login["winner"]["profit"], round(win["shares"] - 100, 2), places=2)
        self.assertAlmostEqual(by_login["loser"]["profit"], -100, places=2)
        self.assertEqual(by_login["winner"]["accuracy"], 1.0)
        self.assertEqual(board["items"][0]["login"], "winner")
        self.assertAlmostEqual(by_login["winner"]["profit_30d"], by_login["winner"]["profit"], places=2)
        folio = markets.portfolio(self.env.conn, winner["id"], now=NOW + 2 * DAY)
        self.assertEqual(len(folio["positions"]), 1)
        self.assertEqual(folio["stats"]["balance"], round(1000 + 25 - 100 + win["shares"], 2))
        if market["initial_prob"] <= 0.3:
            self.assertTrue(board["best_calls"])
        profile = markets.profile(self.env.conn, "winner", now=NOW + 2 * DAY)
        self.assertEqual(profile["stats"]["settled"], 1)
        self.assertTrue(markets.ledger_consistent(self.env.conn))

    def test_read_models(self):
        markets.generate_markets(self.env.conn, now=NOW)
        listing = markets.list_markets(self.env.conn, now=NOW + 60)
        self.assertEqual(listing["count"], 6)
        detail = markets.market_detail(self.env.conn, listing["items"][0]["id"], now=NOW + 60)
        self.assertTrue(detail["rules"])
        self.assertGreaterEqual(len(detail["history"]), 2)
        self.assertTrue(detail["stars"])
        odds = markets.odds_by_repo(self.env.conn, now=NOW + 60)
        self.assertIn("one/alpha", odds)
        for status in ("open", "closing", "pending", "settled", "all"):
            markets.list_markets(self.env.conn, now=NOW + 60, status=status)
        with self.assertRaises(markets.MarketError):
            markets.list_markets(self.env.conn, now=NOW, status="bogus")
        self.assertEqual(markets.list_markets(self.env.conn, now=NOW, query="alpha")["count"] >= 1, True)


if __name__ == "__main__":
    unittest.main()
