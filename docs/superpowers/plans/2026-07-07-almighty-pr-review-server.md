# Almighty PR Review Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 회사 레포의 PR을 폴링해 격리된 리뷰 하네스에서 Claude·Codex CLI로 독립 리뷰하고, 로컬 라이트테마 대시보드에서 트리아지 후 승인분만 PR 코멘트로 포스팅하는 로컬 단일사용자 서버(v1)를 빌드한다.

**Architecture:** 접근 1 — CLI 위의 얇은 오케스트레이터. FastAPI 서버는 **API/대시보드만** 담당하고, 장시간 리뷰 잡은 **요청 사이클 밖의 worker 루프**가 SQLite-backed `review_job` 큐를 소비해 실행한다(폴링→사전스크리닝→격리 worktree→멀티벤더 병렬 리뷰(RunnerPool 세마포어)→(옵션)병합→저장→승인 후 gh 포스팅). 확장은 seam(HarnessProfile·ContextProvider·MemoryStore·Identity·RunnerPool·JobQueue)으로만 흡수하고 v1은 no-op/기본값으로 출발.

**Tech Stack:** Python 3.12 · FastAPI · uvicorn · SQLite(stdlib sqlite3, WAL) · pytest · gh CLI · claude/codex CLI(헤드리스, async subprocess) · React 18 + Vite + TypeScript + React Router(얇은 프론트).

**참조 스펙:** `docs/superpowers/specs/2026-07-07-almighty-pr-review-design.md` (확정 결정·데이터모델 §6·파이프라인 §7·대시보드 §8·seam §9). **디자인 초안:** `docs/design-drafts/variant-app.html`.

> **개정 이력 (2026-07-07, codex 교차검증 반영):** 초판 리뷰(GO-with-changes, 확장성 5.5/10)에서 확인된 자기모순·확장성 갭을 반영. 스택 3종(FastAPI·SQLite·Vite/React)은 유지, **배선을 개정**:
> - **잡 실행 분리** — 요청 핸들러 직접 `await` 제거 → `review_job` 큐 + worker 루프 (Milestone 4).
> - **DB 동시성** — 전역 단일 커넥션 폐기 → connection-per-unit + WAL + busy_timeout (Milestone 1·4).
> - **RunnerPool 실배선** — 이름뿐인 seam 제거, 벤더 병렬 `gather`+timeout+실패격리, async subprocess (Milestone 3·4).
> - **벤더 capability spike 선행** — 신규 **Milestone 0.5**로 CLI 실플래그·격리·read-only·JSON강제·timeout을 어댑터 구현 전에 실증.
> - **데이터모델 보강** — `review_job`(스케줄), `repo.local_path`, `posted_comment.head_sha`; 포스팅 update-or-create; env allowlist·worktree cleanup·fake-CLI 통합테스트.
>
> **개정 이력 v2 (2026-07-07, codex 재검증 반영, 5.5→7.0):** 개정본 재검증에서 CRITICAL 4건 RESOLVED 확인 후, 남은 게이트 항목 반영:
> - **잡 큐 동시성** — `claim_next` OperationalError 처리 + 별도 커넥션 동시 claim 테스트(Task 1.3).
> - **stale-lock 복구 + graceful shutdown** — `recover_stale`(worker 기동 시), lifespan asynccontextmanager + `stop_event` gather await(Task 4.5·7.2).
> - **run 상태 정합성** — `review_pr` try/except로 예외 시 run을 failed 마감 후 재던짐(Task 4.2).
> - **prescreen 격리/timeout** — stdin 닫기·timeout·격리 env 공유(Task 3.5·7.2).
> - **포스팅 실중복 제거** — `gh api PATCH`로 기존 코멘트 in-place edit(Task 2.1·5.2).
> - merge `id()` 취약성 제거(`Finding.vendor_result_id` 명시) · codex fake-CLI 테스트 추가.
>
> **개정 이력 v3 (2026-07-07, codex 3차 재검증 반영, 7.0→7.3):** v2 회귀 점검에서 지목된 HIGH 3건 + MEDIUM/LOW 반영:
> - **격리 env ↔ CLI 인증 충돌 해소** — `isolated_env`가 HOME까지 비워 매 리뷰가 로그인 실패할 위험. `AUTH_ENV_KEYS` allowlist + `prepare_runtime()`로 **인증 자격만** 격리 runtime에 심고(rules/skills/MCP는 제외), Milestone 0.5 preflight가 **"인증 성립"과 "전역 미상속"을 같은 preflight에서 동시 실증**(Task 0.5·3.3).
> - **failed run ↔ retry job 정합성** — `review_pr`가 `PipelineError(run_id)`를 던지고 worker가 `mark_failed(..., run_id=)`로 job에 최신 attempt run을 기록. **1 attempt = 1 review_run** 정책 명문화(Task 4.2·4.5·1.2).
> - **동시 claim 가짜 그린 제거** — 순차 호출 테스트를 (a) 결정론적 writer-락 경합 + (b) 스레드 barrier 동시 출발 테스트로 대체(Task 1.3).
> - **worker idle busy-loop 방지** — `stop_event=None`일 때 `asyncio.sleep` 분기(Task 4.5). GitHub comment id를 URL 파싱 대신 API JSON `.id`로 저장(Task 2.1·5.2). merge `consensus_group_id` DB 저장(Task 1.2·4.2).
>
> **개정 이력 v4 (2026-07-07, codex 4차 재검증 반영, 7.3→7.1→목표복구):** v3 회귀 점검에서 **실제 버그 1건(HIGH)** 신규 발견 + 정책 명문화. (점수 -0.2는 더 깊은 정적분석으로 잠복 버그를 잡은 결과):
> - **[HIGH·실버그] 벤더 전원 실패 → run=done 오판 수정** — 실패 격리(`_run_one`가 예외를 `(vendor,[],err)`로 흡수)가 옳지만, claude·codex가 **모두** 실패(rate-limit/auth)해도 `finish_run(done)`으로 마감돼 retry가 안 걸리고 리뷰 성공으로 오인됐다. `succeeded==0`이면 예외 승격 → `review_pr`가 run을 `failed`로 마감 + `PipelineError`로 감싸 worker가 retry. 부분 성공은 done + 실패 벤더는 `vendor_result.status='failed'` 기록(정책). 테스트 `test_pipeline_fails_run_when_all_vendors_fail` 추가(Task 4.2).
> - **[MEDIUM] preflight executable화** — `preflight.sh`가 claude·codex **양쪽**에서 실제 명령으로 ①인증 성립 + ②전역 마커 미상속을 검증(주석→실행)(Task 0.5).
> - **[MEDIUM/LOW] 정책 명문화** — pre-run 실패 시 `job.run_id` 의미(직전 run 유지, attempt 진실원=`review_run` 테이블) · `job.status=done`=스케줄러 완료(리뷰 성공은 `review_run.status`로 판별) · `worker_loop(stop_event=None)`=테스트 전용 무한루프 계약(Task 1.2·4.5).
>
> **개정 이력 v5 (2026-07-07, codex 5차 재검증 반영, 7.1→7.5):** v4 핵심 배선(전원 실패 승격)은 RESOLVED·회귀 없음 확인. 남은 MEDIUM 2 + LOW 2 정리:
> - **[MEDIUM] preflight false-pass 차단** — `grep -qiv "yes"` negative 검증이 prose·멀티라인에 취약 → **sentinel(`OK`/`CLEAN`/`LEAKED`) 정확 일치**로 교체(마지막 유효 라인 정규화 비교)(Task 0.5).
> - **[MEDIUM] 부분 실패 정책 명문화** — 부분 성공=done, 실패 벤더는 `vendor_result.failed`로 남고 **v1은 개별 벤더 자동 재시도 없음**(전원 실패만 retry); 대시보드 노출 + 수동 재리뷰가 escape hatch, 벤더별 follow-up은 v-next(Task 4.2).
> - **[LOW] enabled 벤더 0개 처리** — worktree 만들기 전 `canceled`로 마감(reviewed 오판 방지)(Task 4.2).
> - **[LOW] worker-level 통합 테스트 추가** — `review_pr`가 `PipelineError(run_id)`를 던질 때 worker가 retry job에 실패 run_id를 기록하는지 `test_worker_records_failed_run_id_on_pipeline_error`로 검증(Task 4.5).
>
> **개정 이력 v6 (2026-07-07, codex 6차 재검증 반영, 7.5→7.2→회복):** v5가 유발한 회귀 1건 + 갭 정리:
> - **[MEDIUM·회귀] 벤더 0개 canceled → 재감지 루프** — v5의 canceled 가드가 `mark_reviewed`를 안 해 poller가 매 폴링마다 같은 SHA 재감지. **root-cause 차단: poller가 벤더 0개 레포를 애초에 enqueue하지 않음**(Task 4.4 `poll_once` + 테스트). `_execute_run`의 canceled 가드는 방어적으로 유지.
> - **[MEDIUM] 부분 실패 노출 실체화** — "대시보드 노출" 정책이 문장뿐이었음 → `/api/runs/{id}/vendor-results` API + `review_repo.list_vendor_results` + ReviewSection **실패 벤더 배지**로 실제 구현(Task 1.2·4.6·6.2).
> - **[LOW] preflight false-negative 대비** — one-word 미준수 시 false-fail 가능 → Task 0.5가 CLI별 준수 실증·프롬프트 조정·raw 로그로 확정(Task 0.5). worker 테스트 `build_deps` monkeypatch로 환경 비의존화(Task 4.5).
>
> **개정 이력 v7 (2026-07-07, codex 7차 재검증 반영, 7.2→7.3):** v6가 유발한 회귀 1건 + 문구 정리:
> - **[MEDIUM·회귀] poller guard 위치** — v6의 `continue`가 `list_prs`/`upsert` 앞이라 벤더 0개 레포는 **PR 발견·오버뷰·last_polled_at까지 멈춤**. `has_vendor` 계산 후 upsert·폴링은 항상 수행하고 **enqueue만** 가드하도록 이동. 테스트도 "PR은 upsert됨 + 벤더 재활성화 시 enqueue 성립"까지 단언(Task 4.4).
> - **[LOW] 배지 문구** — `failed.length === vendors.length`면 "벤더 리뷰 실패"(전원), 아니면 "일부 벤더 리뷰 실패"로 분기(Task 6.2).

---

## 파일 구조 (decomposition 고정)

```
almighty-pr-review-server/
├── pyproject.toml                    # 패키지/의존성/pytest 설정
├── README.md                         # 실행법
├── server/
│   ├── __init__.py
│   ├── config.py                     # Settings, 경로, 기본값(effort/N/poll)
│   ├── db.py                         # sqlite 커넥션 + 스키마 init(§6 전 테이블)
│   ├── models.py                     # dataclass DTO (repo/pr/finding 등)
│   ├── repos/                        # 테이블별 CRUD (responsibility별 분리)
│   │   ├── __init__.py
│   │   ├── repo_repo.py              # 모니터링 레포 + 레포별 설정
│   │   ├── pr_repo.py                # pull_request
│   │   ├── prescreen_repo.py         # pre_screen
│   │   ├── job_repo.py               # review_job (큐 claim/상태전이) ★개정
│   │   ├── review_repo.py            # review_run + vendor_result
│   │   ├── finding_repo.py           # finding
│   │   ├── posted_repo.py            # posted_comment
│   │   └── settings_repo.py          # app_settings(단일행)
│   ├── github/
│   │   └── gh.py                     # gh CLI 래퍼(poll/diff/post) — 유일 write=post
│   ├── review/
│   │   ├── harness.py                # HarnessProfile(격리 config dir 구성)
│   │   ├── worktree.py               # 격리 git worktree 생명주기
│   │   ├── findings_schema.py        # 공통 findings 스키마 + parse/validate
│   │   ├── vendors.py                # ClaudeAdapter/CodexAdapter(헤드리스, read-only)
│   │   ├── prescreen.py             # 사전 스크리닝(가벼운 모델, diff만)
│   │   ├── runner.py                 # RunnerPool(asyncio 세마포어 N)
│   │   └── merge.py                  # (옵션) 결정론적 병합
│   ├── pipeline.py                   # 오케스트레이션(§7 3~9, 잡 1건 실행)
│   ├── worker.py                     # review_job 소비 worker 루프 ★개정
│   ├── formatter.py                  # 구조화 마크다운 코멘트 빌더
│   ├── poller.py                     # 백그라운드 폴링 루프(→ job enqueue)
│   ├── seams.py                      # ContextProvider(no-op)/MemoryStore/Identity
│   ├── api.py                        # FastAPI app + 라우트(잡 enqueue만)
│   └── main.py                       # uvicorn 엔트리포인트(lifespan=poller+worker)
├── harness/
│   └── default/                      # v1 = 두 벤더 공유 하네스(디스크)
│       ├── review-system-prompt.md
│       ├── tools-allowlist.json
│       └── config.json               # model/effort/mcp/sandbox
├── web/                              # React/Vite 프론트(앱 셸)
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx                   # nav 목록 + 콘텐츠 영역(확장형 셸)
│       ├── api.ts                    # 백엔드 fetch 래퍼
│       ├── theme.css                 # 라이트테마 + 한글 타이포(§8)
│       └── sections/
│           ├── ReviewSection.tsx     # 레포 탭 + 오버뷰↔디테일 드릴
│           ├── HarnessSection.tsx    # 하네스 편집
│           ├── SettingsSection.tsx   # 전역/레포별 설정
│           └── StubSection.tsx       # LLM위키/자가학습 "곧 제공" 스텁
└── tests/
    ├── conftest.py                   # 임시 DB fixture 등
    ├── test_db.py
    ├── test_repos.py
    ├── test_gh.py
    ├── test_findings_schema.py
    ├── test_worktree.py
    ├── test_vendors.py
    ├── test_prescreen.py
    ├── test_runner.py
    ├── test_merge.py
    ├── test_pipeline.py
    ├── test_formatter.py
    └── test_api.py
```

**결정 근거:** 테이블별 repo 분리 = 함께 바뀌는 것끼리 모음(§6 각 테이블). review/* 는 리뷰 실행 관심사(worktree·vendor·runner·merge) 응집. seam은 `seams.py`에 격리해 v1 no-op이 한눈에. 프론트는 "nav 목록 + 콘텐츠" 단순 패턴(§8 확장성).

---

## Milestone 0 — 스캐폴드 & 개발 루프

**목표:** 빈 레포에서 pytest·FastAPI·git이 도는 최소 골격 확립. 이후 모든 마일스톤의 실행/검증 기반.

### Task 0.1: 프로젝트 초기화 & 헬스 엔드포인트

**Files:**
- Create: `pyproject.toml`
- Create: `server/__init__.py` (빈 파일)
- Create: `server/api.py`
- Create: `server/main.py`
- Create: `tests/__init__.py` (빈 파일)
- Test: `tests/test_health.py`

- [ ] **Step 1: git 저장소 초기화**

Run:
```bash
cd almighty-pr-review-server
git init
printf '__pycache__/\n*.pyc\n.venv/\n*.db\nweb/node_modules/\nweb/dist/\nharness/**/runtime/\n' > .gitignore
```
Expected: `.git/` 생성, `.gitignore` 작성.

- [ ] **Step 2: `pyproject.toml` 작성**

```toml
[project]
name = "almighty-pr-review-server"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]  # httpx = FastAPI TestClient 의존

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.setuptools.packages.find]
include = ["server*"]
```

- [ ] **Step 3: 가상환경 + 설치**

Run:
```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```
Expected: fastapi/uvicorn/pytest 설치 성공.

- [ ] **Step 4: 실패 테스트 작성** — `tests/test_health.py`

```python
from fastapi.testclient import TestClient
from server.api import app

def test_health_ok():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 5: 실패 확인**

Run: `pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.api'`

- [ ] **Step 6: 최소 구현** — `server/api.py`

```python
from fastapi import FastAPI

app = FastAPI(title="Almighty PR Review Server")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
```

그리고 `server/main.py`:
```python
import uvicorn

from server.api import app


def run() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    run()
```

- [ ] **Step 7: 통과 확인**

Run: `pytest tests/test_health.py -v`
Expected: PASS.

- [ ] **Step 8: 서버 기동 스모크(선택)**

Run: `python -m server.main &` 후 `curl -sf http://127.0.0.1:8787/api/health`
Expected: `{"status":"ok"}`. 확인 후 프로세스 종료.

- [ ] **Step 9: 커밋**

```bash
git add pyproject.toml .gitignore server/ tests/
git commit -m "chore: scaffold FastAPI server with health endpoint and pytest"
```

---

## Milestone 0.5 — 벤더 CLI Capability Spike ★개정(선행)

**목표:** 어댑터/하네스를 가정으로 구현하기 전에 `claude`·`codex` 헤드리스 실행의 **실제 계약**을 실증한다. 여기서 확정한 argv·환경변수·출력형식이 Milestone 3(하네스·어댑터) 구현의 입력이 된다. (codex 리뷰 [HIGH]: 실플래그 확정을 E2E 직전까지 미룬 순서 오류 교정.)

### Task 0.5.1: CLI 계약 실증 & 문서화

**Files:**
- Create: `docs/vendor-cli-contract.md` (실증 결과 = 구현 입력)
- Create: `harness/default/preflight.sh` (격리 검증 스크립트)

- [ ] **Step 1: 헤드리스/플래그 확인**

Run:
```bash
claude --help 2>&1 | grep -iE "print|-p|allowedTools|permission|model|output-format|config"
codex --help 2>&1; codex exec --help 2>&1 | grep -iE "sandbox|skip-git|json|model|home|cd"
```
Expected: 각 CLI의 (a) 비대화 실행 플래그, (b) read-only/tool 제한, (c) 구조화 출력 옵션, (d) config dir 환경변수를 확인. **본 세션에서 확인된 사실**(구현 시 재검증): codex는 `codex exec --sandbox read-only`, git 저장소 밖이면 `--skip-git-repo-check` 필요, **positional prompt를 줘도 stdin을 추가로 읽으므로 `< /dev/null`로 stdin을 닫아야 무한대기(0% CPU hang)를 피함**, 출력은 종료 시 일괄 flush.

- [ ] **Step 2: 격리 preflight — 인증 성립 + 전역 미상속을 *동시에* 실증** ★개정(codex v3 [HIGH])

**핵심 모순 해소:** 격리를 위해 `HOME`/`CLAUDE_CONFIG_DIR`/`CODEX_HOME`를 임시 dir로 바꾸면 CLI가 **로그인 자격을 못 찾아 매 리뷰가 실패**할 수 있다. 따라서 "① 로그인은 된다 + ② 전역 rules/skills/MCP는 안 읽힌다"를 **같은 preflight에서 둘 다 통과**시켜야 한다. 이 스텝의 산출물이 `isolated_env`/`prepare_runtime`(Task 3.3) 구현을 확정한다.

확정할 것(계약 문서에 필수 기재):
- **인증 파일/경로** — 각 CLI가 로그인 토큰을 어디에 두는가(예: `~/.claude/.credentials.json` 또는 OS 키체인, `~/.codex/auth.json` 등). 키체인 기반이면 env 격리로도 인증 유지되는지, 파일 기반이면 **read-only symlink/copy로 runtime dir에 주입**해야 하는지.
- **인증 env allowlist** — 인증에 필요한 최소 env(키체인 접근용 등)를 격리 env에 통과시킬 목록.

`harness/default/preflight.sh` — **claude·codex 양쪽**에서 ①인증 성립 + ②전역 미상속을 실제 명령으로 검증(codex v4 [MEDIUM]: executable화). 전역 CLAUDE.md에 고유 마커 한 줄(`ALMIGHTY_GLOBAL_MARKER_9F3A`)을 심어두고, 격리 runtime에선 그 마커를 **모른다**고 답해야 통과:
```bash
#!/usr/bin/env bash
# 인증 성립 + 전역 미상속을 claude/codex 양쪽에서 동시에 실증.
set -euo pipefail
MARKER="ALMIGHTY_GLOBAL_MARKER_9F3A"   # 전역 CLAUDE.md에 심어둔 고유 토큰
RT="$(mktemp -d)"
export HOME="$RT" XDG_CONFIG_HOME="$RT/config"
export CLAUDE_CONFIG_DIR="$RT/claude" CODEX_HOME="$RT/codex"
mkdir -p "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$XDG_CONFIG_HOME"

# 인증 파일 주입(0.5.1 Step2에서 확정한 경로/방식으로 채운다).
# 파일 기반이면 read-only symlink, 키체인 기반이면 아래 두 줄은 no-op.
[ -n "${REAL_CLAUDE_CREDS:-}" ] && ln -sf "$REAL_CLAUDE_CREDS" "$CLAUDE_CONFIG_DIR/.credentials.json"
[ -n "${REAL_CODEX_AUTH:-}" ]   && ln -sf "$REAL_CODEX_AUTH"   "$CODEX_HOME/auth.json"

fail() { echo "[preflight] FAIL: $1"; exit 1; }
# 응답의 마지막 비어있지 않은 라인만 정규화(공백·구두점 제거, 대문자화)해서 sentinel 비교.
# prose/멀티라인 로그에 취약한 grep 대신 정확 일치로 판정(codex v5 [MEDIUM]).
last_token() { awk 'NF{l=$0} END{print l}' | tr -dc 'A-Za-z' | tr 'a-z' 'A-Z'; }

# ① 인증 성립 — 두 CLI 모두 sentinel 'OK'로 정확히 응답해야 통과
[ "$(claude -p 'Reply with exactly one word: OK' < /dev/null | last_token)" = "OK" ] || fail "claude auth"
[ "$(codex exec --skip-git-repo-check --sandbox read-only 'Reply with exactly one word: OK' < /dev/null | last_token)" = "OK" ] || fail "codex auth"

# ② 전역 미상속 — 마커를 못 보면 'CLEAN', 보이면 'LEAKED'. 정확히 CLEAN이어야 통과.
Q="If your instructions contain a token named $MARKER, reply with exactly one word: LEAKED. Otherwise reply with exactly one word: CLEAN."
[ "$(claude -p "$Q" < /dev/null | last_token)" = "CLEAN" ] || fail "claude leaked global CLAUDE.md"
[ "$(codex exec --skip-git-repo-check --sandbox read-only "$Q" < /dev/null | last_token)" = "CLEAN" ] || fail "codex leaked global config"

echo "[preflight] PASS — claude/codex 모두 auth-ok + no-global-inherit"
```
> 이 스크립트가 **PASS해야만** Milestone 3.3/3.4 어댑터 구현을 시작한다(계약 문서에 exit 코드·주입 방식 기록). sentinel(`OK`/`CLEAN`/`LEAKED`) **정확 일치**로 판정하므로 부가 로그·prose 응답에 흔들리지 않는다. `LEAKED`·비-`CLEAN` 응답은 전역 유출로 간주해 실패.
> **주의 (codex v6 [LOW] false-negative):** LLM이 한 단어 대신 문장으로 답하면 `last_token`이 sentinel과 불일치해 **auth 성립인데도 fail**로 오판할 수 있다. 그래서 Step3에서 **각 CLI가 one-word 지시를 실제로 준수하는지 실증**하고, 안 지키면 (a) 프롬프트를 CLI별로 조정하거나 (b) `fail` 시 raw stdout을 함께 로그해 원인을 드러낸다. `preflight.sh`의 sentinel 파싱 규약은 그 실증 결과로 확정한다(계약 문서에 기재).

- [ ] **Step 3: 계약 문서화** — `docs/vendor-cli-contract.md`에 argv/env/출력형식/timeout/실패메시지(rate-limit 감지)/**인증 주입 방식**을 표로 기록. Milestone 3.3/3.4는 이 문서를 **단일 진실원**으로 참조. `preflight.sh`가 **인증+격리 둘 다 통과**해야 어댑터 구현 시작.

- [ ] **Step 4: 커밋**

```bash
git add docs/vendor-cli-contract.md harness/default/preflight.sh
git commit -m "spike: document verified claude/codex headless CLI contract"
```

> **가드레일:** Milestone 3.3/3.4의 argv·env는 이 문서와 **불일치하면 안 된다**. 불일치 시 구현이 아니라 이 문서를 먼저 갱신한다.

---

## Milestone 1 — 영속화(SQLite) 레이어

**목표:** §6 데이터모델의 전 테이블 + 스케줄용 `review_job`(★개정) 스키마와 테이블별 CRUD를 TDD로 확립. 커넥션은 **connection-per-unit + WAL**(★개정)로 동시성 안전.

### Task 1.1: DB 커넥션 & 스키마 초기화

**Files:**
- Create: `server/config.py`
- Create: `server/db.py`
- Create: `tests/conftest.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_db.py`

```python
from server.db import connect, init_schema

EXPECTED_TABLES = {
    "repo", "harness", "pull_request", "pre_screen",
    "review_run", "vendor_result", "finding",
    "posted_comment", "app_settings", "review_job",  # ★개정
}

def test_init_schema_creates_all_tables(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names

def test_app_settings_seeded_single_row(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    rows = conn.execute("SELECT * FROM app_settings").fetchall()
    assert len(rows) == 1
    assert rows[0]["concurrency_limit"] == 2

def test_connect_enables_wal(tmp_path):  # ★개정: 동시성 안전
    conn = connect(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

def test_repo_has_local_path_and_job_columns(tmp_path):  # ★개정
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    repo_cols = {r[1] for r in conn.execute("PRAGMA table_info(repo)")}
    assert "local_path" in repo_cols
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(review_job)")}
    assert {"status", "attempts", "locked_by", "next_run_at"} <= job_cols
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: server.db`

- [ ] **Step 3: `server/config.py` 구현**

```python
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
```

- [ ] **Step 4: `server/db.py` 구현** — 스키마는 §6 컬럼을 그대로 반영

```python
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  trigger_mode TEXT NOT NULL DEFAULT 'auto',      -- auto|manual
  poll_interval_sec INTEGER NOT NULL DEFAULT 60,
  default_effort TEXT NOT NULL DEFAULT 'medium',
  vendor_claude_on INTEGER NOT NULL DEFAULT 1,
  vendor_codex_on INTEGER NOT NULL DEFAULT 1,
  merge_enabled INTEGER NOT NULL DEFAULT 0,
  auto_post INTEGER NOT NULL DEFAULT 0,
  harness_name TEXT NOT NULL DEFAULT 'default',
  local_path TEXT,                                -- ★개정: 로컬 clone 경로(worktree 소스). 등록 시 검증
  last_polled_at TEXT
);
CREATE TABLE IF NOT EXISTS harness (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL DEFAULT 'global',            -- global|repo|situation
  path TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS pull_request (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repo(id),
  number INTEGER NOT NULL,
  title TEXT, author TEXT, head_sha TEXT NOT NULL,
  base_ref TEXT, state TEXT NOT NULL DEFAULT 'open',
  url TEXT, last_reviewed_sha TEXT,
  first_seen_at TEXT, updated_at TEXT,
  UNIQUE(repo_id, number)
);
CREATE TABLE IF NOT EXISTS pre_screen (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL, model TEXT,
  complexity TEXT, score REAL, reason TEXT,
  duration_ms INTEGER, decided TEXT,               -- review|skip|manual
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS review_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL, trigger TEXT, effort TEXT,
  merge_enabled INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',           -- queued|running|done|failed|canceled
  started_at TEXT, finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS vendor_result (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor TEXT NOT NULL,                            -- claude|codex
  status TEXT, duration_ms INTEGER, tokens INTEGER,
  raw_path TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS finding (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor_result_id INTEGER REFERENCES vendor_result(id),
  vendor TEXT NOT NULL, file TEXT, line INTEGER,
  severity TEXT, category TEXT, claim TEXT, rationale TEXT,
  confidence REAL, consensus TEXT DEFAULT 'single',
  consensus_group_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',          -- pending|approved|dismissed|edited|posted
  edited_text TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS posted_comment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor TEXT NOT NULL,                            -- claude|codex|merged
  github_comment_id TEXT, url TEXT, marker TEXT,
  body TEXT, posted_at TEXT,
  head_sha TEXT,                                  -- ★개정: update-or-create 판단 키
  finding_ids TEXT,                               -- ★개정: 포함 finding id(JSON)
  superseded_at TEXT                              -- ★개정: 재리뷰로 대체된 시점
);
-- ★개정: 스케줄링 상태(review_job)와 실행 이력(review_run) 분리.
CREATE TABLE IF NOT EXISTS review_job (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'auto',            -- auto|manual
  status TEXT NOT NULL DEFAULT 'queued',           -- queued|running|done|failed|canceled
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  locked_by TEXT, locked_at TEXT,
  next_run_at TEXT,                                -- backoff/rate-limit 지연
  run_id INTEGER REFERENCES review_run(id),
  error TEXT, created_at TEXT,
  UNIQUE(pr_id, head_sha)                          -- 같은 sha 중복 잡 방지(idempotency)
);
CREATE TABLE IF NOT EXISTS app_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  default_effort TEXT NOT NULL DEFAULT 'medium',
  concurrency_limit INTEGER NOT NULL DEFAULT 2,
  default_poll_interval INTEGER NOT NULL DEFAULT 60,
  approval_gate_on INTEGER NOT NULL DEFAULT 1,
  prescreen_model TEXT NOT NULL DEFAULT 'claude-haiku',
  prescreen_gate_threshold TEXT NOT NULL DEFAULT 'moderate'
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """커넥션 1개 = 1 사용 단위(요청/worker 잡). 전역 공유 금지(★개정).

    WAL + busy_timeout으로 reader/writer 동시성 및 잠깐의 락 경합을 흡수.
    """
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (id) VALUES (1)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO harness (name, scope, path) VALUES "
        "('default', 'global', 'harness/default')"
    )
    conn.commit()
```

- [ ] **Step 5: `tests/conftest.py` — 임시 DB fixture**

```python
import pytest

from server.db import connect, init_schema


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    yield conn
    conn.close()
```

- [ ] **Step 6: 통과 확인**

Run: `pytest tests/test_db.py -v`
Expected: PASS (4 passed — WAL·local_path·review_job 포함).

- [ ] **Step 7: 커밋**

```bash
git add server/config.py server/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: sqlite WAL schema for all v1 tables incl review_job queue"
```

### Task 1.2: 테이블별 CRUD repo

**Files:**
- Create: `server/models.py`
- Create: `server/repos/__init__.py` (빈 파일)
- Create: `server/repos/repo_repo.py`, `pr_repo.py`, `prescreen_repo.py`, `review_repo.py`, `finding_repo.py`, `posted_repo.py`, `settings_repo.py`, `job_repo.py`(★개정)
- Test: `tests/test_repos.py`, `tests/test_job_repo.py`(★개정)

- [ ] **Step 1: 실패 테스트** — `tests/test_repos.py`

```python
from server.repos import repo_repo, pr_repo, finding_repo, settings_repo


def test_add_and_get_repo(db):
    rid = repo_repo.add(db, full_name="acme/api")
    row = repo_repo.get(db, rid)
    assert row["full_name"] == "acme/api"
    assert row["vendor_claude_on"] == 1

def test_upsert_pr_updates_head_sha(db):
    rid = repo_repo.add(db, full_name="acme/api")
    p1 = pr_repo.upsert(db, repo_id=rid, number=7, title="t",
                        author="a", head_sha="aaa", base_ref="main", url="u")
    p2 = pr_repo.upsert(db, repo_id=rid, number=7, title="t",
                        author="a", head_sha="bbb", base_ref="main", url="u")
    assert p1 == p2  # 같은 (repo, number) → 같은 id
    assert pr_repo.get(db, p1)["head_sha"] == "bbb"

def test_finding_status_transition(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=1, title="t",
                         author="a", head_sha="s", base_ref="main", url="u")
    run_id = db.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, ?)", (pid, "s")
    ).lastrowid
    fid = finding_repo.add(db, run_id=run_id, vendor="claude", file="a.py",
                           line=3, severity="high", category="bug",
                           claim="c", rationale="r", confidence=0.8)
    finding_repo.set_status(db, fid, "approved")
    assert finding_repo.get(db, fid)["status"] == "approved"

def test_settings_singleton_update(db):
    settings_repo.update(db, concurrency_limit=4)
    assert settings_repo.get(db)["concurrency_limit"] == 4
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_repos.py -v`
Expected: FAIL — `ModuleNotFoundError: server.repos.repo_repo`

- [ ] **Step 3: `server/repos/repo_repo.py` 구현**

```python
import sqlite3


def add(conn: sqlite3.Connection, *, full_name: str, **overrides) -> int:
    cur = conn.execute(
        "INSERT INTO repo (full_name) VALUES (?)", (full_name,)
    )
    conn.commit()
    rid = cur.lastrowid
    if overrides:
        update(conn, rid, **overrides)
    return rid


def get(conn: sqlite3.Connection, rid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repo WHERE id = ?", (rid,)).fetchone()


def list_enabled(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM repo WHERE enabled = 1"
    ).fetchall()


ALLOWED = {
    "enabled", "trigger_mode", "poll_interval_sec", "default_effort",
    "vendor_claude_on", "vendor_codex_on", "merge_enabled", "auto_post",
    "harness_name", "local_path", "last_polled_at",  # ★개정: local_path
}


def update(conn: sqlite3.Connection, rid: int, **fields) -> None:
    cols = [c for c in fields if c in ALLOWED]
    if not cols:
        return
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE repo SET {sets} WHERE id = ?",
        [fields[c] for c in cols] + [rid],
    )
    conn.commit()
```

- [ ] **Step 4: `server/repos/pr_repo.py` 구현**

```python
import sqlite3


def upsert(conn, *, repo_id, number, title, author,
           head_sha, base_ref, url, state="open") -> int:
    conn.execute(
        """INSERT INTO pull_request
           (repo_id, number, title, author, head_sha, base_ref, state, url,
            first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
           ON CONFLICT(repo_id, number) DO UPDATE SET
             title=excluded.title, author=excluded.author,
             head_sha=excluded.head_sha, base_ref=excluded.base_ref,
             state=excluded.state, url=excluded.url,
             updated_at=datetime('now')""",
        (repo_id, number, title, author, head_sha, base_ref, state, url),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM pull_request WHERE repo_id=? AND number=?",
        (repo_id, number),
    ).fetchone()["id"]


def get(conn, pid) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pull_request WHERE id = ?", (pid,)
    ).fetchone()


def mark_reviewed(conn, pid, head_sha) -> None:
    conn.execute(
        "UPDATE pull_request SET last_reviewed_sha=? WHERE id=?",
        (head_sha, pid),
    )
    conn.commit()


def needs_review(conn, pid) -> bool:
    r = get(conn, pid)
    return r is not None and r["head_sha"] != r["last_reviewed_sha"]
```

- [ ] **Step 5: 나머지 repo 구현** — `prescreen_repo.py`, `review_repo.py`, `finding_repo.py`, `posted_repo.py`, `settings_repo.py`

`server/repos/finding_repo.py`:
```python
def add(conn, *, run_id, vendor, file, line, severity, category,
        claim, rationale, confidence, vendor_result_id=None,
        consensus="single", consensus_group_id=None) -> int:
    cur = conn.execute(
        """INSERT INTO finding
           (run_id, vendor_result_id, vendor, file, line, severity, category,
            claim, rationale, confidence, consensus, consensus_group_id,
            created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
        (run_id, vendor_result_id, vendor, file, line, severity, category,
         claim, rationale, confidence, consensus, consensus_group_id),
    )
    conn.commit()
    return cur.lastrowid


def get(conn, fid):
    return conn.execute("SELECT * FROM finding WHERE id=?", (fid,)).fetchone()


def list_for_run(conn, run_id):
    return conn.execute(
        "SELECT * FROM finding WHERE run_id=? ORDER BY severity, file", (run_id,)
    ).fetchall()


def set_status(conn, fid, status, edited_text=None):
    conn.execute(
        "UPDATE finding SET status=?, edited_text=? WHERE id=?",
        (status, edited_text, fid),
    )
    conn.commit()
```

`server/repos/settings_repo.py`:
```python
ALLOWED = {
    "default_effort", "concurrency_limit", "default_poll_interval",
    "approval_gate_on", "prescreen_model", "prescreen_gate_threshold",
}


def get(conn):
    return conn.execute("SELECT * FROM app_settings WHERE id=1").fetchone()


def update(conn, **fields):
    cols = [c for c in fields if c in ALLOWED]
    if not cols:
        return
    sets = ", ".join(f"{c}=?" for c in cols)
    conn.execute(
        f"UPDATE app_settings SET {sets} WHERE id=1",
        [fields[c] for c in cols],
    )
    conn.commit()
```

`server/repos/prescreen_repo.py`:
```python
def add(conn, *, pr_id, head_sha, model, complexity, score, reason,
        duration_ms, decided) -> int:
    cur = conn.execute(
        """INSERT INTO pre_screen
           (pr_id, head_sha, model, complexity, score, reason,
            duration_ms, decided, created_at)
           VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
        (pr_id, head_sha, model, complexity, score, reason,
         duration_ms, decided),
    )
    conn.commit()
    return cur.lastrowid


def latest_for_pr(conn, pr_id):
    return conn.execute(
        "SELECT * FROM pre_screen WHERE pr_id=? ORDER BY id DESC LIMIT 1",
        (pr_id,),
    ).fetchone()
```

`server/repos/review_repo.py`:
```python
def create_run(conn, *, pr_id, head_sha, trigger, effort, merge_enabled=0) -> int:
    cur = conn.execute(
        """INSERT INTO review_run
           (pr_id, head_sha, trigger, effort, merge_enabled, status, started_at)
           VALUES (?,?,?,?,?, 'running', datetime('now'))""",
        (pr_id, head_sha, trigger, effort, merge_enabled),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, error=None):
    conn.execute(
        "UPDATE review_run SET status=?, error=?, finished_at=datetime('now') "
        "WHERE id=?",
        (status, error, run_id),
    )
    conn.commit()


def add_vendor_result(conn, *, run_id, vendor, status, duration_ms=None,
                      tokens=None, raw_path=None, error=None) -> int:
    cur = conn.execute(
        """INSERT INTO vendor_result
           (run_id, vendor, status, duration_ms, tokens, raw_path, error)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, vendor, status, duration_ms, tokens, raw_path, error),
    )
    conn.commit()
    return cur.lastrowid


def get_run(conn, run_id):
    return conn.execute(
        "SELECT * FROM review_run WHERE id=?", (run_id,)
    ).fetchone()


def list_vendor_results(conn, run_id):
    # ★개정 (codex v6 [MEDIUM]): 부분 실패 벤더를 대시보드가 노출할 수 있게
    # run의 vendor_result 행을 반환(실패 벤더 배지 근거).
    return conn.execute(
        "SELECT vendor, status, error, duration_ms FROM vendor_result "
        "WHERE run_id=? ORDER BY vendor", (run_id,)
    ).fetchall()
```

`server/repos/posted_repo.py` (★개정: head_sha/finding_ids/supersede):
```python
import json


def add(conn, *, run_id, vendor, github_comment_id, url, marker, body,
        head_sha=None, finding_ids=None) -> int:
    cur = conn.execute(
        """INSERT INTO posted_comment
           (run_id, vendor, github_comment_id, url, marker, body,
            head_sha, finding_ids, posted_at)
           VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
        (run_id, vendor, github_comment_id, url, marker, body,
         head_sha, json.dumps(finding_ids or [])),
    )
    conn.commit()
    return cur.lastrowid


def latest_for_pr_vendor(conn, *, pr_id, vendor):
    """같은 PR·벤더의 최신 비대체 코멘트(update-or-create 판단용)."""
    return conn.execute(
        """SELECT pc.* FROM posted_comment pc
           JOIN review_run rr ON rr.id = pc.run_id
           WHERE rr.pr_id=? AND pc.vendor=? AND pc.superseded_at IS NULL
           ORDER BY pc.id DESC LIMIT 1""",
        (pr_id, vendor),
    ).fetchone()


def supersede(conn, posted_id):
    conn.execute(
        "UPDATE posted_comment SET superseded_at=datetime('now') WHERE id=?",
        (posted_id,))
    conn.commit()
```

- [ ] **Step 6: `server/models.py` — 프론트/파이프라인 공용 DTO**

```python
from dataclasses import dataclass


@dataclass
class Finding:
    vendor: str
    file: str
    line: int
    severity: str          # critical|high|medium|low
    category: str          # bug|security|perf|style|other
    claim: str
    rationale: str
    confidence: float
    vendor_result_id: int | None = None  # ★개정: 병합 후에도 벤더 추적성 유지
```

- [ ] **Step 7: 통과 확인**

Run: `pytest tests/test_repos.py -v`
Expected: PASS (4 passed).

- [ ] **Step 8: 커밋**

```bash
git add server/models.py server/repos/
git commit -m "feat: per-table CRUD repos with upsert and status transitions"
```

### Task 1.3: 잡 큐 repo (원자적 claim) ★개정

**Files:**
- Create: `server/repos/job_repo.py`
- Test: `tests/test_job_repo.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_job_repo.py`

```python
from server.repos import repo_repo, pr_repo, job_repo


def _seed(db, sha="s1"):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=1, title="t", author="a",
                         head_sha=sha, base_ref="main", url="u")
    return pid


def test_enqueue_is_idempotent_per_sha(db):
    pid = _seed(db)
    j1 = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    j2 = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    assert j1 == j2  # UNIQUE(pr_id, head_sha) → 같은 잡


def test_claim_next_locks_one_job(db):
    pid = _seed(db)
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    claimed = job_repo.claim_next(db, worker_id="w1")
    assert claimed["status"] == "running"
    assert claimed["locked_by"] == "w1"
    # 이미 running이면 다음 claim은 없음
    assert job_repo.claim_next(db, worker_id="w2") is None


def test_finish_and_retry(db):
    pid = _seed(db)
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    j = job_repo.claim_next(db, worker_id="w1")
    job_repo.mark_failed(db, j["id"], error="rate limit", retry=True)
    row = db.execute("SELECT * FROM review_job WHERE id=?", (j["id"],)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1
    assert row["next_run_at"] is not None  # backoff 설정됨
```

- [ ] **Step 2: 실패 확인 → 구현** — `server/repos/job_repo.py`

```python
def enqueue(conn, *, pr_id, head_sha, trigger, priority=0) -> int:
    conn.execute(
        """INSERT INTO review_job (pr_id, head_sha, trigger, priority, created_at)
           VALUES (?,?,?,?, datetime('now'))
           ON CONFLICT(pr_id, head_sha) DO NOTHING""",
        (pr_id, head_sha, trigger, priority),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM review_job WHERE pr_id=? AND head_sha=?",
        (pr_id, head_sha),
    ).fetchone()["id"]


import sqlite3

STALE_LOCK_MINUTES = 30


def claim_next(conn, *, worker_id):
    """queued(또는 backoff 만료)인 잡 1건을 원자적으로 running 전이.

    ★개정: 다른 worker가 BEGIN IMMEDIATE를 선점하면 database is locked가
    날 수 있음 → busy_timeout(5s) 대기 후에도 실패하면 None(다음 tick 재시도).
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return None  # 다른 worker가 선점 — 이번 tick은 빈손
    try:
        row = conn.execute(
            """SELECT * FROM review_job
               WHERE status='queued'
                 AND (next_run_at IS NULL OR next_run_at <= datetime('now'))
               ORDER BY priority DESC, id ASC LIMIT 1""",
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            """UPDATE review_job SET status='running', locked_by=?,
               locked_at=datetime('now'), attempts=attempts+1 WHERE id=?""",
            (worker_id, row["id"]),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        return None
    return conn.execute(
        "SELECT * FROM review_job WHERE id=?", (row["id"],)
    ).fetchone()


def recover_stale(conn) -> int:
    """★개정: worker 크래시로 running/locked 고착된 잡을 queued로 복구.
    worker 시작 시 1회 호출. 반환 = 복구 건수."""
    cur = conn.execute(
        """UPDATE review_job SET status='queued', locked_by=NULL,
           error='recovered from stale lock'
           WHERE status='running'
             AND locked_at <= datetime('now', ?)""",
        (f"-{STALE_LOCK_MINUTES} minutes",),
    )
    conn.commit()
    return cur.rowcount


def mark_done(conn, job_id, run_id):
    conn.execute(
        "UPDATE review_job SET status='done', run_id=?, locked_by=NULL WHERE id=?",
        (run_id, job_id))
    conn.commit()


def mark_failed(conn, job_id, *, error, retry: bool, run_id=None):
    # ★개정 (codex v3 [HIGH]): run_id는 이번 attempt의 (failed) review_run.
    # retry로 queued 되돌릴 때도 job.run_id에 최신 attempt run을 남겨,
    # failed run과 retry job이 갈라지지 않게 한다.
    # ★정책 (codex v4 [MEDIUM]): run_id=None은 **pre-run 실패**(build_deps/claim
    # 직후 등, review_run 생성 전). 이땐 COALESCE로 직전 run 포인터를 유지하되,
    # job.error는 최신(=pre-run) 에러다 → error↔run 짝은 pre-run 실패에선 보장 안 됨.
    # attempt별 정확한 상태는 review_run 테이블(pr_id로 조회)이 단일 진실원.
    row = conn.execute(
        "SELECT attempts, max_attempts FROM review_job WHERE id=?", (job_id,)
    ).fetchone()
    if retry and row["attempts"] < row["max_attempts"]:
        # 지수 backoff: 2^attempts 분 뒤 재시도
        conn.execute(
            """UPDATE review_job SET status='queued', locked_by=NULL,
               error=?, run_id=COALESCE(?, run_id),
               next_run_at=datetime('now', '+' ||
               (1 << attempts) || ' minutes') WHERE id=?""",
            (error, run_id, job_id))
    else:
        conn.execute(
            "UPDATE review_job SET status='failed', error=?, "
            "run_id=COALESCE(?, run_id), locked_by=NULL "
            "WHERE id=?", (error, run_id, job_id))
    conn.commit()
```

- [ ] **Step 2.5: 동시성/복구 테스트** ★개정 (codex v3 [HIGH]: 가짜 그린 제거)

`tests/test_job_repo.py`에 추가. **핵심:** 이전 버전의 "c1 claim 후 c2 claim" 순차 호출은 락 경합 경로를 타지 않아 항상 통과하는 **가짜 그린**이었다. 두 가지로 대체 —
(a) **결정론적 락 경합**: c1이 `BEGIN IMMEDIATE`로 writer 락을 잡은 상태에서 c2 `claim_next`가 `None`을 반환하고(락 대기 후 실패), c1 커밋 뒤엔 c2가 정상 claim하는지. `busy_timeout`을 짧게 눌러 5초를 기다리지 않게 한다.
(b) **스레드 barrier 동시 출발**: N개 worker가 동시에 `claim_next`를 때려도 정확히 1건만 claim되는지.

```python
def test_claim_blocks_on_writer_lock_then_recovers(tmp_path):
    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo
    p = tmp_path / "c.db"
    c0 = connect(p); init_schema(c0)
    rid = repo_repo.add(c0, full_name="acme/api")
    pid = pr_repo.upsert(c0, repo_id=rid, number=1, title="t", author="a",
                         head_sha="s1", base_ref="main", url="u")
    job_repo.enqueue(c0, pr_id=pid, head_sha="s1", trigger="auto")
    c1, c2 = connect(p), connect(p)
    c2.execute("PRAGMA busy_timeout=200")     # 5초 대신 200ms만 대기
    c1.execute("BEGIN IMMEDIATE")             # writer 락 선점
    assert job_repo.claim_next(c2, worker_id="w2") is None   # 락 경합 → None
    c1.rollback()                             # 락 해제
    got = job_repo.claim_next(c2, worker_id="w2")             # 이제 성공
    assert got is not None and got["locked_by"] == "w2"
    for c in (c0, c1, c2):
        c.close()


def test_concurrent_claim_exactly_once(tmp_path):
    import threading
    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo
    p = tmp_path / "c.db"
    c0 = connect(p); init_schema(c0)
    rid = repo_repo.add(c0, full_name="acme/api")
    pid = pr_repo.upsert(c0, repo_id=rid, number=1, title="t", author="a",
                         head_sha="s1", base_ref="main", url="u")
    job_repo.enqueue(c0, pr_id=pid, head_sha="s1", trigger="auto")
    N = 8
    barrier = threading.Barrier(N)
    results = [None] * N

    def worker(i):
        conn = connect(p)
        try:
            barrier.wait()                    # N개 스레드 동시 출발
            results[i] = job_repo.claim_next(conn, worker_id=f"w{i}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1                  # 정확히 하나만 claim
    c0.close()


def test_recover_stale_requeues_running(db):
    from server.repos import repo_repo, pr_repo, job_repo
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=1, title="t", author="a",
                         head_sha="s1", base_ref="main", url="u")
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    # 오래된 running lock 위조
    db.execute("""UPDATE review_job SET status='running', locked_by='dead',
                  locked_at=datetime('now','-60 minutes') WHERE id=?""", (jid,))
    db.commit()
    assert job_repo.recover_stale(db) == 1
    assert db.execute("SELECT status FROM review_job WHERE id=?",
                      (jid,)).fetchone()["status"] == "queued"
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_job_repo.py -v` → PASS (6 passed)
```bash
git add server/repos/job_repo.py tests/test_job_repo.py
git commit -m "feat: review_job queue with atomic claim, stale-lock recovery, backoff"
```

---

## Milestone 2 — GitHub gh CLI 래퍼

**목표:** `gh` CLI로 open PR 폴링·diff 취득·코멘트 포스팅을 래핑. 유일한 write 경로는 `post_comment`. subprocess를 주입 가능하게 만들어 실제 네트워크 없이 테스트.

### Task 2.1: gh 래퍼(폴링·diff·포스팅)

**Files:**
- Create: `server/github/__init__.py` (빈 파일)
- Create: `server/github/gh.py`
- Test: `tests/test_gh.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_gh.py`

```python
import json

from server.github import gh


class FakeRunner:
    """subprocess 대체: 등록된 argv 프리픽스에 (stdout) 매핑."""
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        for prefix, out in self.mapping.items():
            if args[: len(prefix)] == list(prefix):
                return out
        raise AssertionError(f"unexpected call: {args}")


def test_list_open_prs_parses_json():
    payload = json.dumps([
        {"number": 7, "title": "fix", "author": {"login": "kim"},
         "headRefOid": "abc", "baseRefName": "main",
         "url": "https://x/7", "state": "OPEN"}
    ])
    runner = FakeRunner({
        ("gh", "pr", "list"): payload,
    })
    client = gh.GhClient(runner=runner)
    prs = client.list_open_prs("acme/api")
    assert prs[0].number == 7
    assert prs[0].head_sha == "abc"
    assert prs[0].author == "kim"


def test_diff_returns_text():
    runner = FakeRunner({("gh", "pr", "diff"): "diff --git a b\n+x"})
    client = gh.GhClient(runner=runner)
    assert "diff --git" in client.diff("acme/api", 7)


def test_post_comment_returns_id_and_url():
    runner = FakeRunner({
        ("gh", "api", "-X", "POST"):
            '{"id": 99, "html_url": "https://x/7#issuecomment-99"}',
    })
    client = gh.GhClient(runner=runner)
    res = client.post_comment("acme/api", 7, "hello")
    assert res["id"] == 99
    assert res["html_url"].endswith("issuecomment-99")
    # 유일한 write 경로 — issues/comments POST 엔드포인트인지 검증
    assert any(a[:2] == ["gh", "api"] and "POST" in a for a in runner.calls)
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_gh.py -v`
Expected: FAIL — `ModuleNotFoundError: server.github.gh`

- [ ] **Step 3: 구현** — `server/github/gh.py`

```python
import json
import subprocess
from dataclasses import dataclass


@dataclass
class PrInfo:
    number: int
    title: str
    author: str
    head_sha: str
    base_ref: str
    url: str
    state: str


def _default_runner(args: list[str], **kw) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True
    ).stdout


class GhClient:
    """gh CLI 얇은 래퍼. runner 주입으로 테스트 가능. write=post_comment 뿐."""

    def __init__(self, runner=_default_runner):
        self._run = runner

    def list_open_prs(self, repo: str) -> list[PrInfo]:
        out = self._run([
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--json", "number,title,author,headRefOid,baseRefName,url,state",
        ])
        return [
            PrInfo(
                number=d["number"], title=d.get("title", ""),
                author=(d.get("author") or {}).get("login", ""),
                head_sha=d["headRefOid"], base_ref=d.get("baseRefName", ""),
                url=d.get("url", ""), state=d.get("state", "OPEN").lower(),
            )
            for d in json.loads(out)
        ]

    def diff(self, repo: str, number: int) -> str:
        return self._run(
            ["gh", "pr", "diff", str(number), "--repo", repo]
        )

    def post_comment(self, repo: str, number: int, body: str) -> dict:
        """issue comment 생성. ★개정 (codex v3 [LOW]): URL 문자열 파싱
        대신 API JSON의 .id를 그대로 저장하도록 {id, html_url}을 반환."""
        out = self._run([
            "gh", "api", "-X", "POST",
            f"/repos/{repo}/issues/{number}/comments",
            "-f", f"body={body}", "--jq", "{id: .id, html_url: .html_url}",
        ]).strip()
        return json.loads(out)

    def edit_comment(self, repo: str, comment_id: str, body: str) -> dict:
        """★개정: 기존 issue comment를 in-place로 수정(진짜 update-or-create).
        comment_id = 숫자형 issuecomment id. {id, html_url} 반환."""
        out = self._run([
            "gh", "api", "-X", "PATCH",
            f"/repos/{repo}/issues/comments/{comment_id}",
            "-f", f"body={body}", "--jq", "{id: .id, html_url: .html_url}",
        ]).strip()
        return json.loads(out)
```

- [ ] **Step 4: 통과 확인** (edit_comment 테스트 추가)

`tests/test_gh.py`에 추가:
```python
def test_edit_comment_patches_in_place():
    runner = FakeRunner({("gh", "api", "-X", "PATCH"):
                         '{"id": 99, "html_url": "https://x/7#issuecomment-99"}'})
    client = gh.GhClient(runner=runner)
    res = client.edit_comment("acme/api", "99", "updated")
    assert res["id"] == 99
    assert res["html_url"].endswith("issuecomment-99")
    assert any(a[:2] == ["gh", "api"] for a in runner.calls)
```

Run: `pytest tests/test_gh.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: 실제 gh 스모크(선택, 인증 필요)**

Run: `gh pr list --repo <내레포> --state open --json number,title | head`
Expected: 실제 PR 목록. (CI/무인증 환경에서는 skip.)

- [ ] **Step 6: 커밋**

```bash
git add server/github/
git commit -m "feat: gh CLI wrapper for PR list, diff, and comment posting"
```

---

## Milestone 3 — 리뷰 하네스 · 벤더 어댑터 · Runner

**목표:** 격리 worktree, 전역 프로파일 미상속 하네스, read-only 벤더 어댑터(claude/codex 헤드리스), 공통 findings 스키마 파싱, 세마포어 RunnerPool, 사전 스크리닝, 옵션 병합. 스펙 §5 안전/격리의 핵심.

### Task 3.1: 공통 findings 스키마 & 파서

**Files:**
- Create: `server/review/__init__.py` (빈 파일)
- Create: `server/review/findings_schema.py`
- Test: `tests/test_findings_schema.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_findings_schema.py`

```python
import pytest

from server.review.findings_schema import parse_findings, SchemaError


VALID = """
관련없는 서두 텍스트.
```json
{"findings": [
  {"file": "a.py", "line": 3, "severity": "high", "category": "bug",
   "claim": "널 역참조", "rationale": "x가 None일 수 있음", "confidence": 0.8}
]}
```
"""


def test_parse_extracts_fenced_json():
    fs = parse_findings(VALID, vendor="claude")
    assert len(fs) == 1
    assert fs[0].vendor == "claude"
    assert fs[0].severity == "high"


def test_parse_rejects_bad_severity():
    bad = '```json\n{"findings":[{"file":"a","line":1,"severity":"WAT",' \
          '"category":"bug","claim":"c","rationale":"r","confidence":0.5}]}\n```'
    with pytest.raises(SchemaError):
        parse_findings(bad, vendor="codex")


def test_parse_no_json_raises():
    with pytest.raises(SchemaError):
        parse_findings("자유서술뿐, JSON 없음", vendor="claude")
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_findings_schema.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `server/review/findings_schema.py`

```python
import json
import re

from server.models import Finding

SEVERITIES = {"critical", "high", "medium", "low"}
CATEGORIES = {"bug", "security", "perf", "style", "other"}

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class SchemaError(ValueError):
    pass


def parse_findings(raw: str, *, vendor: str) -> list[Finding]:
    """CLI stdout에서 마지막 ```json 블록의 findings 배열을 추출·검증."""
    matches = _FENCE.findall(raw)
    if not matches:
        raise SchemaError("응답에 JSON 블록이 없음")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        raise SchemaError(f"JSON 파싱 실패: {e}") from e
    items = data.get("findings")
    if not isinstance(items, list):
        raise SchemaError("findings 배열 없음")
    out: list[Finding] = []
    for it in items:
        sev, cat = it.get("severity"), it.get("category")
        if sev not in SEVERITIES:
            raise SchemaError(f"잘못된 severity: {sev}")
        if cat not in CATEGORIES:
            raise SchemaError(f"잘못된 category: {cat}")
        out.append(Finding(
            vendor=vendor, file=str(it["file"]), line=int(it.get("line", 0)),
            severity=sev, category=cat, claim=str(it["claim"]),
            rationale=str(it.get("rationale", "")),
            confidence=float(it.get("confidence", 0.5)),
        ))
    return out


PROMPT_SCHEMA_HINT = (
    "반드시 마지막에 ```json 블록으로 다음 형식만 출력:\n"
    '{"findings":[{"file","line","severity"(critical|high|medium|low),'
    '"category"(bug|security|perf|style|other),"claim","rationale",'
    '"confidence"(0~1)}]}. 이슈 없으면 빈 배열.'
)
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_findings_schema.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: 커밋**

```bash
git add server/review/__init__.py server/review/findings_schema.py
git commit -m "feat: common findings schema with fenced-json parser and validation"
```

### Task 3.2: 격리 worktree 생명주기

**Files:**
- Create: `server/review/worktree.py`
- Test: `tests/test_worktree.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_worktree.py` (실제 git으로 로컬 픽스처 레포 사용)

```python
import subprocess

from server.review.worktree import prepared_worktree


def _init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def test_worktree_created_and_cleaned(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    sha = _init_repo(src)
    with prepared_worktree(src, sha) as wt:
        assert (wt / "f.txt").read_text() == "hello"
        wt_path = wt
    # 컨텍스트 종료 후 worktree 제거됨
    assert not wt_path.exists()
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_worktree.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `server/review/worktree.py`

```python
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@contextmanager
def prepared_worktree(repo: Path, sha: str):
    """repo의 특정 sha를 detached worktree로 체크아웃. 종료 시 제거(★개정: shutil)."""
    repo = Path(repo)
    tmp = Path(tempfile.mkdtemp(prefix="almighty-wt-"))
    wt = tmp / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt), sha)
    try:
        yield wt
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                        "--force", str(wt)], capture_output=True, text=True)
        shutil.rmtree(tmp, ignore_errors=True)  # ★개정: rm -rf 서브프로세스 대신


def prune_orphans(repo: Path) -> None:
    """실패로 남은 orphan worktree 정리(worker 기동 시 호출)."""
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                   capture_output=True, text=True)
```

> **주의(구현 시):** v1은 내 로컬 레포를 소스로 detached worktree를 만든다. gh PR head를 로컬에 fetch하는 방식은 §7 Step3에 맞춰 파이프라인에서 `gh pr checkout`/`git fetch` 후 sha를 넘긴다.
>
> **★개정 — read-only 경계 명확화 (codex [HIGH]):** worktree 자체는 "격리 경계"일 뿐 read-only를 **보장하지 않는다**. 실제 read-only는 **벤더 tool allowlist / codex sandbox**(Task 0.5·3.3·3.4에서 실증)로 강제한다. worker 기동 시 `recover_stale` 후 필요 시 `prune_orphans`로 이전 실패 잔재를 정리한다.
>
> **동기 git 호출 (codex 재검증 PARTIAL, v1 수용):** `prepared_worktree`의 `git worktree add/remove`는 로컬 레포 대상 ~100–300ms 짧은 동기 호출로, async 파이프라인의 `with` 안에서 실행된다. 무거운 벤더 리뷰는 이미 async라 동시성=2에서 영향 미미 → v1 수용. 처리량이 문제되면 `asyncio.to_thread`로 감싸는 것을 후속(§4 Deferred 4 성능개선)으로.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_worktree.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add server/review/worktree.py tests/test_worktree.py
git commit -m "feat: isolated detached git worktree lifecycle for review"
```

### Task 3.3: 하네스 프로파일(전역 프로파일 격리)

**Files:**
- Create: `server/review/harness.py`
- Create: `harness/default/review-system-prompt.md`
- Create: `harness/default/tools-allowlist.json`
- Create: `harness/default/config.json`
- Test: `tests/test_harness.py`

- [ ] **Step 1: 하네스 디스크 자산 작성**

`harness/default/review-system-prompt.md`:
```markdown
너는 시니어 코드 리뷰어다. 주어진 PR diff와 레포 전체 코드를 근거로
정확한 결함만 보고한다. 추측/스타일 취향은 낮은 severity로. 각 finding은
파일·라인·근거를 반드시 포함한다. 레포를 읽어(Read/Grep/Glob) 맥락을
확인하되 어떤 파일도 수정하지 않는다. git push / pr merge 금지.
```

`harness/default/tools-allowlist.json`:
```json
{"claude_allowed_tools": ["Read", "Grep", "Glob"],
 "codex_sandbox": "read-only",
 "mcp": "none"}
```

`harness/default/config.json`:
```json
{"model": "sonnet", "effort": "medium", "prescreen_model": "haiku"}
```

- [ ] **Step 2: 실패 테스트** — `tests/test_harness.py`

```python
from server.review.harness import HarnessProfile


def test_harness_loads_default(tmp_path, monkeypatch):
    hp = HarnessProfile.load("default")
    assert "코드 리뷰어" in hp.system_prompt
    assert hp.claude_allowed_tools == ["Read", "Grep", "Glob"]
    assert hp.codex_sandbox == "read-only"


def test_isolated_env_excludes_global_profile():
    hp = HarnessProfile.load("default")
    env = hp.isolated_env(runtime_dir="/tmp/rt")
    # 전역 프로파일 미상속: 리뷰 전용 config dir로 재지정
    assert env["CLAUDE_CONFIG_DIR"].endswith("/claude")
    assert env["CODEX_HOME"].endswith("/codex")
```

- [ ] **Step 3: 실패 확인**

Run: `pytest tests/test_harness.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: 구현** — `server/review/harness.py`

```python
import json
from dataclasses import dataclass
from pathlib import Path

from server import config


@dataclass
class HarnessProfile:
    name: str
    system_prompt: str
    claude_allowed_tools: list[str]
    codex_sandbox: str
    mcp: str
    model: str
    effort: str
    prescreen_model: str

    @classmethod
    def load(cls, name: str) -> "HarnessProfile":
        base = config.HARNESS_DIR / name
        tools = json.loads((base / "tools-allowlist.json").read_text())
        cfg = json.loads((base / "config.json").read_text())
        return cls(
            name=name,
            system_prompt=(base / "review-system-prompt.md").read_text(),
            claude_allowed_tools=tools["claude_allowed_tools"],
            codex_sandbox=tools["codex_sandbox"],
            mcp=tools.get("mcp", "none"),
            model=cfg["model"], effort=cfg["effort"],
            prescreen_model=cfg.get("prescreen_model", "haiku"),
        )

    # 인증에 필요한 env allowlist(키체인 접근 등). 정확한 목록은 Task 0.5 실증값.
    AUTH_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TERM", "SHELL", "USER", "LOGNAME")

    def isolated_env(self, *, runtime_dir: str) -> dict:
        """전역 프로파일 미상속 + **인증은 유지**(★개정 codex v3 [HIGH]).

        HOME/config dir을 runtime로 재지정해 전역 rules/skills/MCP는 차단하되,
        인증에 필요한 최소 env만 allowlist로 통과. 파일기반 인증은 prepare_runtime()이
        credentials를 runtime dir에 read-only로 주입한다. 정확한 키/파일은 Task 0.5.
        """
        rt = Path(runtime_dir)
        env = {k: os.environ[k] for k in self.AUTH_ENV_KEYS if k in os.environ}
        env.update({
            "HOME": str(rt),                       # 전역 ~/.* 미상속
            "XDG_CONFIG_HOME": str(rt / "config"),
            "CLAUDE_CONFIG_DIR": str(rt / "claude"),
            "CODEX_HOME": str(rt / "codex"),
        })
        return env

    def prepare_runtime(self, *, runtime_dir: str) -> None:
        """runtime config dir을 만들고 **인증 자격만** 주입(전역 rules/skills/MCP는 안 함).

        구현은 Task 0.5에서 확정한 인증 파일 경로를 read-only symlink로 건다.
        키체인 기반(파일 없음)이면 no-op이면 충분. review()/prescreen 호출 전에 1회.
        """
        rt = Path(runtime_dir)
        for sub in ("claude", "codex", "config"):
            (rt / sub).mkdir(parents=True, exist_ok=True)
        # 예(Task 0.5 확정 후 실제 경로로): 파일기반 인증이면 아래처럼 심링크.
        #   real = Path.home() / ".claude" / ".credentials.json"
        #   if real.exists():
        #       (rt / "claude" / ".credentials.json").symlink_to(real)
```

(파일 상단에 `import os` 추가.)

> **호출 계약:** 벤더 어댑터(`review`)와 prescreen은 실행 전에 `harness.prepare_runtime(runtime_dir=...)`를 호출해 인증을 주입한 뒤 `isolated_env`를 쓴다. `_execute_run`은 `TemporaryDirectory` 생성 직후 1회 호출, `build_deps._prescreen_tuple`도 임시 dir에 대해 호출.

> **⚠ 구현 입력 = Milestone 0.5 산출물:** `isolated_env`/`vendors.py`의 argv·env 키는 **`docs/vendor-cli-contract.md`(Task 0.5)에서 실증된 값**을 그대로 쓴다. 초판에서 지적된 "실플래그 확정을 E2E까지 미룸"을 0.5 선행으로 해소.
>
> **★개정 preflight 테스트(Task 3.3 Step4.5로 추가):** `isolated_env`로 띄운 CLI가 **전역 CLAUDE.md의 고유 마커를 모른다**는 것을 실제로 확인하는 테스트(`ALMIGHTY_E2E=1` opt-in). "로그인은 되지만 전역 rules/skills/MCP는 안 읽힌다"를 실증해야 격리 주장이 성립.

- [ ] **Step 5: 통과 확인 & 커밋**

Run: `pytest tests/test_harness.py -v` → PASS
```bash
git add server/review/harness.py harness/default/ tests/test_harness.py
git commit -m "feat: harness profile with isolated config dirs (no global profile)"
```

### Task 3.4: 벤더 어댑터(claude/codex 헤드리스, read-only)

**Files:**
- Create: `server/review/vendors.py`
- Test: `tests/test_vendors.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_vendors.py` (subprocess 주입 mock)

```python
import asyncio

from server.review.harness import HarnessProfile
from server.review.vendors import ClaudeAdapter, CodexAdapter


def fake_runner(stdout):
    calls = []
    async def run(args, env=None, cwd=None, timeout=None):  # ★개정: async
        calls.append({"args": args, "env": env, "cwd": cwd, "timeout": timeout})
        return stdout
    run.calls = calls
    return run


FAKE_OUT = ('분석 결과\n```json\n{"findings":[{"file":"a.py","line":2,'
            '"severity":"medium","category":"bug","claim":"c","rationale":"r",'
            '"confidence":0.6}]}\n```')


def test_claude_adapter_parses_findings(tmp_path):
    hp = HarnessProfile.load("default")
    runner = fake_runner(FAKE_OUT)
    adapter = ClaudeAdapter(runner=runner)
    fs = asyncio.run(adapter.review(prompt="리뷰해", workdir=tmp_path,
                                    harness=hp, runtime_dir=str(tmp_path / "rt")))
    assert fs[0].vendor == "claude"
    assert fs[0].file == "a.py"
    # read-only 격리 env 주입 + 전역 env 미상속(os.environ 통째 아님)
    assert "CLAUDE_CONFIG_DIR" in runner.calls[0]["env"]
    assert runner.calls[0]["timeout"] is not None


def test_codex_adapter_parses_findings(tmp_path):
    hp = HarnessProfile.load("default")
    runner = fake_runner(FAKE_OUT)
    adapter = CodexAdapter(runner=runner)
    fs = asyncio.run(adapter.review(prompt="리뷰해", workdir=tmp_path,
                                    harness=hp, runtime_dir=str(tmp_path / "rt")))
    assert fs[0].vendor == "codex"
    assert "CODEX_HOME" in runner.calls[0]["env"]
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_vendors.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `server/review/vendors.py`

```python
import asyncio
from pathlib import Path

from server.models import Finding
from server.review.findings_schema import (
    PROMPT_SCHEMA_HINT, parse_findings,
)
from server.review.harness import HarnessProfile

VENDOR_TIMEOUT_SEC = 600  # 벤더별 상한(rate-limit/hang 방어)


class VendorTimeout(RuntimeError):
    pass


async def _default_runner(args, env=None, cwd=None, timeout=None) -> str:
    """async subprocess — 이벤트루프 블록 금지(★개정). stdin은 반드시 닫음.

    (Task 0.5에서 확인: codex는 positional prompt를 줘도 stdin을 추가로 읽어
     닫지 않으면 무한 대기함 → stdin=DEVNULL 필수.)
    """
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise VendorTimeout(f"vendor timeout after {timeout}s") from e
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace")[:500])
    return out.decode(errors="replace")


class _BaseAdapter:
    vendor = ""

    def __init__(self, runner=_default_runner, timeout=VENDOR_TIMEOUT_SEC):
        self._run = runner
        self._timeout = timeout

    def _build_argv(self, prompt: str, hp: HarnessProfile) -> list[str]:
        raise NotImplementedError

    async def review(self, *, prompt: str, workdir: Path,
                     harness: HarnessProfile, runtime_dir: str) -> list[Finding]:
        full = f"{harness.system_prompt}\n\n{prompt}\n\n{PROMPT_SCHEMA_HINT}"
        env = harness.isolated_env(runtime_dir=runtime_dir)  # ★개정: allowlist env
        out = await self._run(
            self._build_argv(full, harness), env=env, cwd=str(workdir),
            timeout=self._timeout,
        )
        return parse_findings(out, vendor=self.vendor)


class ClaudeAdapter(_BaseAdapter):
    vendor = "claude"

    def _build_argv(self, prompt, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        tools = ",".join(hp.claude_allowed_tools)
        return [
            "claude", "-p", prompt,
            "--allowedTools", tools,
            "--model", hp.model,
        ]


class CodexAdapter(_BaseAdapter):
    vendor = "codex"

    def _build_argv(self, prompt, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        return [
            "codex", "exec", "--skip-git-repo-check",
            "--sandbox", hp.codex_sandbox, prompt,
        ]
```

- [ ] **Step 4: 통과 확인 & 커밋**

Run: `pytest tests/test_vendors.py -v` → PASS
```bash
git add server/review/vendors.py tests/test_vendors.py
git commit -m "feat: async read-only claude/codex adapters with timeout and closed stdin"
```

### Task 3.5: 사전 스크리닝(가벼운 모델, diff만)

**Files:**
- Create: `server/review/prescreen.py`
- Test: `tests/test_prescreen.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_prescreen.py`

```python
from server.review.prescreen import prescreen, PreScreenResult


FAKE = ('판단\n```json\n{"complexity":"moderate","score":0.5,'
        '"reason":"핵심 로직 변경"}\n```')


def test_prescreen_parses(tmp_path):
    def runner(args, env=None, cwd=None):
        assert "--model" in args  # 가벼운 모델 지정
        return FAKE
    res = prescreen(diff="diff...", model="haiku", runner=runner)
    assert isinstance(res, PreScreenResult)
    assert res.complexity == "moderate"
    assert res.reason


def test_prescreen_gate_decision():
    res = PreScreenResult(complexity="trivial", score=0.1, reason="오타")
    assert res.decide(threshold="moderate") == "skip"
    res2 = PreScreenResult(complexity="complex", score=0.9, reason="x")
    assert res2.decide(threshold="moderate") == "review"
```

- [ ] **Step 2: 실패 확인 → 구현** — `server/review/prescreen.py`

```python
import json
import re
import subprocess
from dataclasses import dataclass

_ORDER = {"trivial": 0, "moderate": 1, "complex": 2}
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class PreScreenResult:
    complexity: str      # trivial|moderate|complex
    score: float
    reason: str

    def decide(self, *, threshold: str) -> str:
        """threshold 미만 복잡도면 skip, 이상이면 review."""
        return "review" if _ORDER[self.complexity] >= _ORDER[threshold] else "skip"


PRESCREEN_TIMEOUT_SEC = 120  # ★개정: 사전평가도 subprocess → 상한 필수


def _default_runner(args, env=None, cwd=None) -> str:
    # ★개정: 벤더 계약과 동일하게 stdin 닫기 + timeout + (격리)env 적용.
    return subprocess.run(
        args, env=env, cwd=cwd, check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=PRESCREEN_TIMEOUT_SEC,
    ).stdout


PROMPT = (
    "다음 PR diff의 리뷰 필요성을 평가하라. 코드는 읽지 말고 diff만 근거로.\n"
    "마지막에 ```json {\"complexity\":trivial|moderate|complex,"
    "\"score\":0~1,\"reason\":\"한줄\"}``` 만 출력.\n\n"
)


def prescreen(*, diff: str, model: str, runner=_default_runner,
              env=None) -> PreScreenResult:
    """env를 넘기면 격리 config dir로 실행(build_deps가 하네스 env 주입)."""
    out = runner(["claude", "-p", PROMPT + diff, "--model", model], env=env)
    m = _FENCE.findall(out)
    if not m:
        return PreScreenResult("moderate", 0.5, "사전평가 파싱 실패→기본 리뷰")
    d = json.loads(m[-1])
    return PreScreenResult(
        complexity=d.get("complexity", "moderate"),
        score=float(d.get("score", 0.5)),
        reason=str(d.get("reason", "")),
    )
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_prescreen.py -v` → PASS
```bash
git add server/review/prescreen.py tests/test_prescreen.py
git commit -m "feat: lightweight pre-screen with complexity gate decision"
```

### Task 3.6: RunnerPool(세마포어) & 옵션 병합

**Files:**
- Create: `server/review/runner.py`
- Create: `server/review/merge.py`
- Test: `tests/test_runner.py`, `tests/test_merge.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_runner.py`

```python
import asyncio

from server.review.runner import RunnerPool


def test_semaphore_limits_concurrency():
    pool = RunnerPool(limit=2)
    active, peak = 0, 0

    async def job():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    async def main():
        return await asyncio.gather(*(pool.run(job) for _ in range(6)))

    results = asyncio.run(main())
    assert results == ["ok"] * 6
    assert peak <= 2
```

- [ ] **Step 2: 구현** — `server/review/runner.py`

```python
import asyncio


class RunnerPool:
    """동시성 = asyncio 세마포어 N. seam: 추후 분산큐로 교체 가능."""

    def __init__(self, limit: int = 2):
        self._sem = asyncio.Semaphore(limit)

    async def run(self, coro_factory):
        async with self._sem:
            return await coro_factory()
```

- [ ] **Step 3: 병합 실패 테스트** — `tests/test_merge.py`

```python
from server.models import Finding
from server.review.merge import deterministic_merge


def _f(vendor, file, line):
    return Finding(vendor, file, line, "high", "bug", "c", "r", 0.8)


def test_merge_tags_consensus_when_close():
    fs = [_f("claude", "a.py", 10), _f("codex", "a.py", 11)]
    merged = deterministic_merge(fs)
    assert all(m.consensus == "consensus" for m in merged) if hasattr(
        merged[0], "consensus") else True
    # consensus_group으로 묶였는지
    groups = {m.consensus_group_id for m in merged}
    assert len(groups) == 1


def test_merge_keeps_single_when_far():
    fs = [_f("claude", "a.py", 10), _f("codex", "b.py", 200)]
    merged = deterministic_merge(fs)
    assert len({m.consensus_group_id for m in merged}) == 2
```

- [ ] **Step 4: 구현** — `server/review/merge.py`

```python
from dataclasses import dataclass, field

from server.models import Finding

LINE_PROXIMITY = 5


@dataclass
class MergedFinding:
    finding: Finding
    consensus: str            # single|consensus
    consensus_group_id: int
    # 편의 위임
    def __getattr__(self, k):
        return getattr(self.finding, k)


def deterministic_merge(findings: list[Finding]) -> list[MergedFinding]:
    """(파일·라인근접·카테고리)로 CONSENSUS/SINGLE 태깅. LLM 미사용."""
    out: list[MergedFinding] = []
    groups: list[list[Finding]] = []
    for f in findings:
        placed = False
        for g in groups:
            h = g[0]
            if (h.file == f.file and h.category == f.category
                    and abs(h.line - f.line) <= LINE_PROXIMITY):
                g.append(f)
                placed = True
                break
        if not placed:
            groups.append([f])
    for gid, g in enumerate(groups):
        vendors = {x.vendor for x in g}
        tag = "consensus" if len(vendors) > 1 else "single"
        for f in g:
            out.append(MergedFinding(f, tag, gid))
    return out
```

- [ ] **Step 5: 통과 확인 & 커밋**

Run: `pytest tests/test_runner.py tests/test_merge.py -v` → PASS
```bash
git add server/review/runner.py server/review/merge.py \
        tests/test_runner.py tests/test_merge.py
git commit -m "feat: semaphore RunnerPool and optional deterministic merge"
```

---

## Milestone 4 — 오케스트레이션 파이프라인 · Seams · API · Poller · Worker

**목표:** §7 파이프라인(3~6 리뷰~저장)을 async로 엮고(RunnerPool 실배선), seam(ContextProvider no-op 등)을 배선하고, FastAPI 라우트(connection-per-request)·폴러(→job enqueue)·**worker 루프(job 소비)**를 노출. 요청 핸들러는 잡 enqueue만, 실행은 worker가 담당.

### Task 4.1: Seam 인터페이스(v1 no-op)

**Files:**
- Create: `server/seams.py`
- Test: `tests/test_seams.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_seams.py`

```python
from server.seams import NoOpContextProvider, LocalIdentity


def test_context_provider_noop_returns_empty():
    assert NoOpContextProvider().gather(repo="acme/api", pr_number=7) == ""


def test_identity_is_local_me():
    assert LocalIdentity().actor == "me"
```

- [ ] **Step 2: 구현** — `server/seams.py`

```python
from dataclasses import dataclass


class NoOpContextProvider:
    """v1 no-op. B에서 Jira/DB/Graphify 주입 지점."""

    def gather(self, *, repo: str, pr_number: int) -> str:
        return ""


@dataclass
class LocalIdentity:
    """v1 = 나 = 내 gh 시트. team-mode에서 per-user로 교체."""
    actor: str = "me"


class NullMemoryStore:
    """v1 = 저장만(no-op). C에서 학습 신호 소비."""

    def record(self, *, event: str, payload: dict) -> None:
        return None
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_seams.py -v` → PASS
```bash
git add server/seams.py tests/test_seams.py
git commit -m "feat: v1 no-op seams (ContextProvider, Identity, MemoryStore)"
```

### Task 4.2: 리뷰 파이프라인 조립

**Files:**
- Create: `server/pipeline.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_pipeline.py` (벤더/gh/worktree 주입으로 순수 오케스트레이션만 검증)

```python
import asyncio
from contextlib import contextmanager

import pytest

from server.models import Finding
from server.pipeline import review_pr, PipelineDeps, PipelineError
from server.repos import repo_repo, pr_repo, finding_repo, review_repo


@contextmanager
def fake_worktree(repo, sha):
    yield "/tmp/fake-wt"


class FakeAdapter:
    def __init__(self, vendor, findings):
        self.vendor = vendor
        self._f = findings
    async def review(self, **kw):  # ★개정: async
        return self._f


def test_pipeline_persists_findings_both_vendors(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=7, title="t", author="a",
                         head_sha="sha1", base_ref="main", url="u")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter("claude", [Finding("claude", "a.py", 1, "high",
                                           "bug", "c", "r", 0.8)]),
            FakeAdapter("codex", [Finding("codex", "b.py", 2, "low",
                                          "style", "c2", "r2", 0.4)]),
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    fs = finding_repo.list_for_run(db, run_id)
    vendors = {f["vendor"] for f in fs}
    assert vendors == {"claude", "codex"}
    assert pr_repo.get(db, pid)["last_reviewed_sha"] == "sha1"


def test_pipeline_skips_on_trivial_prescreen(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=8, title="t", author="a",
                         head_sha="s", base_ref="main", url="u")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "typo fix",
        worktree=fake_worktree, adapters=[],
        prescreen=lambda diff, model: ("trivial", 0.1, "오타"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="auto", deps=deps))
    # skip → run 상태 canceled, finding 없음
    assert review_repo.get_run(db, run_id)["status"] == "canceled"
    assert finding_repo.list_for_run(db, run_id) == []


def test_pipeline_fails_run_when_all_vendors_fail(db):
    """★개정 (codex v4 [HIGH]): 벤더 전원 실패면 run=failed + PipelineError.
    (rate-limit/auth 실패를 done으로 오판하지 않고 worker가 retry하도록)"""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=9, title="t", author="a",
                         head_sha="s9", base_ref="main", url="u")

    class FailAdapter:
        def __init__(self, vendor):
            self.vendor = vendor
        async def review(self, **kw):
            raise RuntimeError("rate limit exceeded")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[FailAdapter("claude"), FailAdapter("codex")],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError) as ei:
        asyncio.run(review_pr(db, pr_id=pid, trigger="auto", deps=deps))
    assert review_repo.get_run(db, ei.value.run_id)["status"] == "failed"
    assert "rate limit" in str(ei.value)          # retry 판정 근거 문자열 보존
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `server/pipeline.py`

```python
import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from server.config import DEFAULT_EFFORT
from server.repos import (
    finding_repo, prescreen_repo, pr_repo, repo_repo, review_repo,
    settings_repo,
)
from server.review.harness import HarnessProfile
from server.review.merge import deterministic_merge
from server.review.runner import RunnerPool
from server.seams import NoOpContextProvider


class PipelineError(RuntimeError):
    """★개정(codex v3): 실패 시 어느 attempt run이 실패했는지 worker에 전달."""
    def __init__(self, run_id: int, message: str):
        super().__init__(message)
        self.run_id = run_id


@dataclass
class PipelineDeps:
    gh_diff: Callable[[str, int], str]
    worktree: Callable            # contextmanager(repo, sha) -> path
    adapters: list                # vendor adapters (.vendor, async .review())
    prescreen: Callable[[str, str], tuple]  # (diff, model) -> (complexity, score, reason)
    repo_local_path: str
    context: object = field(default_factory=NoOpContextProvider)
    pool: RunnerPool = None       # ★개정: 벤더 병렬 실행 세마포어(없으면 생성)


async def review_pr(conn, *, pr_id: int, trigger: str,
                    deps: PipelineDeps) -> int:
    """run을 만들고 실행. ★개정: 예외 시 run을 failed로 마감 후 재던짐
    (review_run/review_job 상태 정합성). worker가 run_id를 몰라도 run은 스스로 정리됨."""
    pr = pr_repo.get(conn, pr_id)
    repo = repo_repo.get(conn, pr["repo_id"])
    settings = settings_repo.get(conn)
    run_id = review_repo.create_run(
        conn, pr_id=pr_id, head_sha=pr["head_sha"], trigger=trigger,
        effort=repo["default_effort"] or DEFAULT_EFFORT,
        merge_enabled=repo["merge_enabled"],
    )
    try:
        await _execute_run(conn, run_id=run_id, pr=pr, repo=repo,
                           settings=settings, deps=deps)
    except Exception as e:
        review_repo.finish_run(conn, run_id, "failed", error=str(e))
        raise PipelineError(run_id, str(e)) from e  # ★개정: run_id 전달
    return run_id
```

> **★개정 상태 정합성 정책 (codex v3 [HIGH]):** **1 attempt = 1 review_run**. 실패한 attempt는 `review_run.status='failed'`로 이력에 남고, `review_job`은 backoff 후 **다음 attempt에서 새 run을 생성**해 재시도한다. `review_job.run_id`는 항상 **최신 attempt의 run**을 가리키므로(성공/실패 무관, worker가 기록), failed run과 retry job이 갈라지지 않는다. 대시보드는 PR의 run 이력으로 실패 attempt들을 보여준다.

```python
# (참고) review_pr 본체는 위 정책대로 _execute_run을 감싼 형태. 아래는 _execute_run.


async def _execute_run(conn, *, run_id, pr, repo, settings, deps) -> None:
    hp = HarnessProfile.load(repo["harness_name"])
    pool = deps.pool or RunnerPool(limit=settings["concurrency_limit"])

    # sync subprocess(gh/prescreen)를 to_thread로 오프로드 → 이벤트루프 비블록
    diff = await asyncio.to_thread(deps.gh_diff, repo["full_name"], pr["number"])

    # 2. Pre-screen
    complexity, score, reason = await asyncio.to_thread(
        deps.prescreen, diff, hp.prescreen_model)
    from server.review.prescreen import PreScreenResult
    ps = PreScreenResult(complexity, score, reason)
    decided = ps.decide(threshold=settings["prescreen_gate_threshold"])
    prescreen_repo.add(conn, pr_id=pr["id"], head_sha=pr["head_sha"],
                       model=hp.prescreen_model, complexity=complexity,
                       score=score, reason=reason, duration_ms=0,
                       decided=decided)
    if decided == "skip" and repo["trigger_mode"] == "auto":
        review_repo.finish_run(conn, run_id, "canceled")
        pr_repo.mark_reviewed(conn, pr["id"], pr["head_sha"])
        return

    # ★개정 (codex v5 [LOW]): enabled 벤더가 0개면 리뷰할 게 없다 → worktree도
    # 만들지 않고 canceled로 마감(reviewed로 오판하지 않음). trigger/설정 단계에서
    # 걸러지는 게 정상이나 방어적으로 여기서도 canceled 처리.
    adapters = _enabled_adapters(deps.adapters, repo)
    if not adapters:
        review_repo.finish_run(conn, run_id, "canceled",
                               error="no vendor enabled")
        return

    # 3. Prepare + 4. Review — 벤더 병렬(RunnerPool+gather), 실패 격리
    prompt = _build_prompt(pr, diff, deps.context)
    with deps.worktree(Path(deps.repo_local_path), pr["head_sha"]) as wt:
        with tempfile.TemporaryDirectory(prefix="almighty-rt-") as rt:
            hp.prepare_runtime(runtime_dir=rt)   # ★개정: 인증 주입(전역 미상속 유지)
            vr_ids = {
                ad.vendor: review_repo.add_vendor_result(
                    conn, run_id=run_id, vendor=ad.vendor, status="running")
                for ad in adapters
            }

            async def _run_one(ad):
                async def job():
                    return await ad.review(prompt=prompt, workdir=Path(str(wt)),
                                           harness=hp, runtime_dir=rt)
                try:
                    fs = await pool.run(job)
                    return ad.vendor, fs, None
                except Exception as e:  # 한 벤더 실패가 다른 벤더를 막지 않음
                    return ad.vendor, [], str(e)

            results = await asyncio.gather(*(_run_one(a) for a in adapters))

    all_findings = []  # (vendor_result_id, finding)
    succeeded, errors = 0, []
    for vendor, fs, err in results:
        vr_id = vr_ids[vendor]
        if err is not None:
            errors.append(f"{vendor}: {err}")
            conn.execute("UPDATE vendor_result SET status='failed', error=? "
                         "WHERE id=?", (err, vr_id))
        else:
            succeeded += 1
            conn.execute("UPDATE vendor_result SET status='done' WHERE id=?",
                         (vr_id,))
            for f in fs:
                f.vendor_result_id = vr_id      # ★개정: id() 매핑 제거, 명시 부착
                all_findings.append((vr_id, f))
    conn.commit()

    # ★개정 (codex v4 [HIGH]): enabled 벤더가 **전원 실패**면 run을 done으로
    # 오판하지 않는다. 예외로 승격 → review_pr가 run을 failed로 마감하고
    # PipelineError로 감싸 worker가 rate/timeout 시 retry한다.
    if succeeded == 0:
        raise RuntimeError("all vendors failed → " + "; ".join(errors))

    # 5. (옵션) Merge — vendor_result_id를 MergedFinding 위임으로 복원
    if repo["merge_enabled"]:
        merged = deterministic_merge([f for _, f in all_findings])
        _persist(conn, run_id,
                 [(getattr(m, "vendor_result_id", None), m) for m in merged])
    else:
        _persist(conn, run_id, all_findings)

    # 6. Persist done
    # ★정책 (codex v5·v6 [MEDIUM]): **부분 성공 = done**(≥1 벤더 성공). 실패 벤더는
    # vendor_result.status='failed'로 남고 **v1은 개별 벤더 자동 재시도 없음**
    # (전원 실패만 재시도). 노출 경로 = `/api/runs/{id}/vendor-results` +
    # ReviewSection 실패 배지(Task 6.2). 사용자는 이를 보고 수동 재리뷰로
    # 재실행(양 벤더 재실행). 벤더별 follow-up 자동 재시도는 v-next.
    review_repo.finish_run(conn, run_id, "done")
    pr_repo.mark_reviewed(conn, pr["id"], pr["head_sha"])


def _enabled_adapters(adapters, repo):
    out = []
    for ad in adapters:
        if ad.vendor == "claude" and not repo["vendor_claude_on"]:
            continue
        if ad.vendor == "codex" and not repo["vendor_codex_on"]:
            continue
        out.append(ad)
    return out


def _persist(conn, run_id, items):
    for vr_id, f in items:
        finding_repo.add(
            conn, run_id=run_id, vendor_result_id=vr_id, vendor=f.vendor,
            file=f.file, line=f.line, severity=f.severity, category=f.category,
            claim=f.claim, rationale=f.rationale, confidence=f.confidence,
            consensus=getattr(f, "consensus", "single"),
            consensus_group_id=getattr(f, "consensus_group_id", None))


def _build_prompt(pr, diff, context) -> str:
    ctx = context.gather(repo="", pr_number=pr["number"])  # v1 = ""
    ctx_block = f"\n\n## 외부 컨텍스트\n{ctx}" if ctx else ""
    return (
        f"# PR #{pr['number']}: {pr['title']}\n작성자: {pr['author']}\n"
        f"{ctx_block}\n\n## Diff\n```diff\n{diff}\n```\n"
        "필요하면 레포를 읽어 맥락을 확인하라(수정 금지)."
    )
```

> **★개정 반영:** (1) `finish_run # no-op ref` 잔재 제거, (2) 벤더는 `RunnerPool`+`asyncio.gather`로 병렬·**실패 격리**(초판 순차 for-loop → seam 실제 사용), 단 **전원 실패 시엔 run을 failed로 승격**(codex v4 [HIGH]), (3) merge 경로는 `Finding.vendor_result_id` 명시 필드로 벤더 추적성 유지(`id()` 의존 제거). `MergedFinding`은 `finding` 속성 위임(`__getattr__`)으로 `vendor_result_id`를 그대로 노출(Task 3.6 정의).

- [ ] **Step 4: 통과 확인 & 커밋**

Run: `pytest tests/test_pipeline.py -v` → PASS (3 passed)
```bash
git add server/pipeline.py tests/test_pipeline.py
git commit -m "feat: async review pipeline with parallel vendors via RunnerPool"
```

### Task 4.3: FastAPI 라우트

**Files:**
- Modify: `server/api.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_api.py`

```python
from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema


def _client(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def test_add_and_list_repos(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/repos", json={"full_name": "acme/api"})
    assert r.status_code == 201
    lst = client.get("/api/repos").json()
    assert lst[0]["full_name"] == "acme/api"


def test_get_settings(tmp_path):
    client, _ = _client(tmp_path)
    s = client.get("/api/settings").json()
    assert s["concurrency_limit"] == 2


def test_update_finding_status(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import repo_repo, pr_repo, finding_repo, review_repo
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(conn, repo_id=rid, number=1, title="t", author="a",
                         head_sha="s", base_ref="main", url="u")
    run_id = review_repo.create_run(conn, pr_id=pid, head_sha="s",
                                    trigger="manual", effort="medium")
    fid = finding_repo.add(conn, run_id=run_id, vendor="claude", file="a",
                           line=1, severity="high", category="bug",
                           claim="c", rationale="r", confidence=0.8)
    r = client.patch(f"/api/findings/{fid}",
                     json={"status": "approved"})
    assert r.status_code == 200
    assert finding_repo.get(conn, fid)["status"] == "approved"
```

- [ ] **Step 2: 구현** — `server/api.py` (health 유지 + 라우트 추가)

```python
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from server import config
from server.db import connect, init_schema
from server.repos import (
    finding_repo, repo_repo, review_repo, settings_repo,
)

app = FastAPI(title="Almighty PR Review Server")

_initialized = False


def _ensure_schema():
    global _initialized
    if not _initialized:
        conn = connect(config.DB_PATH)
        init_schema(conn)
        conn.close()
        _initialized = True


def get_conn():
    """★개정: 요청마다 커넥션 open/close. 전역 단일 커넥션 금지
    (sqlite3 check_same_thread + FastAPI 스레드풀 충돌 회피). WAL로 동시성 확보."""
    _ensure_schema()
    conn = connect(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


class RepoIn(BaseModel):
    full_name: str
    local_path: str | None = None  # ★개정: worktree 소스 경로


@app.post("/api/repos", status_code=201)
def add_repo(body: RepoIn, conn=Depends(get_conn)):
    rid = repo_repo.add(conn, full_name=body.full_name,
                        local_path=body.local_path)
    return dict(repo_repo.get(conn, rid))


@app.get("/api/repos")
def list_repos(conn=Depends(get_conn)):
    return [dict(r) for r in conn.execute("SELECT * FROM repo").fetchall()]


class RepoPatch(BaseModel):
    enabled: int | None = None
    trigger_mode: str | None = None
    default_effort: str | None = None
    vendor_claude_on: int | None = None
    vendor_codex_on: int | None = None
    merge_enabled: int | None = None
    auto_post: int | None = None
    harness_name: str | None = None
    local_path: str | None = None  # ★개정


@app.patch("/api/repos/{rid}")
def patch_repo(rid: int, body: RepoPatch, conn=Depends(get_conn)):
    repo_repo.update(conn, rid, **body.model_dump(exclude_none=True))
    return dict(repo_repo.get(conn, rid))


@app.get("/api/settings")
def get_settings(conn=Depends(get_conn)):
    return dict(settings_repo.get(conn))


class SettingsPatch(BaseModel):
    default_effort: str | None = None
    concurrency_limit: int | None = None
    default_poll_interval: int | None = None
    approval_gate_on: int | None = None
    prescreen_model: str | None = None
    prescreen_gate_threshold: str | None = None


@app.patch("/api/settings")
def patch_settings(body: SettingsPatch, conn=Depends(get_conn)):
    settings_repo.update(conn, **body.model_dump(exclude_none=True))
    return dict(settings_repo.get(conn))


@app.get("/api/runs/{run_id}/findings")
def run_findings(run_id: int, conn=Depends(get_conn)):
    return [dict(f) for f in finding_repo.list_for_run(conn, run_id)]


@app.get("/api/runs/{run_id}/vendor-results")
def run_vendor_results(run_id: int, conn=Depends(get_conn)):
    # ★개정 (codex v6 [MEDIUM]): 실패 벤더 노출용. 프론트 ReviewSection이
    # status='failed' 벤더에 배지를 띄워 부분 실패를 사용자에게 알린다.
    return [dict(v) for v in review_repo.list_vendor_results(conn, run_id)]


class FindingPatch(BaseModel):
    status: str
    edited_text: str | None = None


@app.patch("/api/findings/{fid}")
def patch_finding(fid: int, body: FindingPatch, conn=Depends(get_conn)):
    finding_repo.set_status(conn, fid, body.status, body.edited_text)
    return dict(finding_repo.get(conn, fid))
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_api.py -v` → PASS (3 passed)
```bash
git add server/api.py tests/test_api.py
git commit -m "feat: FastAPI routes for repos, settings, findings triage"
```

### Task 4.4: 백그라운드 폴러

**Files:**
- Create: `server/poller.py`
- Modify: `server/api.py` (startup에서 폴러 태스크 기동)
- Test: `tests/test_poller.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_poller.py`

```python
from server.poller import poll_once
from server.repos import repo_repo, pr_repo
from server.github.gh import PrInfo


def test_poll_once_upserts_new_prs(db):
    rid = repo_repo.add(db, full_name="acme/api")
    fake_prs = [PrInfo(7, "t", "kim", "sha1", "main", "u", "open")]
    enqueued = []
    poll_once(db,
              list_prs=lambda repo: fake_prs,
              enqueue=lambda pr_id: enqueued.append(pr_id))
    # PR upsert + head_sha != last_reviewed_sha → enqueue
    pid = pr_repo.get(db, 1)["id"]
    assert enqueued == [pid]


def test_poll_once_skips_already_reviewed(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(db, repo_id=rid, number=7, title="t", author="a",
                         head_sha="sha1", base_ref="main", url="u")
    pr_repo.mark_reviewed(db, pid, "sha1")
    enqueued = []
    poll_once(db,
              list_prs=lambda repo: [PrInfo(7, "t", "a", "sha1", "main",
                                            "u", "open")],
              enqueue=lambda pr_id: enqueued.append(pr_id))
    assert enqueued == []  # 같은 sha → skip


def test_poll_once_no_vendor_upserts_pr_but_skips_enqueue(db):
    # ★개정 (codex v6/v7 [MEDIUM]): 벤더 0개 레포도 PR은 발견·upsert(오버뷰 표시)
    # 하되 enqueue만 막는다(재감지 루프 차단). 벤더를 켜면 다음 폴링에 enqueue.
    rid = repo_repo.add(db, full_name="acme/api")
    repo_repo.update(db, rid, vendor_claude_on=0, vendor_codex_on=0)
    prs = [PrInfo(7, "t", "a", "sha1", "main", "u", "open")]
    enqueued = []
    poll_once(db, list_prs=lambda repo: prs,
              enqueue=lambda pr_id: enqueued.append(pr_id))
    assert enqueued == []                          # 벤더 0개 → enqueue 안 함
    assert pr_repo.get(db, 1)["head_sha"] == "sha1"  # PR은 upsert됨(오버뷰 노출)

    # 벤더 재활성화 → 같은 head_sha가 다음 폴링에 정상 enqueue
    repo_repo.update(db, rid, vendor_claude_on=1)
    poll_once(db, list_prs=lambda repo: prs,
              enqueue=lambda pr_id: enqueued.append(pr_id))
    assert enqueued == [1]                          # 재활성화 후 enqueue 성립
```

- [ ] **Step 2: 구현** — `server/poller.py`

```python
import asyncio

from server.github.gh import GhClient
from server.repos import pr_repo, repo_repo


def poll_once(conn, *, list_prs, enqueue) -> None:
    """enabled 레포별로 open PR을 upsert하고 새 head sha면 enqueue."""
    for repo in repo_repo.list_enabled(conn):
        if repo["trigger_mode"] != "auto":
            continue
        # ★개정 (codex v6/v7 [MEDIUM]): PR 발견·upsert·오버뷰·last_polled_at은
        # 항상 수행하고, **enqueue만** 벤더 유무로 가드한다. (벤더 0개 레포도
        # PR은 오버뷰에 뜨되 리뷰 job은 안 쌓임 → 재감지 루프 차단 + 발견 유지.
        # 나중에 벤더를 켜면 needs_review가 여전히 true라 다음 폴링에 enqueue됨)
        has_vendor = repo["vendor_claude_on"] or repo["vendor_codex_on"]
        for pr in list_prs(repo["full_name"]):
            pid = pr_repo.upsert(
                conn, repo_id=repo["id"], number=pr.number, title=pr.title,
                author=pr.author, head_sha=pr.head_sha, base_ref=pr.base_ref,
                url=pr.url, state=pr.state)
            if has_vendor and pr_repo.needs_review(conn, pid):
                enqueue(pid)
        repo_repo.update(conn, repo["id"],
                         last_polled_at=_now(conn))


def _now(conn):
    return conn.execute("SELECT datetime('now') AS n").fetchone()["n"]


async def poll_loop(db_path, *, interval_sec: int = 60, stop_event=None):
    """★개정: 폴러는 매 틱 자기 커넥션을 열고, 새 head sha면 review_job enqueue."""
    from server.db import connect
    from server.repos import job_repo
    client = GhClient()
    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
        try:
            def enqueue(pid):
                pr = pr_repo.get(conn, pid)
                job_repo.enqueue(conn, pr_id=pid, head_sha=pr["head_sha"],
                                 trigger="auto")
            poll_once(conn, list_prs=client.list_open_prs, enqueue=enqueue)
        finally:
            conn.close()
        # ★개정: interval 대기 중에도 stop_event에 즉시 반응(graceful shutdown)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_sec)
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_poller.py -v` → PASS
```bash
git add server/poller.py tests/test_poller.py
git commit -m "feat: polling loop that enqueues review_job on new head sha"
```

### Task 4.5: Worker 루프 (잡 소비) ★개정

**Files:**
- Create: `server/worker.py`
- Test: `tests/test_worker.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_worker.py`

```python
import asyncio

from server.worker import run_one_job
from server.repos import repo_repo, pr_repo, job_repo, review_repo


def test_worker_claims_and_runs_job(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(db, repo_id=rid, number=1, title="t", author="a",
                         head_sha="s1", base_ref="main", url="u")
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")

    async def fake_review_pr(conn, *, pr_id, trigger, deps):
        return review_repo.create_run(conn, pr_id=pr_id, head_sha="s1",
                                      trigger=trigger, effort="medium")

    monkeypatch.setattr("server.worker.review_pr", fake_review_pr)
    claimed = asyncio.run(run_one_job(db, worker_id="w1"))
    assert claimed is True
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "done" and j["run_id"] is not None


def test_worker_marks_failed_with_retry(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(db, repo_id=rid, number=2, title="t", author="a",
                         head_sha="s2", base_ref="main", url="u")
    job_repo.enqueue(db, pr_id=pid, head_sha="s2", trigger="auto")

    async def boom(conn, *, pr_id, trigger, deps):
        raise RuntimeError("rate limit")

    monkeypatch.setattr("server.worker.review_pr", boom)
    asyncio.run(run_one_job(db, worker_id="w1"))
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "queued" and j["attempts"] == 1  # 재시도 예약


def test_worker_records_failed_run_id_on_pipeline_error(db, monkeypatch):
    """★개정 (codex v5 [LOW]): review_pr가 PipelineError(run_id)를 던지면
    worker가 그 실패 attempt run을 retry job에 기록하는 통합 경로 검증."""
    from server.pipeline import PipelineError
    from server.repos import review_repo
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(db, repo_id=rid, number=3, title="t", author="a",
                         head_sha="s3", base_ref="main", url="u")
    job_repo.enqueue(db, pr_id=pid, head_sha="s3", trigger="auto")

    async def boom(conn, *, pr_id, trigger, deps):
        run_id = review_repo.create_run(conn, pr_id=pr_id, head_sha="s3",
                                        trigger=trigger, effort="medium")
        review_repo.finish_run(conn, run_id, "failed", error="all vendors failed")
        raise PipelineError(run_id, "all vendors failed → rate limit")

    # ★개정 (codex v6 [LOW]): build_deps는 real 호출을 피해 monkeypatch(환경 비의존).
    monkeypatch.setattr("server.worker.build_deps", lambda repo: None)
    monkeypatch.setattr("server.worker.review_pr", boom)
    asyncio.run(run_one_job(db, worker_id="w1"))
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "queued"                       # rate → retry 예약
    assert j["run_id"] is not None                       # 실패 attempt run 기록
    assert review_repo.get_run(db, j["run_id"])["status"] == "failed"
```

- [ ] **Step 2: 구현** — `server/worker.py`

```python
import asyncio

from server.pipeline import PipelineDeps, PipelineError, review_pr
from server.repos import job_repo, pr_repo, repo_repo
from server.review.gh_deps import build_deps  # Task 7.2에서 정의


async def run_one_job(conn, *, worker_id: str) -> bool:
    """queued 잡 1건을 claim해 실행. 처리했으면 True."""
    job = job_repo.claim_next(conn, worker_id=worker_id)
    if job is None:
        return False
    pr = pr_repo.get(conn, job["pr_id"])
    repo = repo_repo.get(conn, pr["repo_id"])
    try:
        deps = build_deps(repo)
        run_id = await review_pr(conn, pr_id=job["pr_id"],
                                 trigger=job["trigger"], deps=deps)
        job_repo.mark_done(conn, job["id"], run_id)
    except PipelineError as e:
        # ★개정 (codex v3 [HIGH]): 실패한 attempt의 run_id를 job에 기록해
        # failed run과 retry job이 갈라지지 않게 한다.
        retry = "rate" in str(e).lower() or "timeout" in str(e).lower()
        job_repo.mark_failed(conn, job["id"], error=str(e),
                             retry=retry, run_id=e.run_id)
    except Exception as e:
        # run 생성 이전(build_deps/claim 직후) 실패 → 연결된 run 없음.
        retry = "rate" in str(e).lower() or "timeout" in str(e).lower()
        job_repo.mark_failed(conn, job["id"], error=str(e),
                             retry=retry, run_id=None)
    return True


async def worker_loop(db_path, *, worker_id="w1", idle_sleep=2.0, stop_event=None):
    from server.db import connect
    # ★개정: 시작 시 이전 크래시로 고착된 running 잡을 queued로 복구.
    boot = connect(db_path)
    try:
        n = job_repo.recover_stale(boot)
        if n:
            print(f"[worker] recovered {n} stale jobs")
    finally:
        boot.close()

    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
        try:
            worked = await run_one_job(conn, worker_id=worker_id)
        finally:
            conn.close()
        if not worked:
            # ★개정 (codex v3 [MEDIUM]): stop_event 없으면 plain sleep으로 idle.
            # (stop_event.wait()에 AttributeError를 내며 busy loop 도는 것 방지)
            if stop_event is None:
                await asyncio.sleep(idle_sleep)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep)
                except asyncio.TimeoutError:
                    pass  # idle 대기 만료 → 다음 tick
```

> **★개정 (codex 재검증 [HIGH]):** worker 시작 시 `recover_stale`로 stale lock 회수. idle 대기를 `stop_event.wait()` 타임아웃으로 바꿔 **shutdown 신호에 즉시 반응**(취소 지연 제거).
>
> **★계약 (codex v4 [LOW] 2건):**
> - **`stop_event` 주입 필수** — 운영(lifespan Task 7.2)은 항상 `stop_event`를 넘긴다. `stop_event=None`은 테스트/수동 전용이며 **의도적으로 무한 루프**다. 이 경로를 직접 돌릴 땐 `asyncio.wait_for(worker_loop(...), timeout=...)`나 `task.cancel()`로 감싸 종료를 보장한다(테스트는 `run_one_job`만 직접 호출해 루프를 피함).
> - **`job.status='done'`의 의미 = 스케줄러 완료**(리뷰 성공과 별개). prescreen skip은 `review_run.status='canceled'`로 정상 return하고 worker는 그 run_id로 job을 `done` 처리한다 → "잡은 정상 처리됐고, 리뷰 결과는 review_run.status로 판별". 대시보드/API는 **리뷰 완료 여부를 `review_run.status`로** 읽는다(job.status로 읽지 않음).

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_worker.py -v` → PASS (3 passed)
```bash
git add server/worker.py tests/test_worker.py
git commit -m "feat: worker loop consuming review_job queue with retry"
```

---

## Milestone 5 — 구조화 코멘트 포맷터 & 승인 포스팅

**목표:** 승인 findings를 사람·AI 겸용 구조화 마크다운으로 만들고(§7 Step8), 마커 포함해 gh로 포스팅, posted_comment 기록.

### Task 5.1: 구조화 마크다운 포맷터

**Files:**
- Create: `server/formatter.py`
- Test: `tests/test_formatter.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_formatter.py`

```python
from server.models import Finding
from server.formatter import build_comment, MARKER


def test_comment_has_marker_summary_and_parse_block():
    fs = [
        Finding("claude", "a.py", 12, "high", "bug", "널 역참조",
                "x가 None일 수 있음", 0.8),
        Finding("claude", "b.py", 3, "low", "style", "네이밍", "사소", 0.3),
    ]
    body = build_comment(vendor="claude", findings=fs)
    assert MARKER.format(vendor="claude") in body
    assert "high" in body and "a.py:12" in body
    # 말미 파싱용 구조 블록(학습루프 소비)
    assert "```json" in body
    assert "널 역참조" in body


def test_empty_findings_says_clean():
    body = build_comment(vendor="codex", findings=[])
    assert "발견된 이슈 없음" in body
```

- [ ] **Step 2: 구현** — `server/formatter.py`

```python
import json

from server.models import Finding

MARKER = "<!-- almighty-review [{vendor}] -->"
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def build_comment(*, vendor: str, findings: list[Finding]) -> str:
    marker = MARKER.format(vendor=vendor)
    if not findings:
        return (f"{marker}\n## 🤖 {vendor} 리뷰\n\n발견된 이슈 없음. ✅\n")
    ordered = sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))
    top = ordered[0].severity
    lines = [
        marker,
        f"## 🤖 {vendor} 리뷰",
        f"> 요약: **{len(findings)}건** · 최고 severity **{top}**\n",
    ]
    for f in ordered:
        lines.append(
            f"### [{f.severity}] `{f.file}:{f.line}` — {f.category}\n"
            f"- **주장:** {f.claim}\n"
            f"- **근거:** {f.rationale}\n"
            f"- **확신도:** {f.confidence:.2f}\n"
        )
    parse_block = {
        "vendor": vendor,
        "findings": [
            {"file": f.file, "line": f.line, "severity": f.severity,
             "category": f.category, "claim": f.claim,
             "rationale": f.rationale, "confidence": f.confidence}
            for f in ordered
        ],
    }
    lines.append(
        "\n<details><summary>machine-readable</summary>\n\n"
        "```json\n" + json.dumps(parse_block, ensure_ascii=False, indent=2)
        + "\n```\n</details>"
    )
    return "\n".join(lines)
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_formatter.py -v` → PASS
```bash
git add server/formatter.py tests/test_formatter.py
git commit -m "feat: dual human/AI structured markdown comment formatter"
```

### Task 5.2: 승인분 포스팅 엔드포인트

**Files:**
- Modify: `server/api.py` (POST `/api/runs/{run_id}/post`)
- Test: `tests/test_post.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_post.py`

```python
from fastapi.testclient import TestClient

from server.api import app, get_conn, get_gh
from server.db import connect, init_schema
from server.repos import repo_repo, pr_repo, review_repo, finding_repo


def test_post_only_approved_findings(tmp_path):
    conn = connect(tmp_path / "p.db")
    init_schema(conn)
    posted = []

    class FakeGh:
        def post_comment(self, repo, number, body):
            posted.append((repo, number, body))
            return {"id": 1, "html_url": "https://x/1#issuecomment-1"}

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(conn, repo_id=rid, number=5, title="t", author="a",
                         head_sha="s", base_ref="main", url="u")
    run_id = review_repo.create_run(conn, pr_id=pid, head_sha="s",
                                    trigger="manual", effort="medium")
    f_ok = finding_repo.add(conn, run_id=run_id, vendor="claude", file="a.py",
                            line=1, severity="high", category="bug",
                            claim="c", rationale="r", confidence=0.9)
    f_no = finding_repo.add(conn, run_id=run_id, vendor="claude", file="b.py",
                            line=2, severity="low", category="style",
                            claim="c2", rationale="r2", confidence=0.2)
    finding_repo.set_status(conn, f_ok, "approved")
    finding_repo.set_status(conn, f_no, "dismissed")

    r = client.post(f"/api/runs/{run_id}/post")
    assert r.status_code == 200
    # 승인분만 코멘트 본문에 포함
    assert posted and "a.py:1" in posted[0][2]
    assert "b.py:2" not in posted[0][2]
    # 포스팅된 finding은 status=posted
    assert finding_repo.get(conn, f_ok)["status"] == "posted"
```

- [ ] **Step 2: 구현** — `server/api.py`에 추가

```python
from server.formatter import MARKER, build_comment
from server.github.gh import GhClient
from server.models import Finding
from server.repos import posted_repo, pr_repo

_gh = None


def get_gh():
    global _gh
    if _gh is None:
        _gh = GhClient()
    return _gh


@app.post("/api/runs/{run_id}/post")
def post_run(run_id: int, conn=Depends(get_conn), gh=Depends(get_gh)):
    run = review_repo.get_run(conn, run_id)
    pr = pr_repo.get(conn, run["pr_id"])
    repo = repo_repo.get(conn, pr["repo_id"])
    rows = [f for f in finding_repo.list_for_run(conn, run_id)
            if f["status"] == "approved"]
    posted = []
    by_vendor: dict[str, list[Finding]] = {}
    for f in rows:
        by_vendor.setdefault(f["vendor"], []).append(
            Finding(f["vendor"], f["file"], f["line"], f["severity"],
                    f["category"], f["edited_text"] or f["claim"],
                    f["rationale"], f["confidence"] or 0.0))
    for vendor, findings in by_vendor.items():
        body = build_comment(vendor=vendor, findings=findings)
        # ★개정(codex 재검증 [MEDIUM]): 진짜 update-or-create.
        # 같은 PR·벤더의 기존 비대체 코멘트가 있으면 GitHub상 in-place 수정,
        # 없으면 새로 post. → GitHub 화면에도 중복이 남지 않음.
        prev = posted_repo.latest_for_pr_vendor(conn, pr_id=pr["id"], vendor=vendor)
        fids = [f["id"] for f in rows if f["vendor"] == vendor]
        if prev is not None and prev["github_comment_id"]:
            res = gh.edit_comment(repo["full_name"], prev["github_comment_id"], body)
            posted_repo.supersede(conn, prev["id"])
        else:
            res = gh.post_comment(repo["full_name"], pr["number"], body)
        # ★개정 (codex v3 [LOW]): API JSON의 .id를 그대로 저장(URL 파싱 제거).
        posted_repo.add(conn, run_id=run_id, vendor=vendor,
                        github_comment_id=str(res["id"]), url=res["html_url"],
                        marker=MARKER.format(vendor=vendor), body=body,
                        head_sha=run["head_sha"], finding_ids=fids)
        for f in rows:
            if f["vendor"] == vendor:
                finding_repo.set_status(conn, f["id"], "posted")
        posted.append({"vendor": vendor, "url": res["html_url"]})
    return {"posted": posted}
```

> **★개정:** 기존 마커 코멘트를 `gh api PATCH`로 in-place 수정 → GitHub 화면에도 벤더당 코멘트 1건만 유지(재리뷰 시 갱신). `posted_comment.head_sha`로 리뷰본 추적, 로컬은 이전 row를 supersede.

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_post.py -v` → PASS
```bash
git add server/api.py tests/test_post.py
git commit -m "feat: post approved findings with update-or-create supersede policy"
```

---

## Milestone 6 — 프론트엔드 대시보드(확장형 앱 셸)

**목표:** §8 확정 방향 = 라이트테마 · 좌측 nav 목록 · 리뷰 섹션(레포 탭 + 오버뷰↔디테일 드릴) · 설정 · 하네스편집 · 위키/자가학습 스텁 · 한글 가독성. 승인된 디자인 초안 `docs/design-drafts/variant-app.html` 구조를 React로 이식.

> **참조:** 컴포넌트 계층·라벨·색은 `variant-app.html`을 그대로 따른다(nav 6항목, drill 인터랙션). 초안을 열어 마크업/클래스를 복사 기준으로 삼는다.

### Task 6.1: Vite React 스캐폴드 + 앱 셸 + 테마

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/index.html`, `web/tsconfig.json`
- Create: `web/src/main.tsx`, `web/src/App.tsx`, `web/src/api.ts`, `web/src/theme.css`
- Test: `web/src/App.test.tsx` (Vitest)

- [ ] **Step 1: 스캐폴드**

Run:
```bash
cd web
npm create vite@latest . -- --template react-ts   # 이미 파일 있으면 수동 구성
npm install
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```
Expected: `web/node_modules` 설치, dev 서버 기동 가능.

- [ ] **Step 2: `web/vite.config.ts` — 백엔드 프록시 + vitest**

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8787" } },
  test: { environment: "jsdom", globals: true, setupFiles: "./src/setup.ts" },
});
```
그리고 `web/src/setup.ts`: `import "@testing-library/jest-dom";`

- [ ] **Step 3: `web/src/theme.css` — 라이트테마 + 한글 타이포(§8 확정)**

```css
:root {
  --bg: #f7f8fa; --panel: #ffffff; --border: #e3e6eb;
  --text: #1f2430; --muted: #5b6472; --accent: #2f6feb;
  --sev-critical: #c0392b; --sev-high: #e67e22;
  --sev-medium: #d4a017; --sev-low: #7f8c8d;
}
html, body, #root { height: 100%; margin: 0; background: var(--bg); }
body {
  color: var(--text);
  /* FHD 외부 디스플레이 한글 가독성: 시스템 한글 폰트, 외부폰트 금지 */
  font-family: "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif;
  font-size: 14.5px; line-height: 1.62; font-weight: 450;
  -webkit-font-smoothing: antialiased;
}
.app { display: grid; grid-template-columns: 240px 1fr; height: 100%; }
.nav { background: var(--panel); border-right: 1px solid var(--border); padding: 12px; }
.nav-item { padding: 9px 12px; border-radius: 8px; cursor: pointer; color: var(--muted); }
.nav-item.active { background: #eef3ff; color: var(--accent); font-weight: 600; }
.content { padding: 20px 28px; overflow: auto; }
.badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
```

- [ ] **Step 4: 실패 테스트** — `web/src/App.test.tsx`

```tsx
import { render, screen } from "@testing-library/react";
import App from "./App";

test("renders nav sections", () => {
  render(<App />);
  expect(screen.getByText("리뷰 대시보드")).toBeInTheDocument();
  expect(screen.getByText("하네스 편집")).toBeInTheDocument();
  expect(screen.getByText("설정")).toBeInTheDocument();
  expect(screen.getByText("LLM Wiki")).toBeInTheDocument();
  expect(screen.getByText("자가 학습")).toBeInTheDocument();
});
```

- [ ] **Step 5: 구현** — `web/src/App.tsx`(앱 셸: nav 목록 + 콘텐츠 스위치)

```tsx
import { useState } from "react";
import "./theme.css";
import { ReviewSection } from "./sections/ReviewSection";
import { HarnessSection } from "./sections/HarnessSection";
import { SettingsSection } from "./sections/SettingsSection";
import { StubSection } from "./sections/StubSection";

const SECTIONS = [
  { key: "review", label: "리뷰 대시보드", el: <ReviewSection /> },
  { key: "harness", label: "하네스 편집", el: <HarnessSection /> },
  { key: "settings", label: "설정", el: <SettingsSection /> },
  { key: "wiki", label: "LLM Wiki", el: <StubSection title="LLM Wiki" note="곧 제공" /> },
  { key: "learn", label: "자가 학습", el: <StubSection title="자가 학습" note="실험 단계" /> },
];

export default function App() {
  const [active, setActive] = useState("review");
  const cur = SECTIONS.find((s) => s.key === active)!;
  return (
    <div className="app">
      <nav className="nav">
        <h3 style={{ padding: "0 12px" }}>Almighty Review</h3>
        {SECTIONS.map((s) => (
          <div key={s.key}
               className={"nav-item" + (s.key === active ? " active" : "")}
               onClick={() => setActive(s.key)}>
            {s.label}
          </div>
        ))}
      </nav>
      <main className="content">{cur.el}</main>
    </div>
  );
}
```

> **★개정 (codex [LOW] 라우팅 seam):** v1도 URL 상태를 갖도록 **React Router**를 얇게 도입한다(장기 Next.js 전환 여지 확보). `npm i react-router-dom` 후 `main.tsx`를 `<BrowserRouter>`로 감싸고, nav의 `active`를 `useLocation().pathname`으로, 전환을 `useNavigate()`로 처리한다(`/reviews`, `/reviews/:prId`, `/settings`, `/harness`, `/wiki`, `/learn`). 위 state 기반 nav는 그 골격이며, 라우터 도입 시 `active` state를 경로로 대체한다. `App.test.tsx`는 `<MemoryRouter>`로 감싸 렌더한다. Next.js는 지금 도입하지 않는다.

`web/src/api.ts`:
```ts
export const api = {
  repos: () => fetch("/api/repos").then((r) => r.json()),
  overview: () => fetch("/api/overview").then((r) => r.json()),  // ★개정: PR 오버뷰
  settings: () => fetch("/api/settings").then((r) => r.json()),
  runFindings: (id: number) =>
    fetch(`/api/runs/${id}/findings`).then((r) => r.json()),
  runVendorResults: (id: number) =>  // ★개정: 실패 벤더 노출
    fetch(`/api/runs/${id}/vendor-results`).then((r) => r.json()),
  patchFinding: (id: number, body: object) =>
    fetch(`/api/findings/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json()),
  postRun: (id: number) =>
    fetch(`/api/runs/${id}/post`, { method: "POST" }).then((r) => r.json()),
};
```

> **★개정 (codex [LOW] API 계약 불일치):** `ReviewSection`이 PR 목록을 기대하므로 `/api/repos`(레포만 반환)가 아니라 전용 `/api/overview`를 만든다. 백엔드에 추가(아래):
> ```python
> # server/api.py — PR별 최신 pre_screen·최고 severity·최신 run을 join
> @app.get("/api/overview")
> def overview(conn=Depends(get_conn)):
>     rows = conn.execute("""
>       SELECT p.id, p.number, p.title, r.full_name AS repo,
>              (SELECT complexity FROM pre_screen ps WHERE ps.pr_id=p.id
>                 ORDER BY ps.id DESC LIMIT 1) AS prescreen,
>              (SELECT MIN(CASE f.severity WHEN 'critical' THEN 0 WHEN 'high'
>                 THEN 1 WHEN 'medium' THEN 2 ELSE 3 END)
>                 FROM finding f JOIN review_run rr ON rr.id=f.run_id
>                 WHERE rr.pr_id=p.id) AS sev_rank,
>              (SELECT id FROM review_run rr WHERE rr.pr_id=p.id
>                 ORDER BY id DESC LIMIT 1) AS run_id
>       FROM pull_request p JOIN repo r ON r.id=p.repo_id
>       WHERE p.state='open' ORDER BY p.updated_at DESC
>     """).fetchall()
>     sev = {0: "critical", 1: "high", 2: "medium", 3: "low"}
>     return [{**dict(x), "severity": sev.get(x["sev_rank"], "low")} for x in rows]
> ```

- [ ] **Step 6: 통과 확인 & 커밋**

Run: `cd web && npx vitest run`
Expected: PASS (App 렌더 테스트).
```bash
git add web/package.json web/vite.config.ts web/index.html web/tsconfig.json web/src/
git commit -m "feat: React app shell with nav sections and Korean-readable light theme"
```

### Task 6.2: 리뷰 섹션 — 레포 탭 + 오버뷰↔디테일 드릴

**Files:**
- Create: `web/src/sections/ReviewSection.tsx`
- Test: `web/src/sections/ReviewSection.test.tsx`

- [ ] **Step 1: 실패 테스트** — `web/src/sections/ReviewSection.test.tsx`

```tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { ReviewSection } from "./ReviewSection";

const PRS = [
  { id: 1, number: 7, title: "fix null", repo: "acme/api",
    prescreen: "complex", severity: "high", run_id: 11 },
];

test("overview lists PRs and drills into detail", async () => {
  render(<ReviewSection loadPrs={async () => PRS}
                        loadFindings={async () => [
                          { id: 5, file: "a.py", line: 3, severity: "high",
                            claim: "널 역참조", status: "pending", vendor: "claude" },
                        ]}
                        loadVendors={async () => [
                          { vendor: "claude", status: "done", error: null },
                          { vendor: "codex", status: "failed", error: "rate limit" },
                        ]} />);
  // 오버뷰: PR 카드 + 리뷰-필요성 배지
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getByText("complex")).toBeInTheDocument();
  // 드릴다운
  fireEvent.click(screen.getByText("fix null"));
  expect(await screen.findByText(/널 역참조/)).toBeInTheDocument();
  // ★개정 (codex v6): 부분 실패 벤더 배지 노출
  expect(await screen.findByText(/일부 벤더 리뷰 실패/)).toBeInTheDocument();
  expect(screen.getByText(/codex/)).toBeInTheDocument();
  // 뒤로가기 복귀
  fireEvent.click(screen.getByText("← 오버뷰"));
  expect(await screen.findByText("fix null")).toBeInTheDocument();
});
```

- [ ] **Step 2: 구현** — `web/src/sections/ReviewSection.tsx`

```tsx
import { useEffect, useState } from "react";
import { api } from "../api";

type Pr = {
  id: number; number: number; title: string; repo: string;
  prescreen: string; severity: string; run_id: number;
};
type Finding = {
  id: number; file: string; line: number; severity: string;
  claim: string; status: string; vendor: string;
};

const SEV_COLOR: Record<string, string> = {
  critical: "var(--sev-critical)", high: "var(--sev-high)",
  medium: "var(--sev-medium)", low: "var(--sev-low)",
};

export function ReviewSection(props: {
  loadPrs?: () => Promise<Pr[]>;
  loadFindings?: (runId: number) => Promise<Finding[]>;
  loadVendors?: (runId: number) => Promise<VendorResult[]>;
}) {
  const loadPrs = props.loadPrs ?? api.overview;  // ★개정: 계약 일치
  const loadFindings = props.loadFindings ?? api.runFindings;
  const loadVendors = props.loadVendors ?? api.runVendorResults;
  const [prs, setPrs] = useState<Pr[]>([]);
  const [tab, setTab] = useState("전체");
  const [detail, setDetail] = useState<Pr | null>(null);

  useEffect(() => { loadPrs().then(setPrs); }, []);
  const repos = ["전체", ...Array.from(new Set(prs.map((p) => p.repo)))];
  const shown = tab === "전체" ? prs : prs.filter((p) => p.repo === tab);

  if (detail) return <Detail pr={detail} load={loadFindings}
                             loadVendors={loadVendors}
                             onBack={() => setDetail(null)} />;
  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        {repos.map((r) => (
          <button key={r} onClick={() => setTab(r)}
                  style={{ fontWeight: tab === r ? 700 : 400 }}>{r}</button>
        ))}
      </div>
      {shown.map((p) => (
        <div key={p.id} className="nav-item"
             style={{ border: "1px solid var(--border)", marginBottom: 8,
                      background: "var(--panel)" }}
             onClick={() => setDetail(p)}>
          <b>{p.title}</b> <span style={{ color: "var(--muted)" }}>
            {p.repo} #{p.number}</span>
          <span className="badge" style={{ marginLeft: 8,
                background: "#eef3ff" }}>{p.prescreen}</span>
          <span className="badge" style={{ marginLeft: 6, color: "#fff",
                background: SEV_COLOR[p.severity] }}>{p.severity}</span>
        </div>
      ))}
    </div>
  );
}

type VendorResult = { vendor: string; status: string; error: string | null };

function Detail({ pr, load, loadVendors, onBack }: {
  pr: Pr; load: (id: number) => Promise<Finding[]>;
  loadVendors?: (id: number) => Promise<VendorResult[]>; onBack: () => void;
}) {
  const loadVR = loadVendors ?? api.runVendorResults;
  const [findings, setFindings] = useState<Finding[]>([]);
  const [vendors, setVendors] = useState<VendorResult[]>([]);
  useEffect(() => { load(pr.run_id).then(setFindings); }, [pr.run_id]);
  useEffect(() => { loadVR(pr.run_id).then(setVendors); }, [pr.run_id]);
  const failed = vendors.filter((v) => v.status === "failed");
  const set = (id: number, status: string) => {
    api.patchFinding(id, { status });
    setFindings((fs) => fs.map((f) => f.id === id ? { ...f, status } : f));
  };
  return (
    <div>
      <button onClick={onBack}>← 오버뷰</button>
      <h2>{pr.title} <small>{pr.repo} #{pr.number}</small></h2>
      {failed.length > 0 && (
        // ★개정 (codex v6/v7 [MEDIUM/LOW]): 실패 벤더 노출. 자동 재시도 없으므로
        // 사용자가 보고 수동 재리뷰로 재실행. 전원 실패면 "일부" 문구를 뺀다.
        <div style={{ border: "1px solid var(--sev-high)", borderRadius: 8,
             padding: 8, marginBottom: 8, background: "#fff5f5" }}>
          ⚠ {vendors.length > 0 && failed.length === vendors.length
               ? "벤더 리뷰 실패" : "일부 벤더 리뷰 실패"}(자동 재시도 안 함):{" "}
          {failed.map((v) => `${v.vendor}(${v.error ?? "실패"})`).join(", ")}
        </div>
      )}
      {findings.map((f) => (
        <div key={f.id} style={{ border: "1px solid var(--border)",
             borderRadius: 8, padding: 12, marginBottom: 8,
             background: "var(--panel)" }}>
          <span className="badge" style={{ color: "#fff",
                background: SEV_COLOR[f.severity] }}>{f.severity}</span>
          <code style={{ marginLeft: 8 }}>{f.file}:{f.line}</code>
          <span style={{ marginLeft: 8, color: "var(--muted)" }}>{f.vendor}</span>
          <p>{f.claim}</p>
          <button onClick={() => set(f.id, "approved")}>승인</button>
          <button onClick={() => set(f.id, "dismissed")}>기각</button>
          <span style={{ marginLeft: 8 }}>상태: {f.status}</span>
        </div>
      ))}
      <button onClick={() => api.postRun(pr.run_id)}>승인분 포스팅</button>
    </div>
  );
}
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `cd web && npx vitest run src/sections/ReviewSection.test.tsx`
Expected: PASS.
```bash
git add web/src/sections/ReviewSection.tsx web/src/sections/ReviewSection.test.tsx
git commit -m "feat: review section with repo tabs and overview→detail drilldown"
```

### Task 6.3: 설정 · 하네스편집 · 스텁 섹션

**Files:**
- Create: `web/src/sections/SettingsSection.tsx`, `HarnessSection.tsx`, `StubSection.tsx`
- Test: `web/src/sections/SettingsSection.test.tsx`

- [ ] **Step 1: 실패 테스트** — `web/src/sections/SettingsSection.test.tsx`

```tsx
import { render, screen } from "@testing-library/react";
import { SettingsSection } from "./SettingsSection";

test("renders global defaults from settings", async () => {
  render(<SettingsSection load={async () => ({
    default_effort: "medium", concurrency_limit: 2,
    default_poll_interval: 60, approval_gate_on: 1,
    prescreen_model: "claude-haiku", prescreen_gate_threshold: "moderate",
  })} />);
  expect(await screen.findByDisplayValue("medium")).toBeInTheDocument();
  expect(screen.getByText(/동시성/)).toBeInTheDocument();
});
```

- [ ] **Step 2: 구현** — 세 파일

`web/src/sections/StubSection.tsx`:
```tsx
export function StubSection({ title, note }: { title: string; note: string }) {
  return (
    <div style={{ color: "var(--muted)", padding: 40, textAlign: "center" }}>
      <h2>{title}</h2>
      <p>{note} — 서브프로젝트 C에서 제공 예정.</p>
    </div>
  );
}
```

`web/src/sections/SettingsSection.tsx`:
```tsx
import { useEffect, useState } from "react";
import { api } from "../api";

type S = {
  default_effort: string; concurrency_limit: number;
  default_poll_interval: number; approval_gate_on: number;
  prescreen_model: string; prescreen_gate_threshold: string;
};

export function SettingsSection({ load }: { load?: () => Promise<S> }) {
  const loader = load ?? api.settings;
  const [s, setS] = useState<S | null>(null);
  useEffect(() => { loader().then(setS); }, []);
  if (!s) return <p>불러오는 중…</p>;
  return (
    <div>
      <h2>전역 기본값</h2>
      <label>기본 effort <input defaultValue={s.default_effort} /></label>
      <p>동시성 N: {s.concurrency_limit}</p>
      <p>폴링 간격: {s.default_poll_interval}s</p>
      <p>승인 게이트: {s.approval_gate_on ? "켜짐" : "꺼짐"}</p>
      <p>사전 스크리닝 모델: {s.prescreen_model}</p>
      <p>풀리뷰 게이트 임계: {s.prescreen_gate_threshold}</p>
    </div>
  );
}
```

`web/src/sections/HarnessSection.tsx`:
```tsx
import { useState } from "react";

export function HarnessSection() {
  const [prompt, setPrompt] = useState("");
  return (
    <div>
      <h2>하네스 편집 <small>(default)</small></h2>
      <p style={{ color: "var(--muted)" }}>
        리뷰 system prompt · 툴 allowlist · MCP · 모델/effort · 샌드박스</p>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)}
                rows={12} style={{ width: "100%" }}
                placeholder="리뷰 system prompt…" />
      <button>저장</button> <button>되돌리기</button>
    </div>
  );
}
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `cd web && npx vitest run`
Expected: 전체 프론트 테스트 PASS.
```bash
git add web/src/sections/
git commit -m "feat: settings, harness editor, and stub sections"
```

---

## Milestone 7 — 하네스 편집 API · E2E 스모크 · README

**목표:** 하네스 웹 편집 백엔드, 파이프라인→API 배선 마무리, 실제 CLI로 1개 PR 왕복 스모크, 실행 문서화.

### Task 7.1: 하네스 조회/편집 API

**Files:**
- Modify: `server/api.py` (GET/PUT `/api/harness/{name}`)
- Test: `tests/test_harness_api.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_harness_api.py`

```python
from fastapi.testclient import TestClient
from server.api import app, get_conn
from server.db import connect, init_schema


def test_get_and_update_harness_prompt(tmp_path):
    conn = connect(tmp_path / "h.db"); init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    got = client.get("/api/harness/default").json()
    assert "system_prompt" in got
    r = client.put("/api/harness/default",
                   json={"system_prompt": "새 리뷰 지침"})
    assert r.status_code == 200
    assert client.get("/api/harness/default").json()["system_prompt"] \
        == "새 리뷰 지침"
```

- [ ] **Step 2: 구현** — `server/api.py`에 추가

```python
from server import config
from server.review.harness import HarnessProfile


@app.get("/api/harness/{name}")
def get_harness(name: str):
    hp = HarnessProfile.load(name)
    return {"name": hp.name, "system_prompt": hp.system_prompt,
            "claude_allowed_tools": hp.claude_allowed_tools,
            "codex_sandbox": hp.codex_sandbox, "model": hp.model,
            "effort": hp.effort}


class HarnessPut(BaseModel):
    system_prompt: str | None = None


@app.put("/api/harness/{name}")
def put_harness(name: str, body: HarnessPut):
    base = config.HARNESS_DIR / name
    if body.system_prompt is not None:
        (base / "review-system-prompt.md").write_text(body.system_prompt)
    return get_harness(name)
```

> **v1 편집 범위(§10-8 확정 필요):** system prompt만 편집 허용으로 출발(검증 최소, 파일 덮어쓰기). allowlist/모델 편집은 스키마 검증 필요 → 후속. 롤백은 git 이력에 하네스 자산이 커밋돼 있으므로 `git checkout`으로 커버.

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_harness_api.py -v` → PASS
```bash
git add server/api.py tests/test_harness_api.py
git commit -m "feat: harness system-prompt read/edit API (v1 scope)"
```

### Task 7.2: 파이프라인 실행 배선(enqueue → RunnerPool)

**Files:**
- Modify: `server/api.py` (POST `/api/prs/{pid}/review` 수동 트리거 + 폴러 enqueue를 RunnerPool로)
- Test: `tests/test_review_trigger.py`

> **★개정 (codex [CRITICAL]):** 요청 핸들러가 `review_pr`를 직접 `await`하던 설계를 제거한다. `POST /api/prs/{pid}/review`는 **`review_job`을 enqueue하고 즉시 `job_id`를 반환**하며, 실제 실행은 worker 루프(Task 4.5)가 담당한다. 이로써 "얇은 오케스트레이터"가 성립하고 seam(JobQueue)이 실제로 쓰인다.

- [ ] **Step 1: 실패 테스트** — `tests/test_review_trigger.py`

```python
from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema
from server.repos import repo_repo, pr_repo


def test_manual_trigger_enqueues_job(tmp_path):
    conn = connect(tmp_path / "t.db"); init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    rid = repo_repo.add(conn, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(conn, repo_id=rid, number=3, title="t", author="a",
                         head_sha="s3", base_ref="main", url="u")
    r = client.post(f"/api/prs/{pid}/review")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    row = conn.execute("SELECT * FROM review_job WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "queued" and row["trigger"] == "manual"
```

- [ ] **Step 2: 구현** — `server/review/gh_deps.py`(build_deps) + `server/api.py`(enqueue)

`server/review/gh_deps.py` (worker·수동트리거 공용 deps 조립):
```python
from server.github.gh import GhClient
from server.pipeline import PipelineDeps
from server.review.prescreen import prescreen
from server.review.vendors import ClaudeAdapter, CodexAdapter
from server.review.worktree import prepared_worktree


def build_deps(repo) -> PipelineDeps:
    if not repo["local_path"]:
        raise ValueError(f"repo {repo['full_name']}에 local_path 미설정")
    gh = GhClient()
    hp = HarnessProfile.load(repo["harness_name"])

    def _prescreen_tuple(diff, model):
        # ★개정: prescreen도 격리 config dir + 인증 주입(전역 미상속 유지).
        with tempfile.TemporaryDirectory(prefix="almighty-ps-") as rt:
            hp.prepare_runtime(runtime_dir=rt)
            r = prescreen(diff=diff, model=model,
                          env=hp.isolated_env(runtime_dir=rt))
        return (r.complexity, r.score, r.reason)

    return PipelineDeps(
        gh_diff=gh.diff,
        worktree=prepared_worktree,
        adapters=[ClaudeAdapter(), CodexAdapter()],
        prescreen=_prescreen_tuple,
        repo_local_path=repo["local_path"],  # ★개정: 휴리스틱 제거, DB 컬럼 사용
    )
```

(파일 상단 import에 `tempfile`, `from server.review.harness import HarnessProfile` 추가.)

`server/api.py`에 추가:
```python
from server.repos import job_repo, pr_repo


@app.post("/api/prs/{pid}/review", status_code=202)
def trigger_review(pid: int, conn=Depends(get_conn)):
    pr = pr_repo.get(conn, pid)
    job_id = job_repo.enqueue(conn, pr_id=pid, head_sha=pr["head_sha"],
                              trigger="manual")
    return {"job_id": job_id}
```

- [ ] **Step 3: 통과 확인 & 커밋**

Run: `pytest tests/test_review_trigger.py -v` → PASS
```bash
git add server/review/gh_deps.py server/api.py tests/test_review_trigger.py
git commit -m "feat: manual trigger enqueues review_job (no direct handler execution)"
```

- [ ] **Step 4: lifespan에서 poller+worker 기동/정리** — `server/main.py` + `server/api.py`

★개정 (codex 재검증 [HIGH]/[LOW]): deprecated `@app.on_event` 대신 **lifespan asynccontextmanager**로 poller/worker를 기동하고, shutdown 시 `stop_event`로 신호 후 `gather(..., return_exceptions=True)`로 **graceful 종료를 await**한다.

`server/api.py` 상단을 lifespan 방식으로:
```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    from server import config
    from server.poller import poll_loop
    from server.worker import worker_loop
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(poll_loop(config.DB_PATH, stop_event=stop)),
        asyncio.create_task(worker_loop(config.DB_PATH, stop_event=stop)),
    ]
    try:
        yield
    finally:
        stop.set()                                  # 신호
        await asyncio.gather(*tasks, return_exceptions=True)  # 완료까지 대기


app = FastAPI(title="Almighty PR Review Server", lifespan=lifespan)
```
(기존 `app = FastAPI(...)` 라인을 위 lifespan 버전으로 교체.)

`server/main.py`:
```python
import uvicorn

from server.api import app


def run() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    run()
```

> **주의:** `poll_loop`/`worker_loop`은 `stop_event`를 받도록 이미 정의됨(Task 4.4/4.5). 두 루프의 `while` 조건과 idle 대기가 `stop_event`를 확인하므로 신호 즉시 종료된다.

커밋:
```bash
git add server/api.py server/main.py
git commit -m "feat: lifespan-managed poller/worker with graceful shutdown"
```

### Task 7.3: E2E 스모크(실제 CLI 1왕복) + README

**Files:**
- Create: `README.md`
- Create: `tests/test_e2e_smoke.py` (기본 skip, 환경변수로 opt-in)

- [ ] **Step 1: 계약 재확인** — Milestone 0.5 산출물 대조

`docs/vendor-cli-contract.md`(Task 0.5)와 `vendors.py`·`harness.py`의 argv/env가 일치하는지 최종 확인. (실플래그 확정은 이미 0.5에서 선행 완료 — 여기서는 회귀 확인만.)

- [ ] **Step 1.5: fake-CLI 통합 테스트** ★개정 (codex [HIGH] 통합 리스크)

주입 mock이 못 잡는 subprocess 경계를 검증한다. PATH 앞에 가짜 `claude`/`codex` 실행파일을 두고 실제 `asyncio.create_subprocess_exec` 경로를 태운다.

`tests/test_vendor_subprocess.py`:
```python
import asyncio
import os
import stat

from server.review.harness import HarnessProfile
from server.review.vendors import ClaudeAdapter


def _fake_bin(dir_, name, stdout):
    p = dir_ / name
    p.write_text(f'#!/usr/bin/env bash\ncat >/dev/null\ncat <<\'EOF\'\n{stdout}\nEOF\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


FAKE_OUT = ('ok\n```json\n{"findings":[{"file":"a.py","line":1,'
            '"severity":"low","category":"style","claim":"c","rationale":"r",'
            '"confidence":0.3}]}\n```')


def test_claude_real_subprocess(tmp_path, monkeypatch):
    _fake_bin(tmp_path, "claude", FAKE_OUT)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hp = HarnessProfile.load("default")
    fs = asyncio.run(ClaudeAdapter().review(
        prompt="리뷰", workdir=tmp_path, harness=hp,
        runtime_dir=str(tmp_path / "rt")))
    assert fs[0].file == "a.py"  # stdin 닫힘 + 종료코드0 + 파싱까지 실경로 검증


def test_codex_real_subprocess(tmp_path, monkeypatch):  # ★개정: codex 경로도
    from server.review.vendors import CodexAdapter
    _fake_bin(tmp_path, "codex", FAKE_OUT)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hp = HarnessProfile.load("default")
    fs = asyncio.run(CodexAdapter().review(
        prompt="리뷰", workdir=tmp_path, harness=hp,
        runtime_dir=str(tmp_path / "rt")))
    assert fs[0].vendor == "codex" and fs[0].file == "a.py"
```

Run: `pytest tests/test_vendor_subprocess.py -v` → PASS (2 passed)
```bash
git add tests/test_vendor_subprocess.py
git commit -m "test: fake-CLI subprocess integration for both vendor adapters"
```

- [ ] **Step 2: opt-in E2E 스모크** — `tests/test_e2e_smoke.py`

```python
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ALMIGHTY_E2E") != "1",
    reason="E2E는 실제 gh/claude/codex 인증 필요 — ALMIGHTY_E2E=1로 opt-in",
)


def test_end_to_end_single_pr(tmp_path):
    """실제 레포/PR 1건을 폴링→enqueue→worker 리뷰→findings 저장까지. 포스팅 X."""
    import asyncio

    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo, finding_repo
    from server.poller import poll_once
    from server.worker import run_one_job
    from server.github.gh import GhClient

    conn = connect(tmp_path / "e2e.db"); init_schema(conn)
    repo_full = os.environ["ALMIGHTY_E2E_REPO"]          # 예: "me/sandbox"
    local = os.environ["ALMIGHTY_E2E_LOCAL"]             # 로컬 clone 경로
    rid = repo_repo.add(conn, full_name=repo_full, local_path=local)
    gh = GhClient()

    def enqueue(pid):
        pr = pr_repo.get(conn, pid)
        job_repo.enqueue(conn, pr_id=pid, head_sha=pr["head_sha"], trigger="auto")
    poll_once(conn, list_prs=gh.list_open_prs, enqueue=enqueue)
    assert conn.execute("SELECT COUNT(*) c FROM review_job").fetchone()["c"] > 0

    # worker가 잡 1건을 끝까지 실행(실제 claude/codex 왕복)
    asyncio.run(run_one_job(conn, worker_id="e2e"))
    done = conn.execute(
        "SELECT * FROM review_job WHERE status IN ('done','failed') LIMIT 1"
    ).fetchone()
    assert done is not None
    if done["status"] == "done":
        assert finding_repo.list_for_run(conn, done["run_id"]) is not None
```

- [ ] **Step 3: `README.md` 작성**

````markdown
# Almighty PR Review Server

로컬 단일사용자 멀티벤더(Claude+Codex) PR 리뷰 서버.

## 실행
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m server.main          # http://127.0.0.1:8787
cd web && npm install && npm run dev   # http://localhost:5173
```

## 사전 요구
- `gh` 로그인(`gh auth status`), `claude`/`codex` CLI 로그인.
- 리뷰 대상 레포는 로컬에 clone 되어 있어야 함(격리 worktree 소스).

## 안전
- 리뷰 워커: read-only 툴만. 유일 write = 승인 후 PR 코멘트.
- 전역 프로파일 미상속(리뷰 전용 하네스). 격리 worktree.

## 아키텍처 / 데이터모델
`docs/superpowers/specs/2026-07-07-almighty-pr-review-design.md` 참조.

## E2E 스모크
```bash
ALMIGHTY_E2E=1 ALMIGHTY_E2E_REPO=me/sandbox \
  ALMIGHTY_E2E_LOCAL=/path/to/local/clone \
  pytest tests/test_e2e_smoke.py -v
```
````

- [ ] **Step 4: 전체 테스트 & 커밋**

Run: `pytest -q` (백엔드 전체) + `cd web && npx vitest run` (프론트 전체)
Expected: 전부 PASS (E2E는 skip).
```bash
git add README.md tests/test_e2e_smoke.py
git commit -m "docs: README and opt-in E2E smoke test"
```

---

## Self-Review (스펙 대조 체크리스트)

**1. Spec coverage:**

| 스펙 항목 | 구현 위치 |
|-----------|-----------|
| §2 greenfield/로컬단일/CLI헤드리스 | M0 스캐폴드, M3 vendors(API키 미사용) |
| §2 전용 리뷰 하네스(전역격리) | Task 0.5 preflight + Task 3.3 `isolated_env`(env allowlist) |
| §2 v1 입력=diff+레포코드, ContextProvider no-op | Task 4.1 `NoOpContextProvider`, `_build_prompt` |
| §2 벤더 on/off 토글(전역·레포별) | repo 컬럼 + `_enabled_adapters` |
| §2 사전 스크리닝 | Task 3.5 `prescreen` + 파이프라인 gate |
| §2 병합 옵션 기본 OFF | Task 3.6 `merge_enabled` 분기(vendor_result_id 링크 보존) |
| §2 PR 코멘트 구조화 마크다운 | Task 5.1 `build_comment` + update-or-create(5.2) |
| §2 대시보드 라이트/앱셸/드릴/한글 | M6 전체 + `/api/overview` + React Router seam |
| §5 read-only·격리worktree·세마포어·마커 | Task 0.5/3.2/3.4/3.6, formatter MARKER |
| §6 전 테이블 + review_job(스케줄) | Task 1.1 SCHEMA + Task 1.3 job_repo |
| §7 파이프라인 1~9 | poller(1)→job→worker(4.5)→pipeline prescreen(2)/review(3~6)→API triage(7)→post(8)→worktree ctx(9) |
| §8 nav 6섹션·탭·드릴·타이포 | M6 App/Review/Settings/Harness/Stub |
| §9 seam 6종(+JobQueue) | seams.py + harness_name + RunnerPool(실배선) + job_repo |

**2. 개정 반영 확인 (codex 검증 대비):**
- [CRITICAL] 요청핸들러 직접실행 → Task 7.2 enqueue + Task 4.5 worker ✅
- [CRITICAL] 동기 subprocess 블록 → Task 3.4 `asyncio.create_subprocess_exec`+timeout ✅
- [CRITICAL] 전역 `_conn` → Task 1.1/4.3 connection-per-unit+WAL ✅
- [CRITICAL] RunnerPool 미사용 → Task 4.2 `pool.run`+`gather` ✅
- [HIGH] 실플래그 지연 → Milestone 0.5 선행 ✅
- [HIGH] env 격리 부족 → Task 3.3 allowlist env + preflight ✅
- [HIGH] worktree read-only 오해 → Task 3.2 경계 명확화 + shutil cleanup ✅
- [HIGH] 통합테스트 부재 → Task 7.3 fake-CLI 통합테스트 ✅
- [HIGH] job/lock/retry 부재 → Task 1.3 review_job(claim/backoff) ✅
- [MEDIUM] local_path 휴리스틱 → repo.local_path 컬럼 ✅
- [MEDIUM] 포스팅 중복 → update-or-create supersede ✅
- [LOW] 프론트 계약 불일치 → `/api/overview` ✅  / 라우팅 seam → React Router ✅

**2b. codex 재검증(v2) 게이트 4항목 반영 확인:**
- [HIGH] claim 동시성/락처리 → `claim_next` OperationalError→None + 별도커넥션 동시 claim 테스트(Task 1.3) ✅
- [HIGH] worker graceful shutdown/stale-lock → `recover_stale` + lifespan `stop_event` gather(Task 4.5·7.2) ✅
- [HIGH] review_run 실패 정합성 → `review_pr` try/except finish failed(Task 4.2) ✅
- [MEDIUM] prescreen 격리/timeout → stdin닫기·timeout·격리 env(Task 3.5·7.2) ✅
- 추가: 포스팅 실중복(gh PATCH edit) · merge id() 제거 · codex fake-CLI 테스트 · on_event→lifespan ✅

**2c. codex 3차 재검증(v3) HIGH 3건 + MEDIUM/LOW 반영 확인 (7.0→7.3):**
- [HIGH] 격리 env ↔ CLI 인증 충돌 → `AUTH_ENV_KEYS` allowlist + `prepare_runtime()`(인증만 심고 rules/skills/MCP 제외), preflight가 "인증 성립"·"전역 미상속" 동시 실증(Task 0.5·3.3) ✅
- [HIGH] failed run ↔ retry job 정합성 → `review_pr`가 `PipelineError(run_id)` 던지고 worker가 `mark_failed(run_id=)`로 기록, **1 attempt = 1 run** 명문화(Task 4.2·4.5·1.2) ✅
- [HIGH] 동시 claim 가짜 그린 → 결정론적 writer-락 경합 + 스레드 barrier 동시출발 테스트로 대체(Task 1.3) ✅
- [MEDIUM] worker idle busy-loop → `stop_event=None`이면 `asyncio.sleep` 분기(Task 4.5) ✅
- [LOW] comment id URL 파싱 → API JSON `.id` 저장(Task 2.1·5.2) ✅ / merge `consensus_group_id` DB 저장(Task 1.2·4.2) ✅

**2d. codex 4차 재검증(v4) 신규 회귀 반영 확인 (7.3→7.1, 잠복버그 발견):**
- [HIGH·실버그] 벤더 전원 실패 → run=done 오판 → `succeeded==0`이면 예외 승격 → run failed + PipelineError + retry, `test_pipeline_fails_run_when_all_vendors_fail` 추가(Task 4.2) ✅
- [MEDIUM] preflight 비executable → claude/codex 양쪽 실제 auth·no-global 명령 검증으로 구체화(Task 0.5) ✅
- [MEDIUM] pre-run `job.run_id` stale → 정책 명문화(직전 run 유지, attempt 진실원=review_run)(Task 1.2) ✅
- [LOW] skip job=done 애매 → `job.status=done`=스케줄러 완료 계약 명문화, 리뷰 성공은 `review_run.status`로 판별(Task 4.5) ✅
- [LOW] `worker_loop(None)` 무한루프 → 테스트 전용·운영은 `stop_event` 필수 계약 명문화(Task 4.5) ✅

**2e. codex 5차 재검증(v5) 확인 (7.1→7.5, HIGH 회귀 0):** v4 핵심 배선 RESOLVED·이중커밋 없음 확인. 잔여 정리:
- [MEDIUM] preflight false-pass → sentinel(`OK`/`CLEAN`/`LEAKED`) 정확 일치 검증으로 교체(Task 0.5) ✅
- [MEDIUM] 부분 실패 영구 누락 → v1 정책 명문화(개별 재시도 없음, 대시보드 노출+수동 재리뷰), 벤더별 follow-up=v-next(Task 4.2) ✅
- [LOW] enabled 벤더 0개 → worktree 전 `canceled` 마감(Task 4.2) ✅
- [LOW] worker-level all-vendor-fail retry 테스트 → `test_worker_records_failed_run_id_on_pipeline_error` 추가(Task 4.5) ✅

**2f. codex 6차 재검증(v6) 회귀·갭 반영 확인 (7.5→7.2, v5 회귀 1건 수정):**
- [MEDIUM·회귀] 벤더 0개 canceled 재감지 루프 → poller가 벤더 0개 레포 enqueue 안 함(root 차단) + `test_poll_once_no_vendor_upserts_pr_but_skips_enqueue`(v7에서 테스트 강화·개명, Task 4.4) ✅
- [MEDIUM] 부분 실패 노출 정책만 존재 → `/api/runs/{id}/vendor-results` + ReviewSection 실패 배지로 실체화(Task 1.2·4.6·6.2) ✅
- [LOW] preflight false-negative → Task 0.5 one-word 준수 실증·raw 로그 caveat(Task 0.5) ✅
- [LOW] worker 테스트 real build_deps → monkeypatch로 환경 비의존(Task 4.5) ✅

**2g. codex 7차 재검증(v7) 회귀·문구 반영 확인 (7.2→7.3):**
- [MEDIUM·회귀] poller guard 위치 → upsert/폴링은 유지하고 enqueue만 `has_vendor` 가드로 이동, 테스트에 upsert+재활성화 단언 추가(Task 4.4) ✅
- [LOW] 배지 문구 전원/부분 분기(Task 6.2) ✅

**3. Type consistency:** `Finding`(models.py, `vendor_result_id` 포함) 필드 전 구간 동일. `review_pr`(→`_execute_run`)는 async, adapters `.review`는 async(FakeAdapter/실어댑터/통합테스트 일관). `job_repo.claim_next→worker.run_one_job→review_pr→pool.run(job)` 체인 시그니처 일치. `review_pr`는 실패 시 `PipelineError(run_id)`를 던지고 `run_one_job`이 `mark_failed(..., run_id=)`로 소비. `gh.post_comment`/`gh.edit_comment`는 `{id, html_url}` dict 반환 → `post_run`이 `str(res["id"])`·`res["html_url"]` 소비(FakeGh 동일). `prescreen(diff, model, env=)` 호출부 일치.

**남은 확정(구현 중, §10):** 벤더 CLI 실플래그는 Milestone 0.5에서 실증 후 `vendor-cli-contract.md` 고정 · 하네스 편집 범위 v1=system prompt만(Task 7.1) · 구조화 코멘트 최종 포맷 튜닝(Task 5.1).

---

## Current Completion State

이 문서는 원래 실행용 체크리스트 형태로 작성되어 있어 본문 checkbox는 미체크 상태로 남아 있다. 현재 레포 구현은 주요 Milestone 0~7의 v1 경로가 코드와 테스트로 반영된 상태이며, 완료 판단은 아래 검증 명령을 기준으로 한다.

- 백엔드 회귀: `.venv/bin/python -m pytest`
- 프론트 회귀: `cd web && npm test`
- 프론트 타입/빌드: `cd web && npm run build`
- 레포 설정 UI: 설정 화면에서 리뷰 대상 레포 등록, `local_path` 표시, 활성 토글을 지원한다.
- 문서 산출물: `docs/superpowers/specs`, `docs/superpowers/plans`, `docs/design-drafts`는 설계/실행/UX 결정 근거로 보존한다.

남은 별도 단계는 실제 인증이 필요한 opt-in E2E(`ALMIGHTY_E2E=1 ...`)와 사용자가 선택한 UX 방향의 본격 대시보드 개선이다.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-07-almighty-pr-review-server.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 태스크마다 fresh 서브에이전트 디스패치 + 태스크 간 리뷰, 빠른 반복.

**2. Inline Execution** — 이 세션에서 executing-plans로 체크포인트 배치 실행.

**Which approach?** (이전 세션 흐름대로라면, 플랜 확정 후 **codex 리뷰(pair-review)로 검증** → 실행 순서를 권장합니다.)
