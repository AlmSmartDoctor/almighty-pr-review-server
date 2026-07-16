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

# §10 추천 기본값
DEFAULT_EFFORT = "medium"
# 폴러가 한 번에 조회하는 열린 PR 상한. 이 값 미만이 반환되면 완전한 오픈 셋으로
# 간주해 사라진 PR을 closed로 재조정한다(상한에 걸리면 오검-close 방지로 재조정 skip).
POLL_OPEN_PR_LIMIT = 200
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

# gh subprocess 상한 — 무한 대기 시 폴러/워커가 조용히 영구 정지하므로 필수.
# clone(depth=1)·대형 diff도 감당할 만큼 여유 있게 잡는다.
GH_TIMEOUT_SEC = 300

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

# GitHub 웹훅 공유 시크릿(env-only). 미설정이면 "" → 웹훅 수신 자체를 거부(503).
GITHUB_WEBHOOK_SECRET = os.environ.get("ALMIGHTY_GITHUB_WEBHOOK_SECRET", "")

# 서브프로젝트 C — Slack 반응 루프(env-only). 게시한 리뷰에 달린 👍/👎를 학습 신호로 수집.
# BOT_TOKEN/CHANNEL 미설정이면 게시 자동 비활성, SIGNING_SECRET 미설정이면 반응 웹훅 거부(503).
SLACK_BOT_TOKEN = os.environ.get("ALMIGHTY_SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("ALMIGHTY_SLACK_SIGNING_SECRET", "")
SLACK_CHANNEL = os.environ.get("ALMIGHTY_SLACK_CHANNEL", "")
