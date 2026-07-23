import math
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = BASE_DIR / "almighty.db"
HARNESS_DIR = BASE_DIR / "harness"
# 서비스 전용 영구 clone 루트. 리뷰는 사용자의 라이브 체크아웃이 아니라 여기 clone에서
# worktree를 뜬다(사용자 작업 경로가 실시간으로 바뀌어도 리뷰가 영향받지 않게).
CLONE_DIR = BASE_DIR / ".clones"
# 벤더 원문 stdout 보존 루트(vendor_result.raw_path). 파싱 실패 진단·감사용.
RAW_DIR = BASE_DIR / ".raw"


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool01(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip()
    if raw not in {"0", "1"}:
        raise RuntimeError(f"{name} must be 0 or 1, got {raw!r}")
    return raw == "1"


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"{name} must be finite, got {raw!r}")
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_optional_sha256(name: str) -> str:
    value = os.environ.get(name, "").strip().lower()
    if value and (
        len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise RuntimeError(f"{name} must be a SHA-256 hex digest")
    return value


# §10 추천 기본값
DEFAULT_EFFORT = "medium"
# 폴러가 한 번에 조회하는 열린 PR 상한. 이 값 미만이 반환되면 완전한 오픈 셋으로
# 간주해 사라진 PR을 closed로 재조정한다(상한에 걸리면 오검-close 방지로 재조정 skip).
POLL_OPEN_PR_LIMIT = 200
# UI/API뿐 아니라 poll loop에서도 방어하는 간격 범위. 너무 작은 값은 GitHub 요청 폭주,
# 지나치게 큰 값은 폴러가 사실상 영구 정지하는 운영 장애가 된다.
POLL_INTERVAL_MIN_SEC = 15
POLL_INTERVAL_MAX_SEC = 86_400
CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 8
DEFAULT_REVIEW_MODEL = "sonnet"
DEFAULT_PRESCREEN_MODEL = "haiku"
DEFAULT_CODEX_MODEL = ""  # "" = codex CLI 자체 기본 모델

# 설정 UI가 GET /api/models로 주입받는 선택 가능한 모델·effort "제안" 목록.
# UI는 콤보박스(제안 + 자유 입력)라 여기 없는 정확한 모델 ID도 직접 타이핑할 수 있다.
# 별칭(opus/sonnet/…)은 최신 모델로 자동 매핑되고, 풀네임은 특정 버전에 고정된다.
CLAUDE_MODELS = [
    "opus",
    "sonnet",
    "haiku",
    "fable",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
]
# codex는 gpt-5.6 계열의 변형(sol/terra/luna)이 실제 모델 ID다. 변형 없는 "gpt-5.6"은
# codex가 fallback 메타데이터로 처리해 성능이 저하되므로 제안 목록에서 제외한다.
CODEX_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]

# 포스팅 코멘트 최상단 배너(env-only). 설정 시 게시 코멘트 맨 위에 붙는다(테스트/스테이징
# 표식용). 미설정("")이면 배너 없음(기본 동작 불변).
POST_BANNER = os.environ.get("ALMIGHTY_POST_BANNER", "")

# 리뷰 종료 macOS 알림(osascript). 로컬 단일 사용자 도구라 기본 켜짐 — "0"으로 끔.
NOTIFY_ON_DONE = os.environ.get("ALMIGHTY_NOTIFY", "1") != "0"

# 공개 터널/프록시에서 관리 API를 보호하는 env-only bearer token. 빈 값은 기존
# loopback-only 개발 동작을 보존한다.
ADMIN_TOKEN = os.environ.get("ALMIGHTY_ADMIN_TOKEN", "")
WEBHOOK_MAX_BODY_BYTES = _env_int(
    "ALMIGHTY_WEBHOOK_MAX_BODY_BYTES", 1_048_576, minimum=1
)
ADMIN_ALLOWED_ORIGINS = tuple(
    origin.strip()
    for origin in os.environ.get(
        "ALMIGHTY_ADMIN_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:8787,http://127.0.0.1:8787",
    ).split(",")
    if origin.strip()
)

# gh subprocess 상한 — 무한 대기 시 폴러/워커가 조용히 영구 정지하므로 필수.
# clone(depth=1)·대형 diff도 감당할 만큼 여유 있게 잡는다.
GH_TIMEOUT_SEC = 300
# 한 job 전체 wall-clock 상한. 청크별 vendor timeout이 누적돼 lane을 무기한 점유하지 않게 한다.
JOB_TIMEOUT_SEC = _env_int("ALMIGHTY_JOB_TIMEOUT_SEC", 1800, minimum=1)
# 레포 전체를 탐색하는 Ground Truth 생성은 일반 리뷰의 벤더 상한(10분)보다 오래 걸린다.
WIKI_VENDOR_TIMEOUT_SEC = _env_int(
    "ALMIGHTY_WIKI_VENDOR_TIMEOUT_SEC", 1800, minimum=1
)
WORKER_IDLE_MAX_SEC = _env_float("ALMIGHTY_WORKER_IDLE_MAX_SEC", 30, minimum=0.1)
BACKGROUND_SHUTDOWN_GRACE_SEC = _env_float(
    "ALMIGHTY_BACKGROUND_SHUTDOWN_GRACE_SEC", 10, minimum=0
)
if BACKGROUND_SHUTDOWN_GRACE_SEC > 30:
    raise RuntimeError(
        "ALMIGHTY_BACKGROUND_SHUTDOWN_GRACE_SEC must be <= 30"
    )
BACKGROUND_CLEANUP_TIMEOUT_SEC = _env_float(
    "ALMIGHTY_BACKGROUND_CLEANUP_TIMEOUT_SEC", 20, minimum=0.1
)
if BACKGROUND_SHUTDOWN_GRACE_SEC + BACKGROUND_CLEANUP_TIMEOUT_SEC > 50:
    raise RuntimeError(
        "background shutdown grace plus cleanup timeout must be <= 50"
    )
# 0이면 PR 이력 보존 정책 비활성. raw 진단은 별도 안전 TTL로 항상 관리한다.
RETENTION_DAYS = _env_int("ALMIGHTY_RETENTION_DAYS", 0, minimum=0)
DIAGNOSTIC_RETENTION_DAYS = _env_int(
    "ALMIGHTY_DIAGNOSTIC_RETENTION_DAYS", 7, minimum=1
)
CONTEXT_PAYLOAD_RETENTION_DAYS = _env_int(
    "ALMIGHTY_CONTEXT_PAYLOAD_RETENTION_DAYS", 7, minimum=1
)
# Raw/context cleanup performs irreversible local deletes and is therefore opt-in.
DIAGNOSTIC_CLEANUP_ENABLED = _env_bool01(
    "ALMIGHTY_DIAGNOSTIC_CLEANUP_ENABLED"
)
REVIEW_SCOPE_GUARD_MODE = os.environ.get(
    "ALMIGHTY_REVIEW_SCOPE_GUARD_MODE", "observe"
).strip().lower()
REVIEW_DEDUPE_MODE = os.environ.get(
    "ALMIGHTY_REVIEW_DEDUPE_MODE", "observe"
).strip().lower()
REVIEW_SCOPE_ENFORCE_REPOS = frozenset(
    item.strip()
    for item in os.environ.get("ALMIGHTY_REVIEW_SCOPE_ENFORCE_REPOS", "").split(",")
    if item.strip()
)
REVIEW_DEDUPE_ENFORCE_REPOS = frozenset(
    item.strip()
    for item in os.environ.get("ALMIGHTY_REVIEW_DEDUPE_ENFORCE_REPOS", "").split(",")
    if item.strip()
)
REVIEW_POLICY_ENFORCEMENT_UNLOCKED = _env_bool01(
    "ALMIGHTY_REVIEW_POLICY_ENFORCEMENT_UNLOCKED"
)
REVIEW_BENCHMARK_ATTESTATION_HASH = _env_optional_sha256(
    "ALMIGHTY_REVIEW_BENCHMARK_ATTESTATION_HASH"
)
REVIEW_SCOPE_KILL_SWITCH = _env_bool01("ALMIGHTY_REVIEW_SCOPE_KILL_SWITCH")
REVIEW_DEDUPE_KILL_SWITCH = _env_bool01("ALMIGHTY_REVIEW_DEDUPE_KILL_SWITCH")
REVIEW_SNAPSHOT_TIMEOUT_SEC = _env_int(
    "ALMIGHTY_REVIEW_SNAPSHOT_TIMEOUT_SEC", 300, minimum=1
)
REVIEW_SNAPSHOT_MAX_ARCHIVE_BYTES = _env_int(
    "ALMIGHTY_REVIEW_SNAPSHOT_MAX_ARCHIVE_BYTES", 1_073_741_824, minimum=1
)
REVIEW_SNAPSHOT_MAX_TOTAL_BYTES = _env_int(
    "ALMIGHTY_REVIEW_SNAPSHOT_MAX_TOTAL_BYTES", 2_147_483_648, minimum=1
)
REVIEW_SNAPSHOT_MAX_FILE_BYTES = _env_int(
    "ALMIGHTY_REVIEW_SNAPSHOT_MAX_FILE_BYTES", 268_435_456, minimum=1
)
REVIEW_SNAPSHOT_MAX_FILES = _env_int(
    "ALMIGHTY_REVIEW_SNAPSHOT_MAX_FILES", 200_000, minimum=1
)
if REVIEW_SCOPE_GUARD_MODE not in {"observe", "enforce"}:
    raise RuntimeError("ALMIGHTY_REVIEW_SCOPE_GUARD_MODE must be observe or enforce")
if REVIEW_DEDUPE_MODE not in {"observe", "enforce"}:
    raise RuntimeError("ALMIGHTY_REVIEW_DEDUPE_MODE must be observe or enforce")

# v2 서브프로젝트 B — 외부 컨텍스트 주입
MAX_CONTEXT_CHARS_PER_SOURCE = 8_000
MAX_CONTEXT_CHARS_TOTAL = 20_000
CONTEXT_GATHER_TIMEOUT_SEC = 15
# 프로바이더 자격증명은 env-only (sqlite 금지). 미설정이면 "" → 해당 프로바이더 자동 비활성.
JIRA_BASE_URL = os.environ.get("ALMIGHTY_JIRA_BASE_URL", "")
JIRA_EMAIL = os.environ.get("ALMIGHTY_JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("ALMIGHTY_JIRA_API_TOKEN", "")
JIRA_ACCEPTANCE_CRITERIA_FIELD = os.environ.get(
    "ALMIGHTY_JIRA_ACCEPTANCE_CRITERIA_FIELD", ""
)

# Resolver-backed MSSQL metadata introspection. Safe-DB의 SQL Gateway read guard를 레포 내부에
# 필요한 부분만 이식했다. Gateway URL/token은 env-only, DB 주소·자격증명은 Gateway 밖으로 나오지 않는다.
MSSQL_GATEWAY_URL = os.environ.get("ALMIGHTY_MSSQL_GATEWAY_URL", "")
MSSQL_GATEWAY_TOKEN = os.environ.get("ALMIGHTY_MSSQL_GATEWAY_TOKEN", "")
MSSQL_GATEWAY_TARGET_FIELD = os.environ.get(
    "ALMIGHTY_MSSQL_GATEWAY_TARGET_FIELD", "hospitalId"
)
MSSQL_GATEWAY_LOCK_PATH = BASE_DIR / ".safe-db-locks" / "mssql-gateway.lock"
MSSQL_GATEWAY_AUDIT_PATH = BASE_DIR / ".safe-db-audit.jsonl"

# GitHub 웹훅 공유 시크릿(env-only). 미설정이면 "" → 웹훅 수신 자체를 거부(503).
GITHUB_WEBHOOK_SECRET = os.environ.get("ALMIGHTY_GITHUB_WEBHOOK_SECRET", "")

# 서브프로젝트 C — Slack 반응 루프(env-only). 게시한 리뷰에 달린 👍/👎를 학습 신호로 수집.
# BOT_TOKEN/CHANNEL 미설정이면 게시 자동 비활성, SIGNING_SECRET 미설정이면 반응 웹훅 거부(503).
SLACK_BOT_TOKEN = os.environ.get("ALMIGHTY_SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("ALMIGHTY_SLACK_SIGNING_SECRET", "")
SLACK_CHANNEL = os.environ.get("ALMIGHTY_SLACK_CHANNEL", "")
