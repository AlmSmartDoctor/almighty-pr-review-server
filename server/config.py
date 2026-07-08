from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "almighty.db"
HARNESS_DIR = BASE_DIR / "harness"

# §10 추천 기본값
DEFAULT_EFFORT = "medium"
DEFAULT_CONCURRENCY = 2
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_PRESCREEN_MODEL = "claude-haiku"
DEFAULT_PRESCREEN_THRESHOLD = "moderate"  # trivial 미만이면 skip 후보
