import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "almighty.db"
HARNESS_DIR = BASE_DIR / "harness"

# §10 추천 기본값
DEFAULT_EFFORT = "medium"
DEFAULT_CONCURRENCY = 2
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_PRESCREEN_MODEL = "haiku"
DEFAULT_REVIEW_MODEL = "sonnet"
DEFAULT_CODEX_MODEL = ""  # "" = codex CLI 자체 기본 모델
DEFAULT_PRESCREEN_THRESHOLD = "moderate"  # trivial 미만이면 skip 후보

# v2 서브프로젝트 B — 외부 컨텍스트 주입
MAX_CONTEXT_CHARS_PER_SOURCE = 8_000
MAX_CONTEXT_CHARS_TOTAL = 20_000
CONTEXT_GATHER_TIMEOUT_SEC = 15
# 프로바이더 자격증명은 env-only (sqlite 금지). 미설정이면 "" → 해당 프로바이더 자동 비활성.
JIRA_BASE_URL = os.environ.get("ALMIGHTY_JIRA_BASE_URL", "")
JIRA_EMAIL = os.environ.get("ALMIGHTY_JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("ALMIGHTY_JIRA_API_TOKEN", "")
