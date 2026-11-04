"""EarlyStar tests. Run with:  python -m unittest discover -s tests -t . -v"""

import os
import tempfile

# Point the default data directory at a throwaway folder before collector/markets are imported,
# so tests can never touch the real data/radar.db or data/earlystar.db.
os.environ.setdefault("EARLYSTAR_DATA_DIR", tempfile.mkdtemp(prefix="earlystar-tests-"))
