"""EarlyStar authentication: GitHub OAuth (no scopes), dev login, cookie sessions.

GitHub OAuth app setup (https://github.com/settings/developers → New OAuth App):
    Homepage URL:            https://your-domain
    Authorization callback:  https://your-domain/auth/callback
then set EARLYSTAR_GITHUB_CLIENT_ID, EARLYSTAR_GITHUB_CLIENT_SECRET and EARLYSTAR_BASE_URL.

The GitHub access token is used once to read the public profile and then discarded.
Only a SHA-256 hash of each session token is stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

import markets

SESSION_COOKIE = "sc_session"
STATE_COOKIE = "sc_oauth"
SESSION_DAYS = 30
DEV_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
USER_AGENT = "EarlyStar/1.0 (auth)"


@dataclass
class AuthConfig:
    client_id: str = ""
    client_secret: str = ""
    base_url: str = ""
    dev_login: bool = False
    admins: set = field(default_factory=set)

    @property
    def github_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret and self.base_url)

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    def is_admin(self, login: Optional[str]) -> bool:
        return bool(login) and login.lower() in self.admins

    @classmethod
    def from_env(cls, dev_login: bool = False) -> "AuthConfig":
        admins = {name.strip().lower() for name in os.environ.get("EARLYSTAR_ADMINS", "").split(",") if name.strip()}
        return cls(
            client_id=os.environ.get("EARLYSTAR_GITHUB_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("EARLYSTAR_GITHUB_CLIENT_SECRET", "").strip(),
            base_url=os.environ.get("EARLYSTAR_BASE_URL", "").strip().rstrip("/"),
            dev_login=dev_login,
            admins=admins,
        )


# --------------------------------------------------------------------------- cookies

def parse_cookies(header: Optional[str]) -> dict:
    cookies = {}
    for part in (header or "").split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def cookie_header(name: str, value: str, max_age: int, secure: bool, path: str = "/") -> str:
    parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# --------------------------------------------------------------------------- sessions

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id: int, now: Optional[float] = None) -> str:
    now = now or time.time()
    token = secrets.token_urlsafe(32)
    with markets.transaction(conn):
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_ts, expires_ts) VALUES (?, ?, ?, ?)",
            (_hash(token), user_id, now, now + SESSION_DAYS * 86400),
        )
        conn.execute("DELETE FROM sessions WHERE expires_ts < ?", (now,))
    return token


def user_for_token(conn, token: Optional[str], now: Optional[float] = None):
    if not token or len(token) > 200:
        return None
    now = now or time.time()
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_ts > ?""",
        (_hash(token), now),
    ).fetchone()
    return row


def delete_session(conn, token: Optional[str]) -> None:
    if token:
        with markets.transaction(conn):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


# --------------------------------------------------------------------------- GitHub OAuth

def authorize_url(config: AuthConfig, state: str) -> str:
    params = urllib.parse.urlencode({
        "client_id": config.client_id,
        "redirect_uri": config.base_url + "/auth/callback",
        "state": state,
        "allow_signup": "true",
    })
    return "https://github.com/login/oauth/authorize?" + params


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def github_login(conn, config: AuthConfig, code: str, now: Optional[float] = None):
    """Exchange an OAuth code for the user's public profile and upsert the EarlyStar user."""
    if not code or len(code) > 200:
        raise markets.MarketError("Missing OAuth code.", 400)
    token_response = _post_json(
        "https://github.com/login/oauth/access_token",
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "redirect_uri": config.base_url + "/auth/callback",
        },
    )
    access_token = token_response.get("access_token")
    if not access_token:
        raise markets.MarketError("GitHub sign-in failed: " + str(token_response.get("error_description") or "no token"), 400)
    profile = _get_json("https://api.github.com/user", access_token)
    return markets.upsert_user(
        conn,
        github_id=int(profile["id"]),
        login=profile["login"],
        name=profile.get("name"),
        avatar_url=profile.get("avatar_url"),
        github_created_at=profile.get("created_at"),
        now=now,
    )


def dev_login(conn, login: str, now: Optional[float] = None):
    """Local testing only (server started with --dev-login): sign in as any name."""
    login = (login or "").strip()
    if not DEV_LOGIN_RE.fullmatch(login):
        raise markets.MarketError("Use a GitHub-style username (letters, digits, dashes).", 400)
    return markets.upsert_user(
        conn,
        github_id=None,
        login=login,
        name=login,
        avatar_url=f"https://github.com/{login}.png?size=80",
        is_dev=True,
        now=now,
    )


def new_state() -> str:
    return secrets.token_urlsafe(24)


def safe_next(value: Optional[str]) -> str:
    """Only allow same-site relative redirects after login."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:300]
