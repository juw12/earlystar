#!/usr/bin/env sh
# EarlyStar — local server with dev sign-in (collects every 30 min) and opens the app.
# Optional: export GITHUB_TOKEN=ghp_xxx for higher GitHub rate limits; EARLYSTAR_ADMINS=yourlogin for admin tools.
cd "$(dirname "$0")" || exit 1
exec python3 server.py --dev-login --open "$@"
