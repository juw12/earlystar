"""EarlyStar markets: play-money prediction markets on GitHub repositories.

Markets settle themselves from the radar's own data (star snapshots and the
GitHub Trending log collected by collector.py), so there is no human oracle.

Points are play money: they cannot be bought, sold, transferred or withdrawn.

Standard library only. All money-like values are points stored as REAL and
rounded to 6 decimals on every write; `users.balance == SUM(ledger.delta)` is
an invariant covered by the tests.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from collector import DATA_DIR, DB_PATH as RADAR_DB_PATH, connect as radar_connect, iso_to_ts, upsert_repo

APP_DB_PATH = DATA_DIR / "earlystar.db"

# --------------------------------------------------------------------------- settings

# Economy (play money; points have no cash value)
STARTING_BALANCE = 1000.0
DAILY_BONUS = 25.0
CREATE_FEE = 50.0
MAX_STAKE_PER_MARKET = 500.0        # net points at risk per user per market
MIN_TRADE_POINTS = 1.0
MAX_TRADES_PER_HOUR = 120
MAX_USER_MARKETS_PER_DAY = 3
MIN_ACCOUNT_AGE_DAYS = 14
LIQUIDITY_AUTO = 250.0
LIQUIDITY_USER = 150.0
GRANT_KINDS = ("signup", "daily_bonus", "admin_grant")
PROB_MIN, PROB_MAX = 0.03, 0.97     # clamp for model opening odds

# Resolution
CLOSE_LOOKAHEAD = 6 * 3600          # first snapshot within 6 h after the deadline settles the market
CLOSE_LOOKBACK = 3 * 3600           # fallback: last snapshot within 3 h before the deadline
TRENDING_COVERAGE_MIN = 0.8         # share of window days the collector must have recorded
SPIKE_WINDOW = 48 * 3600
SPIKE_BASELINE_DAYS = 7
SPIKE_MIN_STARS = 200
SPIKE_FACTOR = 5.0
PIN_GRACE = 2 * 86400               # keep refreshing a repo this long after its last market closes
REPO_FRESH_SECONDS = 6 * 3600       # user-created markets need a snapshot at most this old

# Auto-generation
AUTO_INTERVAL = 6 * 3600
AUTO_MAX_PER_DAY = 8
AUTO_MAX_OPEN = 80
AUTO_STARS_PER_RUN = 3
AUTO_TRENDING_PER_RUN = 2
AUTO_VERSUS_PER_RUN = 1
AUTO_STARS_DAYS = 30
AUTO_TRENDING_DAYS = 7
AUTO_TRENDING_NEEDED = 4
AUTO_VERSUS_DAYS = 7
AUTO_MAX_DATA_AGE = 2 * 3600       # only generate from a collection at most this old

# User-created market bounds
USER_STARS_DAYS = (3, 180)
USER_VERSUS_DAYS = (7, 90)

DAY = 86400
KINDS = ("stars_reach", "versus", "trending_days")
SIDES = ("yes", "no")
ACTIONS = ("buy", "sell")
REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
MILESTONES = [m * 10 ** e for e in range(2, 8) for m in (1, 2, 2.5, 5)]
EPS = 1e-9

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY,
    github_id         INTEGER UNIQUE,
    login             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name              TEXT,
    avatar_url        TEXT,
    github_created_at TEXT,
    is_dev            INTEGER NOT NULL DEFAULT 0,
    balance           REAL NOT NULL DEFAULT 0,
    created_ts        REAL NOT NULL,
    last_bonus_day    TEXT,
    is_banned         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS markets (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('stars_reach', 'versus', 'trending_days')),
    repo             TEXT NOT NULL COLLATE NOCASE,
    repo_id          INTEGER NOT NULL,
    repo_b           TEXT COLLATE NOCASE,
    repo_b_id        INTEGER,
    target           INTEGER,
    window_start_day TEXT,
    window_days      INTEGER,
    question         TEXT NOT NULL,
    open_ts          REAL NOT NULL,
    close_ts         REAL NOT NULL,
    created_by       INTEGER REFERENCES users(id),
    liquidity        REAL NOT NULL,
    q_yes            REAL NOT NULL DEFAULT 0,
    q_no             REAL NOT NULL DEFAULT 0,
    initial_prob     REAL NOT NULL,
    start_stars      INTEGER,
    start_stars_b    INTEGER,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'review', 'resolved', 'void')),
    outcome          TEXT CHECK (outcome IN ('yes', 'no')),
    resolved_ts      REAL,
    resolution_note  TEXT,
    flag_note        TEXT,
    volume           REAL NOT NULL DEFAULT 0,
    trade_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status, close_ts);
CREATE INDEX IF NOT EXISTS idx_markets_repo ON markets(repo_id);
CREATE INDEX IF NOT EXISTS idx_markets_repo_b ON markets(repo_b_id);

CREATE TABLE IF NOT EXISTS positions (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    yes_shares REAL NOT NULL DEFAULT 0,
    no_shares  REAL NOT NULL DEFAULT 0,
    spent      REAL NOT NULL DEFAULT 0,
    received   REAL NOT NULL DEFAULT 0,
    payout     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, market_id)
);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY,
    market_id   INTEGER NOT NULL REFERENCES markets(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    ts          REAL NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    action      TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
    shares      REAL NOT NULL,
    points      REAL NOT NULL,
    prob_before REAL NOT NULL,
    prob_after  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, ts);

CREATE TABLE IF NOT EXISTS ledger (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id),
    ts        REAL NOT NULL,
    delta     REAL NOT NULL,
    kind      TEXT NOT NULL,
    market_id INTEGER,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, ts);

CREATE TABLE IF NOT EXISTS jobs (
    name    TEXT PRIMARY KEY,
    last_ts REAL,
    detail  TEXT
);
"""


class MarketError(Exception):
    """A rule violation that should be shown to the user (HTTP 4xx)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- storage

def connect_app(app_path: Optional[Path] = None, radar_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the private app DB with the public radar DB attached as `radar`."""
    app_path = Path(app_path or APP_DB_PATH)
    radar_path = Path(radar_path or RADAR_DB_PATH)
    radar_connect(radar_path).close()  # creates/migrates the radar schema
    app_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("ATTACH DATABASE ? AS radar", (str(radar_path),))
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Serialised write transaction (BEGIN IMMEDIATE) that rolls back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def radar_path_of(conn: sqlite3.Connection) -> Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "radar":
            return Path(row[2])
    raise RuntimeError("radar database is not attached")


def r6(value: float) -> float:
    return round(float(value), 6)


# --------------------------------------------------------------------------- LMSR

def lmsr_cost(q_yes: float, q_no: float, b: float) -> float:
    a, c = q_yes / b, q_no / b
    m = max(a, c)
    return b * (m + math.log(math.exp(a - m) + math.exp(c - m)))


def lmsr_prob(q_yes: float, q_no: float, b: float) -> float:
    """Price of a YES share (= implied probability of YES)."""
    x = (q_no - q_yes) / b
    if x > 0:
        e = math.exp(-x)
        return e / (1.0 + e)
    return 1.0 / (1.0 + math.exp(x))


def shares_for_points(q_yes: float, q_no: float, b: float, side: str, points: float) -> float:
    """Shares of `side` received for spending `points` (closed form)."""
    other = q_no if side == "yes" else q_yes
    own = q_yes if side == "yes" else q_no
    a = (lmsr_cost(q_yes, q_no, b) + points) / b
    c = other / b
    new_own = b * (a + math.log1p(-math.exp(c - a)))
    return new_own - own


def points_for_shares(q_yes: float, q_no: float, b: float, side: str, shares: float) -> float:
    """Points received for selling `shares` of `side`."""
    if side == "yes":
        after = lmsr_cost(q_yes - shares, q_no, b)
    else:
        after = lmsr_cost(q_yes, q_no - shares, b)
    return lmsr_cost(q_yes, q_no, b) - after


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def clamp_prob(p: float) -> float:
    return min(max(p, PROB_MIN), PROB_MAX)


def effective_rate(rate_per_day: Optional[float], age_days: Optional[float] = None) -> float:
    """Expected future stars/day: recent velocity decays (~40 %), and launch-week hype decays much more."""
    if not rate_per_day or rate_per_day <= 0:
        return 0.0
    youth = 1.0 if age_days is None else min(max(age_days / 30.0, 0.25), 1.0)
    return 0.6 * rate_per_day * youth


def model_prob_reach(current: int, target: int, rate_per_day: Optional[float], days: float,
                     age_days: Optional[float] = None) -> float:
    """Opening odds for 'reach target in `days`' from recent star velocity."""
    needed = target - current
    if needed <= 0:
        return PROB_MAX
    rate = effective_rate(rate_per_day, age_days)
    if rate <= 0 or days <= 0:
        return PROB_MIN
    return clamp_prob(sigmoid(2.0 * math.log(rate / (needed / days))))


def repo_age_days(repo: dict, now: float) -> Optional[float]:
    created = iso_to_ts(repo.get("created_at")) if repo.get("created_at") else None
    return (now - created) / DAY if created else None


def target_for(repo: dict, days: int, now: float) -> int:
    """A round target the momentum model rates close to 50/50."""
    expected = repo["stars"] + effective_rate(repo["rate_per_day"], repo_age_days(repo, now)) * days
    return nice_target(max(expected, repo["stars"] * 1.05, repo["stars"] + 50))


def model_prob_versus(rate_a: Optional[float], rate_b: Optional[float]) -> float:
    if not rate_a or not rate_b or rate_a <= 0 or rate_b <= 0:
        return 0.5
    return clamp_prob(sigmoid(2.0 * math.log(rate_a / rate_b)))


def nice_target(value: float) -> int:
    """Round up to two significant digits, snapping to a milestone (10k, 25k…) within +15 %."""
    value = max(float(value), 1.0)
    step = 10 ** max(int(math.floor(math.log10(value))) - 1, 0)
    rounded = int(math.ceil(value / step) * step)
    for milestone in MILESTONES:
        if rounded <= milestone <= rounded * 1.15:
            return int(milestone)
    return rounded


# --------------------------------------------------------------------------- time helpers

def utc_day(ts: float) -> dt.date:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()


def day_start_ts(day: dt.date) -> float:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()


def fmt_date(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def fmt_stars(n: int) -> str:
    if n >= 1_000_000 and n % 100_000 == 0:
        return f"{n / 1_000_000:g}M"
    if n >= 10_000 and n % 1_000 == 0:
        return f"{n // 1_000}k"
    if n >= 1_000 and n % 100 == 0:
        return f"{n / 1_000:g}k"
    return f"{n:,}"


# --------------------------------------------------------------------------- users & ledger

def credit(conn, user_id: int, delta: float, kind: str, now: float,
           market_id: Optional[int] = None, note: Optional[str] = None) -> None:
    """The only way balances change: update the balance and append a ledger row (inside a transaction)."""
    delta = r6(delta)
    if abs(delta) < EPS:
        return
    conn.execute("UPDATE users SET balance = ROUND(balance + ?, 6) WHERE id = ?", (delta, user_id))
    conn.execute(
        "INSERT INTO ledger (user_id, ts, delta, kind, market_id, note) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, now, delta, kind, market_id, note),
    )


def upsert_user(conn, *, github_id: Optional[int], login: str, name: Optional[str] = None,
                avatar_url: Optional[str] = None, github_created_at: Optional[str] = None,
                is_dev: bool = False, now: Optional[float] = None) -> sqlite3.Row:
    """Create (with the signup grant) or refresh a user. Returns the user row."""
    now = now or time.time()
    with transaction(conn):
        if github_id is not None:
            row = conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM users WHERE login = ? AND is_dev = 1", (login,)).fetchone()
        if row is None:
            clash = conn.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()
            if clash is not None:
                # GitHub logins can be released and re-registered; keep the newest owner.
                conn.execute("UPDATE users SET login = login || '~' || id WHERE id = ?", (clash[0],))
            cursor = conn.execute(
                """INSERT INTO users (github_id, login, name, avatar_url, github_created_at, is_dev, balance, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (github_id, login, name, avatar_url, github_created_at, int(is_dev), now),
            )
            credit(conn, cursor.lastrowid, STARTING_BALANCE, "signup", now, note="Welcome to EarlyStar")
            user_id = cursor.lastrowid
        else:
            user_id = row["id"]
            if row["login"].lower() != login.lower():
                clash = conn.execute("SELECT id FROM users WHERE login = ? AND id != ?", (login, user_id)).fetchone()
                if clash is not None:
                    conn.execute("UPDATE users SET login = login || '~' || id WHERE id = ?", (clash[0],))
            conn.execute(
                "UPDATE users SET login = ?, name = ?, avatar_url = ?, github_created_at = COALESCE(?, github_created_at) WHERE id = ?",
                (login, name, avatar_url, github_created_at, user_id),
            )
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user(conn, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def claim_daily_bonus(conn, user_id: int, now: float) -> bool:
    today = utc_day(now).isoformat()
    with transaction(conn):
        row = conn.execute("SELECT last_bonus_day, created_ts FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or row["last_bonus_day"] == today:
            return False
        conn.execute("UPDATE users SET last_bonus_day = ? WHERE id = ?", (today, user_id))
        if utc_day(row["created_ts"]).isoformat() == today:
            return False  # the signup grant covers day one
        credit(conn, user_id, DAILY_BONUS, "daily_bonus", now, note="Daily visit bonus")
    return True


def trade_block_reason(user: sqlite3.Row, now: float) -> Optional[str]:
    if user["is_banned"]:
        return "This account is suspended."
    if not user["is_dev"] and MIN_ACCOUNT_AGE_DAYS > 0:
        created = iso_to_ts(user["github_created_at"]) if user["github_created_at"] else None
        if created is None or now - created < MIN_ACCOUNT_AGE_DAYS * DAY:
            return f"Trading needs a GitHub account at least {MIN_ACCOUNT_AGE_DAYS} days old (anti-spam)."
    return None


# --------------------------------------------------------------------------- radar access

def repo_state(conn, full_name: Optional[str] = None, repo_id: Optional[int] = None,
               now: Optional[float] = None) -> Optional[dict]:
    """Latest known stars and recent star velocity for a repo, from the radar DB."""
    now = now or time.time()
    if repo_id is not None:
        row = conn.execute("SELECT * FROM radar.repos WHERE id = ?", (repo_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM radar.repos WHERE full_name = ? ORDER BY last_seen DESC LIMIT 1", (full_name,)
        ).fetchone()
    if row is None:
        return None
    latest = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (row["id"], now),
    ).fetchone()
    stars = latest["stars"] if latest else row["stars"]
    snapshot_ts = latest["ts"] if latest else row["last_seen"]
    rate, source = star_rate(conn, row, stars, snapshot_ts, now)
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "owner": (row["owner_login"] or row["full_name"].split("/")[0]),
        "avatar_url": row["avatar_url"] or f"https://github.com/{row['full_name'].split('/')[0]}.png?size=80",
        "description": row["description"],
        "language": row["language"],
        "stars": stars,
        "snapshot_ts": snapshot_ts,
        "rate_per_day": rate,
        "rate_source": source,
        "created_at": row["created_at"],
    }


def star_rate(conn, repo_row, stars: int, snapshot_ts: float, now: float):
    """Stars/day over roughly the last 7 days, with honest fallbacks."""
    created = iso_to_ts(repo_row["created_at"]) or 0
    if created and now - created < 7 * DAY:
        return stars / max((snapshot_ts - created) / DAY, 1 / 24), "new-repo"
    base = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (repo_row["id"], now - 7 * DAY + 3600),
    ).fetchone()
    if base is not None and snapshot_ts - base["ts"] > 3600:
        return (stars - base["stars"]) / ((snapshot_ts - base["ts"]) / DAY), "tracked-7d"
    first = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? ORDER BY ts LIMIT 1", (repo_row["id"],)
    ).fetchone()
    if first is not None and snapshot_ts - first["ts"] >= 12 * 3600:
        return (stars - first["stars"]) / ((snapshot_ts - first["ts"]) / DAY), "tracked-short"
    weekly = conn.execute(
        """SELECT period_stars FROM radar.trending WHERE full_name = ? AND since = 'weekly'
           AND period_stars IS NOT NULL ORDER BY day DESC LIMIT 1""",
        (repo_row["full_name"],),
    ).fetchone()
    if weekly is not None:
        return weekly[0] / 7.0, "github-trending"
    if created:
        return stars / max((now - created) / DAY, 1.0), "lifetime"
    return None, "unknown"


def snapshot_at_close(conn, repo_id: int, close_ts: float, now: float):
    """Closing value: first snapshot in [close, close+6h]; else last in [close-3h, close) once 6h passed.

    Returns (stars, ts) or None when undecidable yet, or "missing" when it never will be.
    """
    after = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts >= ? AND ts <= ? ORDER BY ts LIMIT 1",
        (repo_id, close_ts, close_ts + CLOSE_LOOKAHEAD),
    ).fetchone()
    if after is not None:
        return after["stars"], after["ts"]
    if now < close_ts + CLOSE_LOOKAHEAD:
        return None
    before = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts < ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
        (repo_id, close_ts, close_ts - CLOSE_LOOKBACK),
    ).fetchone()
    if before is not None:
        return before["stars"], before["ts"]
    return "missing"


def trending_days_logged(conn, full_names, start_day: dt.date, days: int) -> set:
    end_day = start_day + dt.timedelta(days=days - 1)
    names = [name for name in full_names if name]
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""SELECT DISTINCT day FROM radar.trending
            WHERE since = 'daily' AND language = '' AND day >= ? AND day <= ?
              AND full_name COLLATE NOCASE IN ({placeholders})""",
        (start_day.isoformat(), end_day.isoformat(), *names),
    )
    return {row[0] for row in rows}


def trending_coverage(conn, start_day: dt.date, days: int) -> set:
    end_day = start_day + dt.timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT DISTINCT day FROM radar.trending WHERE since = 'daily' AND language = '' AND day >= ? AND day <= ?",
        (start_day.isoformat(), end_day.isoformat()),
    )
    return {row[0] for row in rows}


def star_spike(conn, repo_id: int, end_ts: float) -> Optional[dict]:
    """Detect a suspicious star burst in the 48 h before `end_ts` relative to the prior week."""
    end = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (repo_id, end_ts),
    ).fetchone()
    mid = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (repo_id, end_ts - SPIKE_WINDOW),
    ).fetchone()
    if end is None or mid is None or end["ts"] - mid["ts"] < 3600:
        return None
    base = conn.execute(
        "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (repo_id, mid["ts"] - SPIKE_BASELINE_DAYS * DAY),
    ).fetchone()
    if base is None:
        base = conn.execute(
            "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts < ? ORDER BY ts LIMIT 1",
            (repo_id, mid["ts"] - 2 * DAY),
        ).fetchone()
    if base is None or mid["ts"] - base["ts"] < 2 * DAY:
        return None
    recent = (end["stars"] - mid["stars"]) / (end["ts"] - mid["ts"]) * SPIKE_WINDOW
    baseline = (mid["stars"] - base["stars"]) / (mid["ts"] - base["ts"]) * SPIKE_WINDOW
    flagged = recent >= SPIKE_MIN_STARS and recent > SPIKE_FACTOR * max(baseline, 20.0)
    return {"recent_48h": round(recent), "baseline_48h": round(baseline), "flagged": flagged}


def pin_repo(conn, repo_id: int, full_name: str, until_ts: float) -> None:
    conn.execute(
        """INSERT INTO radar.pinned (repo_id, full_name, until_ts) VALUES (?, ?, ?)
           ON CONFLICT(repo_id) DO UPDATE SET full_name = excluded.full_name,
                                              until_ts = MAX(until_ts, excluded.until_ts)""",
        (repo_id, full_name, until_ts),
    )


def refresh_pin(conn, repo_id: Optional[int]) -> None:
    if repo_id is None:
        return
    row = conn.execute(
        """SELECT MAX(close_ts) FROM markets WHERE status IN ('open', 'review')
           AND (repo_id = ? OR repo_b_id = ?)""",
        (repo_id, repo_id),
    ).fetchone()
    if row[0] is None:
        conn.execute("DELETE FROM radar.pinned WHERE repo_id = ?", (repo_id,))
    else:
        conn.execute("UPDATE radar.pinned SET until_ts = ? WHERE repo_id = ?", (row[0] + PIN_GRACE, repo_id))


# --------------------------------------------------------------------------- market creation

def question_for(kind: str, repo: str, repo_b: Optional[str], target: Optional[int],
                 close_ts: float, window_start: Optional[dt.date] = None, window_days: Optional[int] = None) -> str:
    deadline = utc_day(close_ts - 1)
    if kind == "stars_reach":
        return f"Will {repo} reach {fmt_stars(target)}★ by {fmt_date(deadline)}?"
    if kind == "versus":
        return f"Will {repo} gain more stars than {repo_b} by {fmt_date(deadline)}?"
    end = window_start + dt.timedelta(days=window_days - 1)
    span = f"{window_start:%b} {window_start.day}–{end:%b} {end.day}"
    return f"Will {repo} be on GitHub Trending on at least {target} of the {window_days} days {span}?"


def rules_for(market) -> list:
    m = dict(market)
    deadline = dt.datetime.fromtimestamp(m["close_ts"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    common = [
        f"Trading closes at {deadline}.",
        "Settled automatically from EarlyStar's own GitHub snapshots (collected every 30 minutes).",
        "Points are play money with no cash value. Repo owners can't trade calls on their own repos.",
        "A suspicious star spike just before a YES puts the call into review; an admin then settles or voids it.",
    ]
    if m["kind"] == "stars_reach":
        specific = [
            f"YES as soon as any snapshot taken before the deadline shows at least {m['target']:,} stars.",
            "Otherwise settles on the first snapshot taken at or after the deadline (normally within 30 min).",
            "If there is none within 6 h, the last snapshot within 3 h before the deadline is used; otherwise the call is voided and everyone is refunded.",
        ]
    elif m["kind"] == "versus":
        specific = [
            f"Gains are measured from the stars recorded when the call opened ({m['repo']}: {m['start_stars']:,}, {m['repo_b']}: {m['start_stars_b']:,}).",
            "Closing values use the first snapshot at or after the deadline for each repo (same fallbacks as star calls).",
            f"YES if {m['repo']} gained strictly more stars. A tie or missing data voids the call.",
        ]
    else:
        start = dt.date.fromisoformat(m["window_start_day"])
        end = start + dt.timedelta(days=m["window_days"] - 1)
        specific = [
            f"Counts UTC days {start.isoformat()} to {end.isoformat()} on which {m['repo']} appears on GitHub Trending (daily, all languages) in any EarlyStar capture.",
            f"YES as soon as {m['target']} such days are logged; NO when the window ends.",
            "If EarlyStar missed days and those days could have changed the answer, the call is voided.",
        ]
    return specific + common


def _insert_market(conn, *, kind: str, repo: dict, repo_b: Optional[dict], target: Optional[int],
                   window_start: Optional[dt.date], window_days: Optional[int], open_ts: float,
                   close_ts: float, created_by: Optional[int], liquidity: float, initial_prob: float) -> int:
    initial_prob = clamp_prob(initial_prob)
    question = question_for(kind, repo["full_name"], repo_b["full_name"] if repo_b else None, target,
                            close_ts, window_start, window_days)
    cursor = conn.execute(
        """INSERT INTO markets (kind, repo, repo_id, repo_b, repo_b_id, target, window_start_day, window_days,
                                question, open_ts, close_ts, created_by, liquidity, q_yes, q_no, initial_prob,
                                start_stars, start_stars_b)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
        (
            kind, repo["full_name"], repo["id"],
            repo_b["full_name"] if repo_b else None, repo_b["id"] if repo_b else None,
            target, window_start.isoformat() if window_start else None, window_days,
            question, open_ts, close_ts, created_by, liquidity,
            r6(liquidity * logit(initial_prob)), r6(initial_prob),
            repo["stars"], repo_b["stars"] if repo_b else None,
        ),
    )
    pin_repo(conn, repo["id"], repo["full_name"], close_ts + PIN_GRACE)
    if repo_b:
        pin_repo(conn, repo_b["id"], repo_b["full_name"], close_ts + PIN_GRACE)
    return cursor.lastrowid


def deadline_ts(now: float, days: int) -> float:
    """End of the UTC day `days` days from now."""
    return day_start_ts(utc_day(now) + dt.timedelta(days=days + 1))


def default_fetch_repo(full_name: str) -> dict:
    from collector import GitHubClient, load_token
    import urllib.error

    try:
        data, _ = GitHubClient(load_token()).api("/repos/" + full_name)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise MarketError("That repository doesn't exist on GitHub (or is private).", 404)
        raise MarketError(f"GitHub lookup failed ({error.code}). Try again in a minute.", 502)
    return data


def ensure_repo(conn, full_name: str, now: float, fetch_repo: Optional[Callable[[str], dict]] = None) -> dict:
    """Make sure the radar has a fresh snapshot of `full_name`, fetching it from GitHub if needed."""
    full_name = full_name.strip().strip("/")
    if full_name.lower().startswith("https://github.com/"):
        full_name = full_name[len("https://github.com/"):].strip("/")
    if not REPO_NAME_RE.fullmatch(full_name) or ".." in full_name:
        raise MarketError("Repository must look like owner/name.")
    state = repo_state(conn, full_name=full_name, now=now)
    if state is not None and now - (state["snapshot_ts"] or 0) <= REPO_FRESH_SECONDS:
        return state
    item = (fetch_repo or default_fetch_repo)(full_name)
    if item.get("private") or item.get("archived"):
        raise MarketError("Calls need a public, non-archived repository.")
    radar = radar_connect(radar_path_of(conn))
    try:
        with radar:
            upsert_repo(radar, item, now)
    finally:
        radar.close()
    state = repo_state(conn, repo_id=item["id"], now=now)
    if state is None:
        raise MarketError("Couldn't record that repository. Try again.", 502)
    return state


def suggest(conn, full_name: str, now: float, target: Optional[int] = None, days: int = 30,
            fetch_repo: Optional[Callable[[str], dict]] = None) -> dict:
    repo = ensure_repo(conn, full_name, now, fetch_repo)
    if target is None:
        target = target_for(repo, days, now)
    return {
        "repo": repo,
        "days": days,
        "target": target,
        "model_prob": round(model_prob_reach(repo["stars"], target, repo["rate_per_day"], days, repo_age_days(repo, now)), 4),
        "open_calls": [market_summary(conn, row, now) for row in conn.execute(
            "SELECT * FROM markets WHERE status IN ('open', 'review') AND (repo_id = ? OR repo_b_id = ?) ORDER BY close_ts",
            (repo["id"], repo["id"]),
        )],
    }


def create_user_market(conn, user: sqlite3.Row, payload: dict, now: Optional[float] = None,
                       fetch_repo: Optional[Callable[[str], dict]] = None) -> int:
    """Validate and create a market proposed by a user (charges the creation fee)."""
    now = now or time.time()
    reason = trade_block_reason(user, now)
    if reason:
        raise MarketError(reason, 403)
    kind = payload.get("kind")
    if kind not in ("stars_reach", "versus"):
        raise MarketError("kind must be stars_reach or versus.")
    try:
        days = int(payload.get("days"))
    except (TypeError, ValueError):
        raise MarketError("days must be a whole number.")
    repo = ensure_repo(conn, str(payload.get("repo") or ""), now, fetch_repo)
    owners = {repo["owner"].lower()}
    target = None
    repo_b = None
    if kind == "stars_reach":
        low, high = USER_STARS_DAYS
        if not low <= days <= high:
            raise MarketError(f"Star calls run {low}–{high} days.")
        try:
            target = int(payload.get("target"))
        except (TypeError, ValueError):
            raise MarketError("target must be a whole number of stars.")
        minimum = max(repo["stars"] + 10, math.ceil(repo["stars"] * 1.02))
        if target < minimum:
            raise MarketError(f"Target must be at least {minimum:,} stars (current: {repo['stars']:,}).")
        if target > repo["stars"] * 50 + 1000:
            raise MarketError("That target is unrealistically high.")
        duplicate = conn.execute(
            "SELECT id FROM markets WHERE status = 'open' AND kind = 'stars_reach' AND repo_id = ? AND target = ?",
            (repo["id"], target),
        ).fetchone()
        prob = model_prob_reach(repo["stars"], target, repo["rate_per_day"], days, repo_age_days(repo, now))
    else:
        low, high = USER_VERSUS_DAYS
        if not low <= days <= high:
            raise MarketError(f"Versus calls run {low}–{high} days.")
        repo_b = ensure_repo(conn, str(payload.get("repo_b") or ""), now, fetch_repo)
        if repo_b["id"] == repo["id"]:
            raise MarketError("Pick two different repositories.")
        owners.add(repo_b["owner"].lower())
        duplicate = conn.execute(
            """SELECT id FROM markets WHERE status = 'open' AND kind = 'versus'
               AND ((repo_id = ? AND repo_b_id = ?) OR (repo_id = ? AND repo_b_id = ?))""",
            (repo["id"], repo_b["id"], repo_b["id"], repo["id"]),
        ).fetchone()
        prob = model_prob_versus(repo["rate_per_day"], repo_b["rate_per_day"])
    if duplicate is not None:
        raise MarketError(f"That call already exists (#{duplicate[0]}).", 409)
    if user["login"].lower() in owners:
        raise MarketError("You can't create calls on your own repository.", 403)

    with transaction(conn):
        fresh = get_user(conn, user["id"])
        created_today = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE created_by = ? AND open_ts > ?", (user["id"], now - DAY)
        ).fetchone()[0]
        if created_today >= MAX_USER_MARKETS_PER_DAY:
            raise MarketError(f"You can create {MAX_USER_MARKETS_PER_DAY} calls per day.", 429)
        if fresh["balance"] < CREATE_FEE - EPS:
            raise MarketError(f"Creating a call costs {CREATE_FEE:g} points.", 402)
        market_id = _insert_market(
            conn, kind=kind, repo=repo, repo_b=repo_b, target=target, window_start=None, window_days=None,
            open_ts=now, close_ts=deadline_ts(now, days), created_by=user["id"],
            liquidity=LIQUIDITY_USER, initial_prob=prob,
        )
        credit(conn, user["id"], -CREATE_FEE, "create_fee", now, market_id, "Call creation fee")
    return market_id


# --------------------------------------------------------------------------- trading

def market_row(conn, market_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if row is None:
        raise MarketError("No such call.", 404)
    return row


def known_early_outcome(conn, market, now: float) -> Optional[str]:
    """'yes' if the radar already shows the YES condition met (trading halts until settlement)."""
    if market["kind"] == "stars_reach":
        hit = conn.execute(
            """SELECT 1 FROM radar.snapshots WHERE repo_id = ? AND ts >= ? AND ts < ? AND ts <= ?
               AND stars >= ? LIMIT 1""",
            (market["repo_id"], market["open_ts"], market["close_ts"], now, market["target"]),
        ).fetchone()
        return "yes" if hit else None
    if market["kind"] == "trending_days":
        start = dt.date.fromisoformat(market["window_start_day"])
        days = trending_days_logged(conn, _names_for(conn, market), start, market["window_days"])
        return "yes" if len(days) >= market["target"] else None
    return None


def _names_for(conn, market) -> list:
    names = [market["repo"]]
    current = conn.execute("SELECT full_name FROM radar.repos WHERE id = ?", (market["repo_id"],)).fetchone()
    if current is not None and current[0].lower() != market["repo"].lower():
        names.append(current[0])
    return names


def _price_quote(market, side: str, action: str, amount: float) -> dict:
    b = market["liquidity"]
    q_yes, q_no = market["q_yes"], market["q_no"]
    before = lmsr_prob(q_yes, q_no, b)
    if action == "buy":
        shares = shares_for_points(q_yes, q_no, b, side, amount)
        points = amount
        new_q = (q_yes + shares, q_no) if side == "yes" else (q_yes, q_no + shares)
    else:
        shares = amount
        points = points_for_shares(q_yes, q_no, b, side, amount)
        new_q = (q_yes - shares, q_no) if side == "yes" else (q_yes, q_no - shares)
    after = lmsr_prob(new_q[0], new_q[1], b)
    return {
        "side": side,
        "action": action,
        "shares": r6(shares),
        "points": r6(points),
        "avg_price": r6(points / shares) if shares > EPS else None,
        "prob_before": r6(before),
        "prob_after": r6(after),
        "payout_if_win": r6(shares) if action == "buy" else None,
        "new_q": new_q,
    }


def _validate_trade_args(side, action, amount) -> float:
    if side not in SIDES:
        raise MarketError("side must be yes or no.")
    if action not in ACTIONS:
        raise MarketError("action must be buy or sell.")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise MarketError("amount must be a number.")
    if not math.isfinite(amount) or amount <= 0:
        raise MarketError("amount must be positive.")
    if action == "buy" and amount < MIN_TRADE_POINTS:
        raise MarketError(f"Minimum trade is {MIN_TRADE_POINTS:g} point.")
    if amount > 1_000_000:
        raise MarketError("amount is too large.")
    return amount


def quote(conn, market_id: int, side: str, action: str, amount, now: Optional[float] = None) -> dict:
    now = now or time.time()
    amount = _validate_trade_args(side, action, amount)
    market = market_row(conn, market_id)
    result = _price_quote(market, side, action, amount)
    result.pop("new_q")
    result["tradable"] = market["status"] == "open" and market["open_ts"] <= now < market["close_ts"]
    return result


def execute_trade(conn, user_id: int, market_id: int, side: str, action: str, amount,
                  min_shares: Optional[float] = None, min_points: Optional[float] = None,
                  now: Optional[float] = None) -> dict:
    """Buy (amount = points to spend) or sell (amount = shares) against the LMSR market maker."""
    now = now or time.time()
    amount = _validate_trade_args(side, action, amount)
    with transaction(conn):
        user = get_user(conn, user_id)
        if user is None:
            raise MarketError("Sign in to trade.", 401)
        reason = trade_block_reason(user, now)
        if reason:
            raise MarketError(reason, 403)
        market = market_row(conn, market_id)
        if market["status"] != "open" or now >= market["close_ts"]:
            raise MarketError("This call is closed for trading.", 409)
        if now < market["open_ts"]:
            raise MarketError("This call hasn't opened yet.", 409)
        owners = {market["repo"].split("/")[0].lower()}
        if market["repo_b"]:
            owners.add(market["repo_b"].split("/")[0].lower())
        for repo_id in (market["repo_id"], market["repo_b_id"]):
            if repo_id is not None:
                owner = conn.execute("SELECT owner_login FROM radar.repos WHERE id = ?", (repo_id,)).fetchone()
                if owner is not None and owner[0]:
                    owners.add(owner[0].lower())
        if user["login"].lower() in owners:
            raise MarketError("Repo owners can't trade calls on their own repos.", 403)
        if known_early_outcome(conn, market, now):
            raise MarketError("The YES condition was just met — this call is settling.", 409)
        recent = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id = ? AND ts > ?", (user_id, now - 3600)
        ).fetchone()[0]
        if recent >= MAX_TRADES_PER_HOUR:
            raise MarketError("Slow down — too many trades this hour.", 429)

        position = conn.execute(
            "SELECT * FROM positions WHERE user_id = ? AND market_id = ?", (user_id, market_id)
        ).fetchone()
        held = (position[f"{side}_shares"] if position else 0.0)
        at_risk = (position["spent"] - position["received"]) if position else 0.0

        if action == "buy":
            if amount > user["balance"] + EPS:
                raise MarketError(f"Not enough points (balance {user['balance']:.1f}).", 402)
            if at_risk + amount > MAX_STAKE_PER_MARKET + EPS:
                room = max(MAX_STAKE_PER_MARKET - at_risk, 0)
                raise MarketError(f"Max {MAX_STAKE_PER_MARKET:g} points at risk per call — you can add {room:.1f} more.", 409)
        else:
            if held <= EPS:
                raise MarketError(f"You don't hold any {side.upper()} shares.", 409)
            if amount > held + 1e-6:
                raise MarketError(f"You only hold {held:.2f} {side.upper()} shares.", 409)
            amount = min(amount, held)

        result = _price_quote(market, side, action, amount)
        if action == "buy" and min_shares is not None and result["shares"] + 1e-6 < float(min_shares):
            raise MarketError("Price moved — you'd get fewer shares than expected. Review and retry.", 409)
        if action == "sell" and min_points is not None and result["points"] + 1e-6 < float(min_points):
            raise MarketError("Price moved — you'd get fewer points than expected. Review and retry.", 409)

        shares, points = result["shares"], result["points"]
        new_q_yes, new_q_no = result["new_q"]
        conn.execute("UPDATE markets SET q_yes = ?, q_no = ?, volume = ROUND(volume + ?, 6), trade_count = trade_count + 1 WHERE id = ?",
                     (r6(new_q_yes), r6(new_q_no), points, market_id))
        if position is None:
            conn.execute("INSERT INTO positions (user_id, market_id) VALUES (?, ?)", (user_id, market_id))
        sign = 1 if action == "buy" else -1
        conn.execute(
            f"""UPDATE positions SET {side}_shares = MAX(ROUND({side}_shares + ?, 6), 0),
                                     spent = ROUND(spent + ?, 6), received = ROUND(received + ?, 6)
                WHERE user_id = ? AND market_id = ?""",
            (sign * shares, points if action == "buy" else 0, points if action == "sell" else 0, user_id, market_id),
        )
        credit(conn, user_id, -points if action == "buy" else points, action, now, market_id)
        conn.execute(
            """INSERT INTO trades (market_id, user_id, ts, side, action, shares, points, prob_before, prob_after)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (market_id, user_id, now, side, action, shares, points, result["prob_before"], result["prob_after"]),
        )
    result.pop("new_q")
    result["balance"] = r6(get_user(conn, user_id)["balance"])
    result["position"] = position_dict(conn.execute(
        "SELECT * FROM positions WHERE user_id = ? AND market_id = ?", (user_id, market_id)).fetchone())
    return result


def position_dict(row) -> Optional[dict]:
    if row is None:
        return None
    return {
        "yes_shares": r6(row["yes_shares"]),
        "no_shares": r6(row["no_shares"]),
        "spent": r6(row["spent"]),
        "received": r6(row["received"]),
        "payout": r6(row["payout"]),
    }


# --------------------------------------------------------------------------- settlement

def settle(conn, market_id: int, outcome: str, note: str, now: float) -> None:
    """Pay 1 point per winning share. Must be called inside a transaction."""
    market = market_row(conn, market_id)
    if market["status"] in ("resolved", "void"):
        raise MarketError("Call already settled.", 409)
    if outcome not in SIDES:
        raise MarketError("outcome must be yes or no.")
    for position in conn.execute("SELECT * FROM positions WHERE market_id = ?", (market_id,)).fetchall():
        payout = position["yes_shares"] if outcome == "yes" else position["no_shares"]
        if payout > EPS:
            credit(conn, position["user_id"], payout, "payout", now, market_id, f"Won: {outcome.upper()}")
        conn.execute("UPDATE positions SET payout = ? WHERE user_id = ? AND market_id = ?",
                     (r6(payout), position["user_id"], market_id))
    conn.execute(
        "UPDATE markets SET status = 'resolved', outcome = ?, resolved_ts = ?, resolution_note = ? WHERE id = ?",
        (outcome, now, note, market_id),
    )
    refresh_pin(conn, market["repo_id"])
    refresh_pin(conn, market["repo_b_id"])


def void(conn, market_id: int, note: str, now: float) -> None:
    """Refund every trader's net cost; claw back selling profits (capped at balance). Inside a transaction."""
    market = market_row(conn, market_id)
    if market["status"] in ("resolved", "void"):
        raise MarketError("Call already settled.", 409)
    for position in conn.execute("SELECT * FROM positions WHERE market_id = ?", (market_id,)).fetchall():
        net = r6(position["spent"] - position["received"])
        if net > EPS:
            credit(conn, position["user_id"], net, "refund", now, market_id, "Call voided: refund")
        elif net < -EPS:
            balance = get_user(conn, position["user_id"])["balance"]
            claw = min(-net, max(balance, 0.0))
            if claw > EPS:
                credit(conn, position["user_id"], -claw, "clawback", now, market_id, "Call voided: profit returned")
    conn.execute(
        "UPDATE markets SET status = 'void', outcome = NULL, resolved_ts = ?, resolution_note = ? WHERE id = ?",
        (now, note, market_id),
    )
    refresh_pin(conn, market["repo_id"])
    refresh_pin(conn, market["repo_b_id"])


def evaluate(conn, market, now: float):
    """Decide a market from radar data: ('yes'|'no'|'void'|'wait', note, spike_repo_id)."""
    kind = market["kind"]
    if kind == "stars_reach":
        hit = conn.execute(
            """SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts >= ? AND ts < ? AND ts <= ?
               AND stars >= ? ORDER BY ts LIMIT 1""",
            (market["repo_id"], market["open_ts"], market["close_ts"], now, market["target"]),
        ).fetchone()
        if hit is not None:
            when = dt.datetime.fromtimestamp(hit["ts"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return "yes", f"Reached {hit['stars']:,}★ (target {market['target']:,}) at {when}.", market["repo_id"]
        if now < market["close_ts"]:
            return "wait", None, None
        closing = snapshot_at_close(conn, market["repo_id"], market["close_ts"], now)
        if closing is None:
            return "wait", None, None
        if closing == "missing":
            return "void", "No snapshot near the deadline — refunded.", None
        stars, ts = closing
        when = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if stars >= market["target"]:
            return "yes", f"Closing snapshot {stars:,}★ at {when} (target {market['target']:,}).", market["repo_id"]
        return "no", f"Closing snapshot {stars:,}★ at {when}, short of {market['target']:,}.", None

    if kind == "versus":
        if now < market["close_ts"]:
            return "wait", None, None
        closing_a = snapshot_at_close(conn, market["repo_id"], market["close_ts"], now)
        closing_b = snapshot_at_close(conn, market["repo_b_id"], market["close_ts"], now)
        if closing_a is None or closing_b is None:
            return "wait", None, None
        if closing_a == "missing" or closing_b == "missing":
            return "void", "Missing snapshot near the deadline — refunded.", None
        gain_a = closing_a[0] - market["start_stars"]
        gain_b = closing_b[0] - market["start_stars_b"]
        note = f"{market['repo']} +{gain_a:,}★ vs {market['repo_b']} +{gain_b:,}★."
        if gain_a == gain_b:
            return "void", note + " Tie — refunded.", None
        if gain_a > gain_b:
            return "yes", note, market["repo_id"]
        return "no", note, market["repo_b_id"]

    start = dt.date.fromisoformat(market["window_start_day"])
    days = market["window_days"]
    logged = trending_days_logged(conn, _names_for(conn, market), start, days)
    if len(logged) >= market["target"]:
        return "yes", f"On GitHub Trending on {len(logged)} days ({', '.join(sorted(logged))}).", market["repo_id"]
    if now < market["close_ts"]:
        return "wait", None, None
    coverage = trending_coverage(conn, start, days)
    missing = days - len(coverage)
    if len(coverage) < math.ceil(TRENDING_COVERAGE_MIN * days) or len(logged) + missing >= market["target"]:
        return "void", f"EarlyStar recorded Trending on only {len(coverage)} of {days} days — refunded.", None
    return "no", f"On GitHub Trending on {len(logged)} of {days} days (needed {market['target']}).", None


def resolve_due(conn, now: Optional[float] = None) -> dict:
    """Settle every market whose outcome the radar data now determines."""
    now = now or time.time()
    summary = {"resolved": 0, "void": 0, "review": 0, "checked": 0}
    rows = conn.execute("SELECT * FROM markets WHERE status = 'open' ORDER BY close_ts").fetchall()
    for market in rows:
        if market["kind"] == "versus" and now < market["close_ts"]:
            continue
        summary["checked"] += 1
        decision, note, beneficiary = evaluate(conn, market, now)
        if decision == "wait":
            continue
        with transaction(conn):
            current = market_row(conn, market["id"])
            if current["status"] != "open":
                continue
            if decision == "void":
                void(conn, market["id"], note, now)
                summary["void"] += 1
                continue
            # Bought stars help whichever repo "wins": YES for stars/trending calls, either side for versus.
            spike_relevant = market["kind"] == "versus" or decision == "yes"
            spike = star_spike(conn, beneficiary, min(now, market["close_ts"])) if beneficiary and spike_relevant else None
            if spike_relevant:
                if spike and spike["flagged"]:
                    flag = (f"Star spike: +{spike['recent_48h']:,}★ in 48 h vs a {spike['baseline_48h']:,}★ baseline. "
                            f"Proposed outcome {decision.upper()}: {note}")
                    conn.execute("UPDATE markets SET status = 'review', flag_note = ? WHERE id = ?", (flag, market["id"]))
                    summary["review"] += 1
                    continue
            settle(conn, market["id"], decision, note, now)
            summary["resolved"] += 1
    return summary


def admin_resolve(conn, market_id: int, outcome: str, note: str, now: Optional[float] = None) -> None:
    now = now or time.time()
    with transaction(conn):
        market = market_row(conn, market_id)
        if market["status"] in ("resolved", "void"):
            raise MarketError("Call already settled.", 409)
        if outcome == "void":
            void(conn, market_id, note or "Voided by an admin.", now)
        elif outcome in SIDES:
            if market["status"] == "open" and now < market["close_ts"] and not known_early_outcome(conn, market, now):
                raise MarketError("Open calls can only be voided before the deadline.", 409)
            settle(conn, market_id, outcome, note or f"Settled {outcome.upper()} by an admin.", now)
        else:
            raise MarketError("outcome must be yes, no or void.")


# --------------------------------------------------------------------------- auto-generation

def generate_markets(conn, now: Optional[float] = None) -> dict:
    """Create calls from the radar: stars-reach for top movers, trending streaks, and versus pairs."""
    now = now or time.time()
    from server import query_movers  # lazy: server imports this module

    last_run = conn.execute("SELECT MAX(ts) FROM radar.runs").fetchone()[0]
    if last_run is None or now - last_run > AUTO_MAX_DATA_AGE:
        return None  # wait for a fresh collection rather than pricing calls off stale stars
    created = {"stars_reach": [], "trending_days": [], "versus": []}
    open_auto = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE created_by IS NULL AND status = 'open'"
    ).fetchone()[0]
    recent_auto = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE created_by IS NULL AND open_ts > ?", (now - DAY,)
    ).fetchone()[0]
    budget = min(AUTO_MAX_OPEN - open_auto, AUTO_MAX_PER_DAY - recent_auto)
    if budget <= 0:
        return {"created": created, "budget": 0}
    busy = {row[0] for row in conn.execute(
        "SELECT repo_id FROM markets WHERE status IN ('open', 'review') UNION "
        "SELECT repo_b_id FROM markets WHERE status IN ('open', 'review') AND repo_b_id IS NOT NULL")}

    movers = query_movers(conn, "7d", None, 300, 80)["items"]
    candidates = []
    for item in movers:
        if item["gain_source"] not in ("tracked", "exact-new", "github-trending") or item["gain"] < 150:
            continue
        state = repo_state(conn, repo_id=item["id"], now=now)
        if state is None or now - (state["snapshot_ts"] or 0) > REPO_FRESH_SECONDS:
            continue
        state["rate_per_day"] = item["gain_per_day"]
        candidates.append(state)

    # 1. stars_reach for the strongest movers
    for repo in candidates:
        if budget <= 0 or len(created["stars_reach"]) >= AUTO_STARS_PER_RUN:
            break
        if repo["id"] in busy:
            continue
        target = target_for(repo, AUTO_STARS_DAYS, now)
        if target - repo["stars"] < 100:
            continue
        prob = model_prob_reach(repo["stars"], target, repo["rate_per_day"], AUTO_STARS_DAYS, repo_age_days(repo, now))
        if not 0.15 <= prob <= 0.85:
            continue
        with transaction(conn):
            market_id = _insert_market(
                conn, kind="stars_reach", repo=repo, repo_b=None, target=target, window_start=None,
                window_days=None, open_ts=now, close_ts=deadline_ts(now, AUTO_STARS_DAYS), created_by=None,
                liquidity=LIQUIDITY_AUTO, initial_prob=prob,
            )
        created["stars_reach"].append(market_id)
        busy.add(repo["id"])
        budget -= 1

    # 2. trending_days for today's top-10 Trending repos
    latest_day = conn.execute(
        "SELECT MAX(day) FROM radar.trending WHERE since = 'daily' AND language = ''"
    ).fetchone()[0]
    if latest_day:
        rows = conn.execute(
            """SELECT full_name, rank FROM radar.trending WHERE day = ? AND since = 'daily' AND language = ''
               ORDER BY rank LIMIT 10""",
            (latest_day,),
        ).fetchall()
        for row in rows:
            if budget <= 0 or len(created["trending_days"]) >= AUTO_TRENDING_PER_RUN:
                break
            repo = repo_state(conn, full_name=row["full_name"], now=now)
            if repo is None or repo["id"] in busy:
                continue
            streak = len(trending_days_logged(conn, [repo["full_name"]], utc_day(now) - dt.timedelta(days=6), 7))
            prob = min(max(0.25 + 0.08 * streak, 0.2), 0.75)
            start = utc_day(now) + dt.timedelta(days=1)
            with transaction(conn):
                market_id = _insert_market(
                    conn, kind="trending_days", repo=repo, repo_b=None, target=AUTO_TRENDING_NEEDED,
                    window_start=start, window_days=AUTO_TRENDING_DAYS, open_ts=now,
                    close_ts=day_start_ts(start + dt.timedelta(days=AUTO_TRENDING_DAYS)), created_by=None,
                    liquidity=LIQUIDITY_AUTO, initial_prob=prob,
                )
            created["trending_days"].append(market_id)
            busy.add(repo["id"])
            budget -= 1

    # 3. versus: two top movers with similar daily gains
    pool = [repo for repo in candidates[:15] if repo["id"] not in busy and repo["rate_per_day"]]
    for i, first in enumerate(pool):
        if budget <= 0 or len(created["versus"]) >= AUTO_VERSUS_PER_RUN:
            break
        for second in pool[i + 1:]:
            ratio = first["rate_per_day"] / second["rate_per_day"]
            if 0.8 <= ratio <= 1.25 and first["owner"].lower() != second["owner"].lower():
                with transaction(conn):
                    market_id = _insert_market(
                        conn, kind="versus", repo=first, repo_b=second, target=None, window_start=None,
                        window_days=None, open_ts=now, close_ts=deadline_ts(now, AUTO_VERSUS_DAYS),
                        created_by=None, liquidity=LIQUIDITY_AUTO,
                        initial_prob=model_prob_versus(first["rate_per_day"], second["rate_per_day"]),
                    )
                created["versus"].append(market_id)
                busy.update({first["id"], second["id"]})
                budget -= 1
                break
    return {"created": created, "budget": budget}


def run_job_if_due(conn, name: str, interval: float, job: Callable, now: Optional[float] = None):
    now = now or time.time()
    row = conn.execute("SELECT last_ts FROM jobs WHERE name = ?", (name,)).fetchone()
    if row is not None and row[0] and now - row[0] < interval:
        return None
    result = job(conn, now)
    if result is None:
        return None  # job declined to run (e.g. stale data); try again next tick
    import json

    conn.execute(
        "INSERT INTO jobs (name, last_ts, detail) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET last_ts = excluded.last_ts, detail = excluded.detail",
        (name, now, json.dumps(result, default=str)),
    )
    return result


# --------------------------------------------------------------------------- read models

def display_status(market, now: float) -> str:
    if market["status"] == "open":
        return "open" if now < market["close_ts"] else "closed"
    return market["status"]


def market_summary(conn, row, now: float, extra: Optional[dict] = None) -> dict:
    prob = lmsr_prob(row["q_yes"], row["q_no"], row["liquidity"])
    day_ago = conn.execute(
        "SELECT prob_after FROM trades WHERE market_id = ? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
        (row["id"], now - DAY),
    ).fetchone()
    reference = day_ago[0] if day_ago is not None else row["initial_prob"]
    repo = conn.execute("SELECT owner_login, avatar_url, stars, language FROM radar.repos WHERE id = ?",
                        (row["repo_id"],)).fetchone()
    stars_now = conn.execute(
        "SELECT stars FROM radar.snapshots WHERE repo_id = ? ORDER BY ts DESC LIMIT 1", (row["repo_id"],)
    ).fetchone()
    stars_b = None
    if row["repo_b_id"] is not None:
        stars_b = conn.execute(
            "SELECT stars FROM radar.snapshots WHERE repo_id = ? ORDER BY ts DESC LIMIT 1", (row["repo_b_id"],)
        ).fetchone()
    traders = conn.execute("SELECT COUNT(DISTINCT user_id) FROM trades WHERE market_id = ?", (row["id"],)).fetchone()[0]
    creator = None
    if row["created_by"] is not None:
        creator_row = conn.execute("SELECT login FROM users WHERE id = ?", (row["created_by"],)).fetchone()
        creator = creator_row[0] if creator_row else None
    owner = row["repo"].split("/")[0]
    summary = {
        "id": row["id"],
        "kind": row["kind"],
        "question": row["question"],
        "repo": row["repo"],
        "repo_id": row["repo_id"],
        "repo_b": row["repo_b"],
        "repo_b_id": row["repo_b_id"],
        "target": row["target"],
        "window_start_day": row["window_start_day"],
        "window_days": row["window_days"],
        "open_ts": row["open_ts"],
        "close_ts": row["close_ts"],
        "status": display_status(row, now),
        "outcome": row["outcome"],
        "prob": round(prob, 4),
        "change_24h": round(prob - reference, 4),
        "initial_prob": row["initial_prob"],
        "volume": round(row["volume"], 2),
        "trade_count": row["trade_count"],
        "traders": traders,
        "liquidity": row["liquidity"],
        "creator": creator,
        "auto": row["created_by"] is None,
        "start_stars": row["start_stars"],
        "start_stars_b": row["start_stars_b"],
        "stars_now": stars_now[0] if stars_now else (repo["stars"] if repo else None),
        "stars_now_b": stars_b[0] if stars_b else None,
        "avatar_url": (repo["avatar_url"] if repo and repo["avatar_url"] else f"https://github.com/{owner}.png?size=80"),
        "language": repo["language"] if repo else None,
        "resolved_ts": row["resolved_ts"],
        "resolution_note": row["resolution_note"],
    }
    if row["kind"] == "trending_days":
        start = dt.date.fromisoformat(row["window_start_day"])
        summary["trending_days_so_far"] = len(trending_days_logged(conn, _names_for(conn, row), start, row["window_days"]))
    if extra:
        summary.update(extra)
    return summary


SORTS = {
    "volume": "volume DESC, id DESC",
    "closing": "close_ts ASC, id DESC",
    "new": "open_ts DESC, id DESC",
    "resolved": "resolved_ts DESC, id DESC",
}


def list_markets(conn, now: Optional[float] = None, status: str = "open", kind: Optional[str] = None,
                 repo: Optional[str] = None, user_id: Optional[int] = None, mine: bool = False,
                 sort: str = "volume", limit: int = 100, query: Optional[str] = None) -> dict:
    now = now or time.time()
    where, params = [], []
    if status == "open":
        where.append("status = 'open' AND close_ts > ?")
        params.append(now)
    elif status == "closing":
        where.append("status = 'open' AND close_ts > ? AND close_ts <= ?")
        params += [now, now + 3 * DAY]
    elif status == "settled":
        where.append("status IN ('resolved', 'void')")
    elif status == "pending":
        where.append("(status = 'review' OR (status = 'open' AND close_ts <= ?))")
        params.append(now)
    elif status != "all":
        raise MarketError("status must be open, closing, pending, settled or all.")
    if kind:
        if kind not in KINDS:
            raise MarketError("unknown kind")
        where.append("kind = ?")
        params.append(kind)
    if repo:
        where.append("(repo = ? OR repo_b = ?)")
        params += [repo, repo]
    if query:
        where.append("(question LIKE ? ESCAPE '\\')")
        params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if mine:
        if user_id is None:
            return {"items": [], "count": 0}
        where.append("(id IN (SELECT market_id FROM positions WHERE user_id = ?) OR created_by = ?)")
        params += [user_id, user_id]
    order = SORTS.get(sort)
    if order is None:
        raise MarketError("sort must be volume, closing, new or resolved.")
    if status == "settled" and sort == "volume":
        order = SORTS["resolved"]
    sql = "SELECT * FROM markets" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT ?"
    rows = conn.execute(sql, (*params, max(1, min(int(limit), 500)))).fetchall()
    items = [market_summary(conn, row, now) for row in rows]
    if user_id is not None:
        positions = {
            row["market_id"]: position_dict(row)
            for row in conn.execute("SELECT * FROM positions WHERE user_id = ?", (user_id,))
        }
        for item in items:
            item["my_position"] = positions.get(item["id"])
    return {"items": items, "count": len(items)}


def market_detail(conn, market_id: int, now: Optional[float] = None, user_id: Optional[int] = None) -> dict:
    now = now or time.time()
    row = market_row(conn, market_id)
    detail = market_summary(conn, row, now)
    history = [[round(row["open_ts"] * 1000), row["initial_prob"]]]
    for trade in conn.execute("SELECT ts, prob_after FROM trades WHERE market_id = ? ORDER BY ts, id", (market_id,)):
        history.append([round(trade["ts"] * 1000), round(trade["prob_after"], 4)])
    end = row["resolved_ts"] or min(now, row["close_ts"])
    history.append([round(end * 1000), detail["prob"] if row["status"] != "resolved" else (1.0 if row["outcome"] == "yes" else 0.0)])

    def star_series(repo_id):
        if repo_id is None:
            return []
        points = conn.execute(
            "SELECT ts, stars FROM radar.snapshots WHERE repo_id = ? AND ts >= ? ORDER BY ts",
            (repo_id, row["open_ts"] - 14 * DAY),
        ).fetchall()
        return [[round(p["ts"] * 1000), p["stars"]] for p in points]

    trades = conn.execute(
        """SELECT t.ts, t.side, t.action, t.shares, t.points, t.prob_after, u.login, u.avatar_url
           FROM trades t JOIN users u ON u.id = t.user_id WHERE t.market_id = ? ORDER BY t.ts DESC, t.id DESC LIMIT 30""",
        (market_id,),
    ).fetchall()
    holders = {}
    for side in SIDES:
        holders[side] = [
            {"login": h["login"], "avatar_url": h["avatar_url"], "shares": round(h["shares"], 2)}
            for h in conn.execute(
                f"""SELECT u.login, u.avatar_url, p.{side}_shares AS shares FROM positions p
                    JOIN users u ON u.id = p.user_id WHERE p.market_id = ? AND p.{side}_shares > 0.01
                    ORDER BY p.{side}_shares DESC LIMIT 5""",
                (market_id,),
            )
        ]
    my_position = None
    stake_room = None
    if user_id is not None:
        position = conn.execute(
            "SELECT * FROM positions WHERE user_id = ? AND market_id = ?", (user_id, market_id)
        ).fetchone()
        my_position = position_dict(position)
        at_risk = (position["spent"] - position["received"]) if position else 0.0
        stake_room = round(max(MAX_STAKE_PER_MARKET - at_risk, 0.0), 2)
        if my_position:
            my_position["value"] = round(position_value(row, position), 4)
    rate_repo = repo_state(conn, repo_id=row["repo_id"], now=now)
    trending_grid = None
    if row["kind"] == "trending_days":
        start = dt.date.fromisoformat(row["window_start_day"])
        logged = trending_days_logged(conn, _names_for(conn, row), start, row["window_days"])
        covered = trending_coverage(conn, start, row["window_days"])
        trending_grid = []
        for offset in range(row["window_days"]):
            day = (start + dt.timedelta(days=offset)).isoformat()
            state = "hit" if day in logged else ("miss" if day in covered else ("future" if day > utc_day(now).isoformat() else "nodata"))
            trending_grid.append({"day": day, "state": state})
    detail.update(
        history=history,
        stars=star_series(row["repo_id"]),
        stars_b=star_series(row["repo_b_id"]),
        trending_grid=trending_grid,
        trades=[dict(t) for t in trades],
        holders=holders,
        my_position=my_position,
        stake_room=stake_room,
        rules=rules_for(row),
        flag_note=row["flag_note"] if row["status"] == "review" else None,
        rate_per_day=rate_repo["rate_per_day"] if rate_repo else None,
        description=rate_repo["description"] if rate_repo else None,
        max_stake=MAX_STAKE_PER_MARKET,
    )
    return detail


def position_value(market, position) -> float:
    if market["status"] == "resolved":
        return position["payout"]
    if market["status"] == "void":
        return 0.0
    prob = lmsr_prob(market["q_yes"], market["q_no"], market["liquidity"])
    return position["yes_shares"] * prob + position["no_shares"] * (1 - prob)


def _user_stats(conn, user_id: int, now: float) -> dict:
    user = get_user(conn, user_id)
    rows = conn.execute(
        """SELECT p.*, m.status, m.q_yes, m.q_no, m.liquidity, m.resolved_ts, m.outcome
           FROM positions p JOIN markets m ON m.id = p.market_id WHERE p.user_id = ?""",
        (user_id,),
    ).fetchall()
    open_value = 0.0
    settled = correct = 0
    realized_30d = 0.0
    for row in rows:
        if row["status"] in ("open", "review"):
            open_value += position_value(row, row)
        elif row["status"] == "resolved":
            pnl = row["payout"] + row["received"] - row["spent"]
            if row["spent"] > EPS:
                settled += 1
                correct += pnl > 0.005
            if row["resolved_ts"] and row["resolved_ts"] >= now - 30 * DAY:
                realized_30d += pnl
    placeholders = ",".join("?" for _ in GRANT_KINDS)
    grants = conn.execute(
        f"SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE user_id = ? AND kind IN ({placeholders})",
        (user_id, *GRANT_KINDS),
    ).fetchone()[0]
    fees = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE user_id = ? AND kind = 'create_fee'", (user_id,)
    ).fetchone()[0]
    trades = conn.execute("SELECT COUNT(*) FROM trades WHERE user_id = ?", (user_id,)).fetchone()[0]
    return {
        "login": user["login"],
        "avatar_url": user["avatar_url"],
        "balance": round(user["balance"], 2),
        "open_value": round(open_value, 2),
        "net_worth": round(user["balance"] + open_value, 2),
        "profit": round(user["balance"] + open_value - grants - fees, 2),
        "profit_30d": round(realized_30d, 2),
        "settled": settled,
        "accuracy": round(correct / settled, 3) if settled else None,
        "markets": len(rows),
        "trades": trades,
    }


def best_calls(conn, user_id: Optional[int] = None, limit: int = 5) -> list:
    sql = """SELECT t.ts, t.side, t.points, t.shares, t.prob_before, m.id AS market_id, m.question, u.login, u.avatar_url,
                    CASE WHEN t.side = 'yes' THEN t.prob_before ELSE 1 - t.prob_before END AS price
             FROM trades t JOIN markets m ON m.id = t.market_id JOIN users u ON u.id = t.user_id
             WHERE t.action = 'buy' AND m.status = 'resolved' AND t.side = m.outcome
               AND (CASE WHEN t.side = 'yes' THEN t.prob_before ELSE 1 - t.prob_before END) <= 0.3"""
    params = []
    if user_id is not None:
        sql += " AND t.user_id = ?"
        params.append(user_id)
    sql += " ORDER BY price ASC, t.points DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def leaderboard(conn, now: Optional[float] = None, period: str = "all", limit: int = 50) -> dict:
    now = now or time.time()
    if period not in ("all", "30d"):
        raise MarketError("period must be all or 30d.")
    user_ids = [row[0] for row in conn.execute(
        "SELECT DISTINCT t.user_id FROM trades t JOIN users u ON u.id = t.user_id WHERE u.is_banned = 0")]
    rows = [_user_stats(conn, user_id, now) for user_id in user_ids]
    key = "profit" if period == "all" else "profit_30d"
    rows.sort(key=lambda row: (row[key], row["trades"]), reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return {"period": period, "items": rows[:limit], "best_calls": best_calls(conn, None, 8)}


def portfolio(conn, user_id: int, now: Optional[float] = None) -> dict:
    now = now or time.time()
    stats = _user_stats(conn, user_id, now)
    positions = []
    for row in conn.execute(
        """SELECT p.*, m.* FROM positions p JOIN markets m ON m.id = p.market_id
           WHERE p.user_id = ? ORDER BY m.status = 'open' DESC, m.close_ts""",
        (user_id,),
    ):
        value = position_value(row, row)
        positions.append({
            "market": market_summary(conn, conn.execute("SELECT * FROM markets WHERE id = ?", (row["market_id"],)).fetchone(), now),
            "position": position_dict(row),
            "value": round(value, 2),
            "pnl": round(value + row["received"] - row["spent"], 2) if row["status"] != "void" else 0.0,
        })
    ledger = [dict(row) for row in conn.execute(
        """SELECT l.ts, l.delta, l.kind, l.market_id, l.note, m.question FROM ledger l
           LEFT JOIN markets m ON m.id = l.market_id WHERE l.user_id = ? ORDER BY l.id DESC LIMIT 60""",
        (user_id,),
    )]
    return {"stats": stats, "positions": positions, "ledger": ledger}


def profile(conn, login: str, now: Optional[float] = None) -> dict:
    now = now or time.time()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    if user is None:
        raise MarketError("No such user.", 404)
    stats = _user_stats(conn, user["id"], now)
    open_positions = []
    for row in conn.execute(
        """SELECT p.*, m.* FROM positions p JOIN markets m ON m.id = p.market_id
           WHERE p.user_id = ? AND m.status IN ('open', 'review') ORDER BY m.close_ts LIMIT 20""",
        (user["id"],),
    ):
        side = "yes" if row["yes_shares"] >= row["no_shares"] else "no"
        open_positions.append({
            "market_id": row["market_id"],
            "question": row["question"],
            "side": side,
            "shares": round(max(row["yes_shares"], row["no_shares"]), 2),
            "prob": round(lmsr_prob(row["q_yes"], row["q_no"], row["liquidity"]), 4),
        })
    created = [market_summary(conn, row, now) for row in conn.execute(
        "SELECT * FROM markets WHERE created_by = ? ORDER BY open_ts DESC LIMIT 10", (user["id"],))]
    return {
        "user": {"login": user["login"], "avatar_url": user["avatar_url"], "name": user["name"], "joined": user["created_ts"]},
        "stats": stats,
        "best_calls": best_calls(conn, user["id"], 10),
        "open_positions": open_positions,
        "created": created,
    }


def review_queue(conn, now: Optional[float] = None) -> list:
    now = now or time.time()
    rows = conn.execute(
        "SELECT * FROM markets WHERE status = 'review' OR (status = 'open' AND close_ts <= ?) ORDER BY close_ts",
        (now - CLOSE_LOOKAHEAD,),
    ).fetchall()
    return [market_summary(conn, row, now, {"flag_note": row["flag_note"]}) for row in rows]


def odds_by_repo(conn, now: Optional[float] = None) -> dict:
    """Compact map full_name(lower) -> open calls, for odds badges on radar cards."""
    now = now or time.time()
    result = {}
    for row in conn.execute(
        "SELECT * FROM markets WHERE status = 'open' AND close_ts > ? ORDER BY volume DESC", (now,)
    ):
        entry = {
            "id": row["id"],
            "kind": row["kind"],
            "target": row["target"],
            "prob": round(lmsr_prob(row["q_yes"], row["q_no"], row["liquidity"]), 3),
            "question": row["question"],
        }
        for name in filter(None, (row["repo"], row["repo_b"])):
            result.setdefault(name.lower(), []).append(entry)
    return result


def ledger_consistent(conn) -> bool:
    bad = conn.execute(
        """SELECT u.id FROM users u LEFT JOIN (SELECT user_id, SUM(delta) AS total FROM ledger GROUP BY user_id) l
           ON l.user_id = u.id WHERE ABS(u.balance - COALESCE(l.total, 0)) > 1e-4"""
    ).fetchall()
    return not bad
