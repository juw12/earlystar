"""Export EarlyStar's SQLite history as a static site for GitHub Pages.

    python export_static.py site

Writes site/index.html plus site/data/*.json, which index.html reads when it is
hosted without server.py (movers per window, a Trending archive per day, star
history per repo and a health summary).
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from collector import BASE_DIR, connect
from server import WINDOWS, query_health, query_history, query_movers, query_trending

TRENDING_DAYS_KEPT = 60
BADGE_TOP_RANK = 100  # repos in the top 100 of 24h movers get a rank badge


def compact(number: int) -> str:
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(number)


def badge_payload(stars: int, rank_24h: int | None, gain_24h: int | None, gain_7d: int | None) -> dict:
    """shields.io endpoint format: https://shields.io/badges/endpoint-badge"""
    if rank_24h is not None and rank_24h <= BADGE_TOP_RANK and gain_24h and gain_24h > 0:
        message, color = f"#{rank_24h} today · +{compact(gain_24h)}★", "4f46e5"
    elif gain_7d and gain_7d > 0:
        message, color = f"+{compact(gain_7d)}★ this week", "6366f1"
    else:
        message, color = f"{compact(stars)}★ tracked", "64748b"
    return {"schemaVersion": 1, "label": "EarlyStar", "message": message, "color": color, "cacheSeconds": 1800}


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")


def export(out_dir: Path) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True)
    index = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    index = index.replace('<meta name="earlystar-host" content="">', '<meta name="earlystar-host" content="static">', 1)
    (out_dir / "index.html").write_text(index, encoding="utf-8")
    # Installable web app (PWA): manifest, service worker and icons.
    for name in ("manifest.webmanifest", "sw.js"):
        shutil.copy(BASE_DIR / name, out_dir / name)
    shutil.copytree(BASE_DIR / "icons", out_dir / "icons")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    conn = connect()
    try:
        movers = {}
        for window in WINDOWS:
            movers[window] = query_movers(conn, window, None, 0, 1000)
            write_json(data_dir / f"movers-{window}.json", movers[window])

        days = [row[0] for row in conn.execute(
            "SELECT DISTINCT day FROM trending ORDER BY day DESC LIMIT ?", (TRENDING_DAYS_KEPT,))]
        combos = conn.execute("SELECT DISTINCT since, language FROM trending").fetchall()
        for day in days:
            lists = {}
            for since, language in combos:
                result = query_trending(conn, since, day, language)
                if result["items"]:
                    lists[f"{since}|{language}"] = result["items"]
            write_json(data_dir / "trending" / f"{day}.json", {"day": day, "lists": lists})
        write_json(data_dir / "trending" / "days.json", {
            "days": days,
            "languages": sorted({language for _, language in combos}),
        })

        repo_rows = conn.execute("SELECT id, full_name, stars FROM repos").fetchall()
        repo_ids = [row[0] for row in repo_rows]
        for repo_id in repo_ids:
            write_json(data_dir / "history" / f"{repo_id}.json", query_history(conn, repo_id))

        # Public lookup index and README badges (shields.io endpoint JSON) for every tracked repo.
        write_json(data_dir / "repos.json", {full_name: repo_id for repo_id, full_name, _ in repo_rows})
        rank_24h = {item["id"]: index + 1 for index, item in enumerate(movers["24h"]["items"])}
        gain_24h = {item["id"]: item["gain"] for item in movers["24h"]["items"]}
        gain_7d = {item["id"]: item["gain"] for item in movers["7d"]["items"]}
        for repo_id, full_name, stars in repo_rows:
            payload = badge_payload(stars or 0, rank_24h.get(repo_id), gain_24h.get(repo_id), gain_7d.get(repo_id))
            write_json(data_dir / "badge" / f"{full_name}.json", payload)

        last_run = conn.execute("SELECT ts, duration FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
        last_finished = (last_run[0] + (last_run[1] or 0)) if last_run else None
        static_collector = SimpleNamespace(state={"enabled": False, "running": False, "last_finished": last_finished})
        health = query_health(conn, static_collector)
        health.update(token=False, static=True, generated_at=int(time.time()))
        write_json(data_dir / "health.json", health)
    finally:
        conn.close()
    return {"days": len(days), "repos": len(repo_ids)}


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "site").resolve()
    summary = export(target)
    print(f"Exported static site to {target}: {summary['repos']} repo histories, {summary['days']} trending days")
