"""Export RepoRadar's SQLite history as a static site for GitHub Pages.

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


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")


def export(out_dir: Path) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True)
    shutil.copy(BASE_DIR / "index.html", out_dir / "index.html")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    conn = connect()
    try:
        for window in WINDOWS:
            write_json(data_dir / f"movers-{window}.json", query_movers(conn, window, None, 0, 1000))

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

        repo_ids = [row[0] for row in conn.execute("SELECT id FROM repos")]
        for repo_id in repo_ids:
            write_json(data_dir / "history" / f"{repo_id}.json", query_history(conn, repo_id))

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
