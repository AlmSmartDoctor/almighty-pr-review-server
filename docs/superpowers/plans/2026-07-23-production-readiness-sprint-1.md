# Almighty PR Review — Production Readiness Sprint 1 Plan

- **Date:** 2026-07-23
- **Status:** Offline tooling complete — live evidence `not_run`, rollout locked
- **Scope:** 현재 대규모 미커밋 변경 안정화, vendor telemetry 실증, sandbox E2E, adjudicated benchmark 수집 기반, canary 관측 UI
- **Execution rule:** 이 문서는 구현 승인이나 live 호출·GitHub 쓰기·커밋·푸시 승인을 의미하지 않는다. 각 외부 호출/쓰기 게이트에서 별도 확인을 받는다.

## 1. 배경과 현재 기준선

현재 `main`은 `origin/main`과 같은 `e3447e7`이지만 작업 트리에 대규모 미커밋 변경이 있다.

- tracked 수정 74개, tracked diff 약 `+10,234/-1,338`
- 최초 플랜 기준 status의 untracked **collapsed entry**는 37개였다. Round 1 플랜/리뷰 문서 2개가 추가된 현재 collapsed 기준은 39개다. 다만 Task 0.1의 all-path inventory universe는 `--untracked-files=all`을 쓰므로 현재 **48 all-path entry**와 구분해 기록한다. 신규 구현·테스트·문서와 `.playwright-mcp/` 생성물이 섞여 있다.
- 주요 대형 파일: `server/api.py` 1,623줄, `server/pipeline.py` 1,511줄,
  `web/src/sections/SettingsSection.tsx` 1,269줄, `web/src/sections/ReviewSection.tsx` 1,253줄
- 현재 offline gate:
  - `.venv/bin/pytest -q`: 697개 수집, 2 skip, 실패 0
  - `cd web && npm test -- --run`: 105개 통과
  - `cd web && npm run build`: 통과
  - `git diff --check`: 통과
  - synthetic scope/dedupe benchmark: 기능 정확도 smoke 통과, rollout은 의도대로 `can_enforce=false`
- 아직 실행하지 않은 검증:
  - 실제 GitHub/vendor E2E
  - 성공한 Claude structured telemetry probe
  - adjudicated 100+ finding benchmark
  - scope/dedupe enforce canary

기존 계획 문서의 686 Python/98 web 수치는 현재 기준선과 다르므로 안정화 단계에서 갱신한다.

### 리뷰 후 용량 판정

독립 reviewer 3개 관점(correctness/feasibility, security/privacy, operability/sizing)에서 초안은 **NO-GO** 판정을 받았다. 주요 원인은 채점 불가능한 benchmark prediction 계약, live duplicate truth 부재, sprint-done/live/rollout 경계 혼재, 과대 범위였다. 아래 수정본은 해당 finding을 반영했다.

전체 범위는 solo 기준 약 **20–30 engineer-days**로 추정되므로 하나의 1–2주 sprint로 실행하지 않는다.

| Delivery slice | 범위 | 추정 | 종료 게이트 |
|---|---|---:|---|
| **1A** | Milestone 0, M1.1, PolicyDecision/run snapshot/retry identity: diff 안정화 + telemetry 안전 계약 + retry basis 고정 | 7–10일 | tooling offline green; M1.2 live attestation은 별도 evidence로 `passed/failed/not_run` |
| **1B** | Milestone 2: sandbox E2E harness + 단계별 live rehearsal | 3–5일 | no-write/retry pass, write 단계별 evidence |
| **1C** | Milestone 3: benchmark schemas/approved bundle·blind runner/scorer tooling | 6–9일 | synthetic/public tooling pass; final paired benchmark/attestation은 1D clean candidate 뒤 실행 |
| **1D** | Operations aggregate/API/UI + final candidate stabilization | 6–9일 | bounded query, UI/build pass, clean candidate identity 확정 |

각 slice는 별도 승인·검증·리뷰 후 다음 slice로 이동한다. 본 문서는 다섯 workstream의 상위 실행 계획이며, rollout unlock은 어느 delivery slice의 자동 완료 조건도 아니다.

## 2. 스프린트 목표

1. 현재 변경 전체를 누락 없이 분류하고 검토 가능한 vertical slice로 안정화한다.
2. Claude/Codex structured telemetry를 exact CLI version에서 content-safe하게 실증한다.
3. 전용 sandbox PR에서 sync → review → partial retry → post → webhook 흐름을 단계별로 검증한다.
4. 비독점 자료를 사람 판정하는 label-blind benchmark 수집·검증·채점 기반을 만든다.
5. 실행 당시 policy/cohort를 보존하고 false rejection·duplicate·partial·비용을 조회하는 read-only 운영 화면을 만든다.

## 3. 비목표

- 이번 스프린트에서 scope/dedupe를 전면 `enforce`로 전환하지 않는다.
- semantic/paraphrase duplicate 자동 삭제를 추가하지 않는다.
- plain snapshot을 OS-level read containment로 홍보하지 않는다.
- 민감/사내 레포에서 live canary를 실행하지 않는다.
- GitHub repository, webhook, PR, branch를 자동 생성·삭제하지 않는다.
- Slack posting/reaction E2E를 이번 GitHub E2E에 포함하지 않는다.
- team-mode, 임의 MSSQL row query, path-enforcing broker/container 전체 구현을 포함하지 않는다.
- 현재 변경을 이유로 unrelated architecture refactor를 확장하지 않는다.

## 4. 확정 설계 원칙

### D1. 안정화 전에는 live/외부 write 금지

현재 diff의 파일 inventory, 기능 분류, offline gate, blocker disposition이 확정되기 전에는 다음을 실행하지 않는다.

- paid/authenticated vendor probe
- GitHub comment/post, push, webhook 설정 변경
- Slack post
- scope/dedupe enforce 또는 unlock
- commit/push/rebase

커밋·푸시는 구현 완료 후에도 사용자의 별도 승인 없이는 실행하지 않는다.

### D2. Claude structured telemetry는 성공 attestation 전까지 잠근다

`docs/vendor-cli-contract.md`의 Claude terminal/error event schema는 expected-schema fixture로 저장할 수 있지만, **attestation gate 전에는 production structured allowlist를 활성화하지 않는다.** preflight는 그 fixture를 검증할 수 있으나 production adapter는 legacy text + `telemetry_status=unavailable`로 동작한다. M1.2는 sanitized evidence만 생성하고 production activation을 절대 바꾸지 않는다. attestation 뒤 allowlist activation은 별도 tested/fresh-reviewed commit과 별도 사용자 승인으로만 수행한다. live success는 다음을 모두 만족해야 한다.

- exact CLI version과 event schema 확인
- exit 0
- final output 추출 성공
- usage와 실제 tool event 파싱 성공
- `tool_calls >= 1`인 real tool call
- 원문을 저장하지 않는 bounded parser 검증

### D3. live auth는 선택 vendor별 최소 materialization

Codex-only 실행은 Claude credential을, Claude-only 실행은 Codex auth를 준비하지 않는다. 하나의 shared runtime에 양쪽 credential을 넣지 않고 vendor별 runtime을 소유·정리한다. 이 계약은 primary review뿐 아니라 retry, verify, prescreen, Wiki의 모든 `prepare_runtime()` 호출부에 적용한다. runtime credential은 invocation별 임시 디렉터리에만 두고 성공·setup failure·timeout·cancel 후 잔존 여부를 확인한다.

### D4. sandbox E2E는 exact target·credential trust·단계별 write gate를 요구한다

- `owner/repo`와 PR 번호는 필수이며, 실행자가 아닌 독립 operator가 관리하는 default-deny allowlist와 정확히 일치해야 한다.
- `GH_CONFIG_DIR`을 새 임시 격리 디렉터리로 강제하고, ambient `GH_TOKEN`/`GITHUB_TOKEN`/기타 GitHub token 변수와 native `gh` auth를 제거한다. operator가 provision한 단일 credential만 inject한다.
- review/retry는 read-only capability를 token metadata 또는 사전 승인된 capability attestation으로 독립 검증한다. 검증할 수 없으면 E2E를 시작하지 않는다.
- 기본 모드는 remote write 0건이다. 실제 PR comment는 별도 `--allow-post`와 사용자 확인 후에만 실행한다.
- 실제 `synchronize` webhook은 자동 push하지 않는다. 사용자가 승인한 sandbox push 또는 수동 commit 후 delivery만 검증한다.
- `ALMIGHTY_POST_BANNER`와 operation marker로 모든 rehearsal comment를 식별한다. Slack은 비활성화한다.

### D5. strong read containment는 미증명 상태로 유지한다

plain snapshot은 `.git`과 cwd-relative history 탐색을 줄이는 defense-in-depth일 뿐이며 model tool의 cwd 밖 read는 기술적으로 containment되지 않는다. live probe/E2E 승인 문구에는 “입력은 synthetic이지만 cwd 밖 read는 막지 못한다”를 명시한다. 따라서 clean VM 또는 전용 OS account(회사 checkout·credential·secret 없음)에서만 live probe/E2E를 실행한다. 이 환경을 제공·검증할 수 없으면 live 단계는 blocked/not_run으로 남긴다.

### D6. benchmark label과 model input을 물리적으로 분리한다

- model run manifest에는 expected defect, adjudication, known-clean label을 넣지 않는다.
- adjudication answer file은 별도 경로와 schema를 사용한다.
- proprietary PR/Jira/DB/context는 수집 도구가 기본 거부한다.
- Git에 넣는 것은 synthetic/public-redistributable fixture와 sanitized aggregate report뿐이다.

### D7. requested/effective policy·cohort는 run 시점에 snapshot한다

현재 설정을 조회 시점에 재계산해 과거 run을 분류하지 않는다. pipeline이 사용한 **requested와 effective** scope/dedupe mode, reason/selection source/2차원 cohort, policy/config hash, benchmark attestation hash를 `review_run`에 저장한다. legacy row는 `unknown`으로 표시한다. 최초 canary는 비교 baseline이 없으므로 `insufficient_baseline` 상태이며, 성능·안전성의 긍정 evidence가 아니다.

### D8. canary UI는 read-only다

새 운영 화면은 상태와 근거를 보여 주지만 enforce/unlock/kill switch를 변경하지 않는다. 변경 제어는 기존 설정/env 경계에 남기고, UI에는 restart 필요 여부와 effective reason을 명확히 표시한다.

## 5. 공통 검증 계약

모든 구현 milestone은 다음 순서를 따른다.

1. 실패 테스트 또는 재현 fixture 추가
2. 실패 확인
3. 최소 구현
4. focused test
5. Python 전체 테스트
6. web 영향이 있으면 Vitest + production build
7. `git diff --check`
8. 관련 문서와 residual risk 갱신

공통 명령:

```bash
.venv/bin/pytest -q
cd web && npm test -- --run && npm run build
cd .. && git diff --check
.venv/bin/python scripts/review-pipeline-benchmark.py \
  --output /tmp/review-pipeline-benchmark.json
```

테스트는 공유 temp namespace를 사용하는 benchmark/live command와 병렬 실행하지 않는다.

---

## Milestone 0 — 현재 변경 세트 안정화

### Task 0.1: authoritative inventory와 생성물 분류

**Files**

- Create: `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1-inventory.md`
- Modify: `.gitignore`
- Inspect: 전체 `git status`, tracked/untracked diff, 신규 파일

**Steps**

1. **Task-owned write 전** `git status --porcelain=v1 -z --untracked-files=all` raw bytes를 저장하고 SHA-256, `HEAD`, branch/upstream를 inventory에 기록해 immutable **B0**로 고정한다. B0는 collapsed count가 아니라 all-path entry count를 함께 기록한다.
2. classification universe는 **B0 ∪ B1**이다. B0의 tracked/untracked path와 이후 새 path를 `intended source`, `test`, `docs`, `generated/local artifact`, `unknown`으로 정확히 한 번 분류한다.
3. ignored scope는 모든 ignored dependency file이 아니라 `.gitignore`/global excludes의 **ignore root와 rule** 목록으로만 기록한다. secret·credential·`.env`·runtime DB·raw transcript·private benchmark workspace는 별도 sensitive denylist로 path 존재 여부만 검사하며 내용은 출력하지 않는다.
4. B0를 얻은 뒤에만 intended inventory 문서와 승인된 `.gitignore` rule을 쓴다. 그 뒤 동일 명령의 raw bytes/SHA를 **B1**로 고정하고, B0→B1의 exact expected transition set(승인된 inventory/.gitignore path와 명시된 ignored transition만)을 검증한다. 예상 밖 add/delete/rename/modify가 있으면 중단한다.
5. write 없는 stability window 동안 같은 status를 다시 수집해 **B2**로 고정하고 B1 raw bytes와 SHA가 완전히 같은지 검증한다. B1≠B2이면 분류·commit planning을 중단하고 concurrent mutation을 조사한다.
6. `.playwright-mcp/`가 재생성 가능한 로컬 산출물임을 확인한 뒤 ignore한다. 증거 가치가 있는 screenshot/report는 별도 명시 경로로 옮기지 않는 한 commit하지 않는다.
7. unknown 파일, B0/B1 expected-transition 위반, B1≠B2, 또는 denylist path가 tracked 후보에 있으면 분할을 중단하고 사용자에게 범위를 확인한다.

**Acceptance**

- B0/B1/B2의 `--porcelain=v1 -z --untracked-files=all` raw SHA-256, B0→B1 expected transition 검증, B1==B2 stability 결과가 inventory에 남는다.
- classification universe B0∪B1의 tracked/untracked all-path entry가 inventory에 정확히 한 번 등장하며 ignored는 root/rule, sensitive는 denylist로 분리된다.
- B0의 collapsed `git status --short` 113 entries와 all-path 122 entries(그중 untracked 48)는 서로 대체하지 않고 별도 수치로 기록된다.
- 생성물과 private data가 commit 후보에서 제외된다.
- pre-existing user change를 임의로 되돌리거나 흡수하지 않는다.

### Task 0.2: vertical slice 분할안 확정

**Status:** 분할안 확정. 아직 stage/commit하지 않았으며 아래 순서와 hunk ownership은 사용자 승인 전 실행 계약이 아니다.

#### Commit order와 공통 규칙

논리적 의존 순서는 **C0 → C1 → C2 → C3 → C4 → C5 → C6**이다. 다만 현재 diff에서 C2 worker/lifespan은 C5의 pipeline exception·signature와 `gh_deps`/`PipelineDeps`를 직접 소비하므로 C2–C5 intermediate tree는 독립 import/runtime가 성립하지 않는다. 따라서 **현재 승인 후보 commit order는 C0 → C1 → G1(C2–C5 integrated) → C6**이다. C2–C5는 G1 내부의 semantic ownership·focused validation·feature rollback 구획이며 독립 commit으로 stage하지 않는다. Task 0.3/0.4/M1.1 구현 중 공통 lifecycle/pipeline contract를 먼저 추출하고 실제 intermediate index tree의 compile/schema/focused test를 증명한 경우에만 C2 → C3 → C4 → C5 commit 분리를 다시 제안한다.

각 commit은 자기 schema/config hunk를 먼저 적용하고 repository/backend/API/test를 같은 commit에서 세운다. `server/db.py`, `server/config.py`, `server/api.py` 같은 split-owned 파일은 아래 semantic selector를 사용하며 line number만으로 stage하지 않는다. selector가 겹치거나 import/schema/focused test가 독립적으로 서지 않으면 dependent hunk를 G1에 합치고 사용자에게 변경된 manifest를 다시 제시한다.

각 logical slice는 최소 명시된 focused test를 통과하고, 실제 commit 후보 C0/C1/G1/C6는 `python -m compileall -q server`, 해당되는 web test/build, `git diff --check`를 통과해야 한다. G1과 C6 후에는 공통 offline full gate를 다시 실행한다. 현재 결합 working tree의 green 결과를 intermediate commit green 증거로 대체하지 않는다. live vendor/GitHub/Gateway/webhook/Slack 호출, stage, commit, push, rebase는 이 Task에서 실행하지 않는다.

#### C0 — inventory와 repository hygiene

- **Whole-path ownership:** `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1-inventory.md`, `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1.md`, `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1-review.md`.
- **`.gitignore` hunk ownership:** generated dependency/IDE/local-agent 규칙(`node_modules/`, `.idea/`, `.pi-subagents/`, `.playwright-mcp/`)은 C0, Safe-DB runtime 규칙(`.safe-db-locks/`, `.safe-db-audit.jsonl`)은 C4. Task 0.1이 실제로 추가한 hunk는 `.playwright-mcp/` 하나뿐이며 다른 pre-existing hunk를 Task 0.1 작업으로 주장하지 않는다.
- **Validation:** inventory의 B0/B1/B2/B3 SHA, exact 123-row classification, `git check-ignore -v .playwright-mcp`, `git diff --check`.
- **Rollback:** 문서와 ignore rule만 되돌린다. 로컬 생성물 자체를 삭제하지 않는다.

#### C1 — management security, route boundary, loading/error shell

- **Whole-path ownership:** `server/http_security.py`, `server/routes/__init__.py`, `server/routes/harness.py`, `web/src/App.tsx`, `web/src/App.test.tsx`, `web/src/api.ts`, `web/src/index.css`, `web/src/components/env-status.tsx`, `web/src/components/page-head.tsx`, `web/src/components/repo-tabs.tsx`, `web/src/components/repo-tabs.test.tsx`, `web/src/components/status-line.tsx`, `web/src/components/loading-state.tsx`, `web/src/components/route-error-boundary.tsx`, `tests/test_security.py`, `tests/test_health.py`.
- **Split hunk ownership:** `server/config.py`의 `ADMIN_TOKEN`/allowed-origin hunk, `server/api.py`의 middleware·public health/deep-health·`harness_router` include 및 기존 inline harness route 제거 hunk, `tests/test_api.py`의 health/harness contract hunk.
- **Order/import/API contract:** config → middleware/router → API tests → SPA bearer transport/auth gate → lazy route/error/loading shell. `web/src/api.ts` bearer wrapper와 `App.tsx` auth gate는 분리하지 않는다. `server/routes/harness.py`와 `api.py` include/remove hunk도 분리하지 않는다.
- **Focused validation:** `.venv/bin/pytest -q tests/test_security.py tests/test_health.py tests/test_harness.py tests/test_api.py -k 'health or harness or auth'`; `cd web && npm test -- --run src/App.test.tsx src/components/repo-tabs.test.tsx && npm run build`.
- **Rollback:** token 미설정은 기존 loopback-open 동작을 보존한다. token이 설정된 배포에서 server security만 rollback하면 관리 API가 다시 노출되므로 backend와 auth UI를 같은 rollback 단위로 취급한다.

#### C2 — process lease, worker fencing, retention, safe diagnostics

- **Whole-path ownership:** `server/repos/process_repo.py`, `server/retention.py`, `tests/test_config.py`, `tests/test_process_lease.py`, `tests/test_retention.py`.
- **Split hunk ownership:** `server/config.py`의 job/wiki timeout·worker idle·retention TTL hunk; `server/db.py`의 `process_lease`, job/run/wiki owner columns와 claim indexes; `server/repos/job_repo.py`의 owner-aware claim/finalizer/recovery hunk; `server/repos/review_repo.py`의 run owner/terminal-close hunk; `server/repos/wiki_repo.py`의 owner-aware claim hunk; `server/worker.py`의 process registration/heartbeat/lane timeout/startup recovery hunk; `server/api.py` lifespan의 lease·worker lanes·cleanup loop와 safe deep-health/telemetry hunk; 대응 `tests/test_db.py`, `tests/test_job_repo.py`, `tests/test_worker.py`, `tests/test_api.py` hunk.
- **Migration/order:** additive lease/owner schema → repository fencing → worker/lifespan loops → retention. 현재 구현은 `RETENTION_DAYS=0`이어도 lifespan이 diagnostic/context cleanup loop를 시작하고 기본 7일 TTL로 raw file, `raw_path`, `context_text`, `context_chunks`를 비가역 삭제한다. C2/G1 commit-ready 전 cleanup 전체를 default-off activation gate 뒤로 옮기고 명시적 enable에서만 시작하도록 수정·테스트한다.
- **Focused validation:** `.venv/bin/pytest -q tests/test_config.py tests/test_db.py tests/test_process_lease.py tests/test_retention.py tests/test_job_repo.py tests/test_worker.py tests/test_api.py -k 'lease or owner or timeout or retention or telemetry or deep_health'`. default-off에서 cleanup call/delete 0, opt-in에서만 bounded cleanup이 실행되는 테스트를 포함한다.
- **Rollback:** 새 worker를 먼저 중지하고 lease TTL/owner 상태를 확인한 뒤 구 worker를 시작한다. cleanup opt-in 이후 삭제된 DB row/file은 복구 불가하므로 activation 직전 대상·TTL·backup/restore 상태와 rollback 불가를 제시해 별도 승인을 받는다. G1 code rollback은 C2–C5 전체 revert이며 개별 feature는 default-off/toggle로 먼저 비활성화한다.

#### C3 — GitHub sync/posting idempotency와 PR lifecycle

- **Whole-path ownership:** `server/poller.py`, `server/repos/posted_repo.py`, `server/repos/post_operation_repo.py`, `server/repos/pr_repo.py`, `tests/test_gh.py`, `tests/test_overview.py`, `tests/test_poller.py`, `tests/test_post.py`, `tests/test_post_slack.py`, `tests/test_review_trigger.py`.
- **Split hunk ownership:** `server/db.py`의 `github_post_operation`과 `finding.posting_operation_id`; `server/github/gh.py`의 bounded open-PR/list/review/post lookup hunk(현재 PR context read hunk는 C4); `server/repos/job_repo.py`의 enqueue/revive/stale-head/closed-PR lifecycle hunk; `server/repos/finding_repo.py`의 posting reservation/status hunk; `server/repos/repo_repo.py`의 `commit=False` transaction API와 repo 삭제 시 `github_post_operation`을 FK-safe 순서로 처리하는 hunk; `server/repos/pr_repo.py`의 `commit=False` API; `server/worker.py`의 disabled/closed/stale-head 취소 hunk; `server/api.py`의 overview job state, manual trigger, post preview/freshness/server-side posting policy/operation adoption/finalize hunk; 대응 `tests/test_api.py`, `tests/test_job_repo.py`, `tests/test_worker.py` hunk.
- **Migration/order:** posting table/FK → operation repository → repo deletion/CASCADE contract → poll transaction and job lifecycle → posting API. post operation이 있는 repo 삭제가 `FOREIGN KEY constraint failed` 없이 정해진 cleanup 정책을 따르는 회귀 테스트가 선행돼야 한다. `github_post_operation`, marker/adoption, finding reservation과 remote mutation hunk는 하나의 rollback 단위다.
- **Focused validation:** `.venv/bin/pytest -q tests/test_gh.py tests/test_poller.py tests/test_job_repo.py tests/test_worker.py tests/test_overview.py tests/test_post.py tests/test_post_slack.py tests/test_review_trigger.py tests/test_api.py -k 'post or poll or enqueue or closed or stale or trigger or overview'`.
- **Rollback:** offline fake transport까지만 실행한다. 실제 remote review가 생성된 뒤에는 DB rollback으로 되돌릴 수 없으므로 marker/adoption row를 보존하고 forward-fix한다. live GitHub read/write와 webhook/Slack은 각각 별도 승인 전 `not_run`이다.

#### C4 — governed context, live MSSQL metadata, review rules와 Wiki

- **Whole-path ownership:** `docs/context-provider-contract.md`, `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md`, `docs/superpowers/specs/2026-07-20-live-mssql-introspection.md`, `server/context/base.py`, `server/context/composite.py`, `server/context/db_schema_source.py`, `server/context/jira_provider.py`, `server/context/registry.py`, `server/context/source_provider.py`, `server/context/static_provider.py`, `server/context/current_pr_reviews_source.py`, `server/context/live_mssql_source.py`, `server/context/review_rules_source.py`, `server/context/status.py`, `server/repos/review_rule_repo.py`, `server/repos/settings_repo.py`, `server/safe_db/ORIGIN.md`, `server/safe_db/__init__.py`, `server/safe_db/sql_gateway.py`, `server/wiki.py`, `tests/test_context.py`, `tests/test_live_mssql_source.py`, `tests/test_review_rules.py`, `tests/test_wiki.py`.
- **Split hunk ownership:** `.gitignore`의 Safe-DB lock/audit 규칙; `server/config.py`의 MSSQL Gateway config; `server/db.py`의 current-PR context toggle, `live_db_target_id`, `review_rule` table; `server/github/gh.py`의 current PR reviews/comments read hunk; `server/repos/repo_repo.py`의 context/live target allowlist; `server/repos/wiki_repo.py`의 source/catalog persistence hunk; `server/worker.py`의 Wiki timeout/catalog runtime hunk; `server/api.py`의 repo context settings, context-status, learn/rule propose/activate/disable hunk; 대응 `tests/test_api.py`, `tests/test_db.py`, `tests/test_worker.py` hunk.
- **Migration/order:** additive settings/rule schema → guarded SQL Gateway → context providers/registry/status → API/settings/rules → Wiki evidence. `live_mssql_source.py`와 `server/safe_db/**`는 security claim 때문에 분리하지 않는다. Wiki prompt/reference 형식과 evidence validator도 분리하지 않는다.
- **Focused validation:** `.venv/bin/pytest -q tests/test_context.py tests/test_live_mssql_source.py tests/test_review_rules.py tests/test_wiki.py tests/test_api.py tests/test_db.py tests/test_worker.py -k 'context or mssql or review_rule or wiki or live_db'`.
- **Rollback:** context source/global/repo toggle을 먼저 off하고 Gateway URL/token을 제거한다. additive rule/catalog data는 inert 상태로 남긴다. live MSSQL Gateway 호출은 별도 승인 전 `not_run`이다.

#### C5 — deterministic review engine, telemetry, snapshot, retry와 verification

- **Whole-path ownership:** `docs/vendor-cli-contract.md`, `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup.md`, `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup-review.md`, `harness/default/review-system-prompt.md`, `scripts/review-cli-telemetry-preflight.py`, `scripts/review-read-containment-preflight.py`, `server/models.py`, `server/pipeline.py`, `server/review/diff_filter.py`, `server/review/finding_policy.py`, `server/review/findings_schema.py`, `server/review/gh_deps.py`, `server/review/pipeline_contracts.py`, `server/review/prescreen.py`, `server/review/rollout.py`, `server/review/snapshot.py`, `server/review/vendor_telemetry.py`, `server/review/vendors.py`, `server/review/verify.py`, `server/review/worktree.py`, `tests/test_diff_filter.py`, `tests/test_findings_schema.py`, `tests/test_gh_deps.py`, `tests/test_harness.py`, `tests/test_pipeline.py`, `tests/test_prescreen.py`, `tests/test_review_snapshot.py`, `tests/test_vendor_telemetry.py`, `tests/test_vendors.py`, `tests/test_verify.py`.
- **Approved future-path ownership:** 현재 B3에서 clean인 `server/review/harness.py`가 Task 0.3/M1.1에서 변경되면 C5/G1에 귀속한다. 첫 write 전에 B3 raw SHA를 재확인하고, write 후 이 path만 추가된 expected transition과 갱신된 ownership count를 기록한다. Task 0.1의 B0∪B1 historical universe를 재작성하지 않는다.
- **Split hunk ownership:** `server/config.py`의 scope/dedupe rollout·snapshot limits; `server/db.py`의 context chunks, execution envelope, ownership/scope/dedupe/verification fields; `server/repos/finding_repo.py`의 scope/dedupe/verification persistence; `server/repos/repo_repo.py`의 policy override; `server/repos/review_repo.py`의 context chunks/execution envelope/retry metadata; `server/api.py`의 policy payload, run context/vendor telemetry, raw-output denial, failed-vendor retry, finding mutation guards; 대응 `tests/test_api.py`, `tests/test_db.py` hunk.
- **Migration/order:** schema → `pipeline_contracts`/diff ownership → context hash binding → vendor execution envelope → snapshot/worktree → pipeline/retry → verification/API. `server/pipeline.py:_execute_run`과 retry path는 같은 hash/ownership metadata를 생산·소비하므로 기계적으로 나누지 않는다.
- **Hard preconditions:** Task 0.3의 Claude legacy gate, vendor-isolated credential runtime, cleanup diagnostics와 Task 0.4의 complete retry identity를 먼저 구현한다. M1.1의 bounded stream reader, unknown-key-name suppression, report caps를 통과하기 전 telemetry preflight/live activation을 commit-ready로 선언하지 않는다.
- **Focused validation:** `.venv/bin/pytest -q tests/test_diff_filter.py tests/test_findings_schema.py tests/test_gh_deps.py tests/test_harness.py tests/test_pipeline.py tests/test_prescreen.py tests/test_review_snapshot.py tests/test_vendor_telemetry.py tests/test_vendors.py tests/test_verify.py tests/test_api.py tests/test_db.py`.
- **Rollback:** scope/dedupe는 observe default와 kill switch를 유지하고 structured telemetry는 exact attestation 없는 version에서 legacy/unavailable로 degrade한다. snapshot은 OS sandbox로 주장하지 않는다. live vendor/read-containment probe는 clean VM/dedicated account와 별도 승인 전 `not_run`이다.

#### C6 — feature UI wiring, rollout docs와 benchmark smoke

- **Whole-path ownership:** `README.md`, `benchmarks/review_pipeline/fixtures/synthetic_scope_dedupe.json`, `docs/review-pipeline-rollout.md`, `scripts/review-pipeline-benchmark.py`, `tests/test_review_benchmark.py`, `web/src/sections/HarnessSection.tsx`, `web/src/sections/LearnSection.tsx`, `web/src/sections/LearnSection.test.tsx`, `web/src/sections/ReviewSection.tsx`, `web/src/sections/ReviewSection.test.tsx`, `web/src/sections/SettingsSection.tsx`, `web/src/sections/SettingsSection.test.tsx`, `web/src/sections/WikiSection.tsx`.
- **API prerequisites:** C2 overview/runtime state, C3 posting/retry lifecycle, C4 context-status/rules/Wiki, C5 run context/execution telemetry/scope-dedupe fields가 먼저 고정돼야 한다. ReviewSection의 run request identity state와 render gate, SettingsSection의 context-status schema/fallback, LearnSection의 `repo_id`/`review_rules` contract는 각각 분리하지 않는다.
- **Validation:** `.venv/bin/pytest -q tests/test_review_benchmark.py`; `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/review-pipeline-benchmark.py --output /tmp/review-pipeline-benchmark.json`; `cd web && npm test -- --run && npm run build`; 이후 Python 전체 test와 `git diff --check`.
- **Rollback:** UI/docs/synthetic fixture는 backend additive contract를 남긴 채 되돌릴 수 있다. synthetic 3-finding smoke는 enforcement unlock evidence가 아니다. private corpus/result를 만들기 전에 `benchmarks/review_pipeline/private/`와 `benchmarks/review_pipeline/results/` ignore rule을 별도 승인된 C6 hunk로 추가한다.

#### Split-owned path/hunk completeness rule

다음 path는 whole-file stage 금지이며 위 semantic ownership이 authoritative하다: `.gitignore`, `server/api.py`, `server/config.py`, `server/db.py`, `server/github/gh.py`, `server/repos/finding_repo.py`, `server/repos/job_repo.py`, `server/repos/repo_repo.py`, `server/repos/review_repo.py`, `server/repos/wiki_repo.py`, `server/worker.py`, `tests/test_api.py`, `tests/test_db.py`, `tests/test_job_repo.py`, `tests/test_worker.py`. 그 밖의 현재 B0∪B1 intended path는 위 C0–C6의 whole-path ownership에 정확히 한 번 귀속된다. 현재 status 116 paths는 101 whole-path + 15 split-owned로 exact coverage가 검증됐다. `server/review/harness.py` 같은 승인된 후속 task path는 별도 expected-transition 기록 후 manifest를 확장한다. `server/pipeline.py`와 `tests/test_pipeline.py`는 크지만 내부 계약 결합 때문에 C5 whole-path이며, frontend section은 backend API가 모두 선 뒤 C6 whole-path로 둔다.

#### Known blockers와 external-effect gates

- C2 worker/lifespan과 C5 pipeline/`gh_deps` 계약이 결합돼 있어 현재 C2–C5는 G1 단일 commit 후보다. independent commit 분리는 실제 intermediate tree 검증 전 금지한다.
- Claude `2.1.198` structured activation, 양 vendor credential materialization, unbounded preflight output, unknown event key 이름 노출은 C5/G1 commit approval 전 blocker다.
- repo 삭제의 `github_post_operation` FK-safe cleanup과 회귀 테스트가 C3/G1 blocker다.
- diagnostic/context cleanup은 현재 기본 활성·비가역이므로 default-off activation gate와 별도 production enable 승인 전 C2/G1 commit-ready가 아니다.
- retention/diagnostic delete, live vendor/read-containment probe, live MSSQL, GitHub sync/post, webhook ingress, Slack과 benchmark collector/model run은 각각 명시적 승인 전 실행하지 않는다.
- stage/commit 전 parent가 실제 diff에서 위 ownership manifest를 다시 계산하고, 실제 C0/C1/G1/C6 candidate index의 compile/schema/focused/full gate 결과와 commit message를 사용자에게 제시한다. push/rebase는 commit 승인과도 별개다.

**Acceptance**

- 현재 intended 116 paths는 101 whole-path와 15 split-owned path로 정확히 한 번 귀속되고, 승인된 후속 task path의 baseline transition 규칙이 정의된다.
- C2–C5 logical slice의 schema/import/API/test 선행관계와 feature rollback은 명확하며, 현재 실제 commit boundary는 G1 하나임을 숨기지 않는다.
- lifecycle/pipeline contract를 분리해 intermediate tree가 검증되기 전 C2–C5를 독립 commit으로 표시하지 않는다.
- blocker가 있는 G1은 구현·검증 전 commit-ready로 표시하지 않는다.
- commit, staging, history rewrite, external-effect command는 아직 수행하지 않는다.

### Task 0.3: 발견된 blocker 해소

**Files**

- Modify: `server/review/vendors.py`
- Modify: `server/review/vendor_telemetry.py`
- Modify: `server/review/harness.py`, `server/pipeline.py`, `server/review/verify.py`, `server/review/gh_deps.py`, `server/wiki.py` (credential isolation coordinated sub-slice: all `prepare_runtime()` callers)
- Modify: `server/review/snapshot.py`
- Modify: `server/repos/repo_repo.py`, `server/config.py`, `server/api.py`, `server/retention.py`
- Modify: `docs/vendor-cli-contract.md`
- Modify: `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup.md`
- Test: `tests/test_vendors.py`, `tests/test_vendor_telemetry.py`, `tests/test_harness.py`, `tests/test_review_snapshot.py`, `tests/test_api.py`, `tests/test_retention.py`

**Steps**

1. Claude 2.1.198을 성공 attestation 전 structured allowlist에서 제거하거나 explicit attestation gate 뒤로 이동한다.
2. `HarnessProfile.prepare_runtime()`이 선택 vendor의 credential만 준비하도록 계약을 좁힌다.
3. snapshot/runtime cleanup 실패를 `ignore_errors=True`로 숨기지 않고, 잔존 여부와 safe diagnostic code를 검증한다.
4. cancel/timeout/setup failure에서도 source snapshot과 credential runtime이 정리되는 테스트를 추가한다.
5. `github_post_operation`이 있는 repo 삭제를 FK-safe order/CASCADE contract로 고치고 회귀 테스트를 추가한다.
6. diagnostic/context cleanup loop를 default-off activation gate 뒤로 옮기고 opt-in에서만 비가역 delete가 실행됨을 테스트한다.
7. 기존 계획 문서의 테스트 수치와 구현 상태를 현재 기준선으로 갱신한다.

**Acceptance**

- 성공 attestation 없는 Claude structured telemetry가 production에서 켜지지 않는다.
- vendor별 불필요 credential materialization이 없다.
- cleanup 잔존이 조용한 성공으로 처리되지 않는다.
- post operation이 있는 repo 삭제가 FK 오류 없이 정해진 cleanup contract를 따른다.
- diagnostic/context delete는 default-off이며 explicit opt-in에서만 실행된다.
- 전체 offline gate가 다시 녹색이다.

**Implementation evidence (2026-07-23)**

- Claude 2.1.198은 production `event_schema()`에서 제거됐고 attestation-only parser mode만 남았다.
- `HarnessProfile.runtime_credentials()`가 선택 vendor만 준비하며 success/setup failure/cancel/timeout에서 credential 제거를 검증한다. residue는 `runtime_cleanup_failed`로 vendor success를 차단하되 활성 cancellation/timeout 예외를 덮지 않고 safe note/log를 남긴다.
- plain snapshot restore/rmtree failure는 `snapshot_cleanup_failed`로 드러나며 더 이상 `ignore_errors=True`로 숨기지 않는다. 활성 cancellation이 있으면 cancellation을 보존하고 safe note/log를 남긴다.
- diagnostic/context cleanup scheduler는 `ALMIGHTY_DIAGNOSTIC_CLEANUP_ENABLED=0` 기본값에서 생성되지 않고 explicit `1`에서만 시작한다.
- repo 삭제는 finding을 먼저 제거한 뒤 `github_post_operation`, `review_run` 순으로 정리하며 post operation 포함 회귀 테스트가 통과했다.
- focused Task 0.3 tests와 Python full gate(715 collected, 2 skipped, 0 failed), web 105 tests, production build, `git diff --check`가 통과했다.
- Task 0.4 retry identity는 아래 후속 단계에서 완료됐고, M1.1 bounded preflight/signature hardening은 아직 후속 gate다. live probe는 실행하지 않았다.

### Task 0.4: PolicyDecision·run snapshot·retry basis를 Slice 1A에 고정

**Files**

- Modify: `server/db.py`, `server/review/finding_policy.py`, `server/pipeline.py`, `server/repos/review_repo.py`, `server/review/vendor_telemetry.py` (retry envelope schema/identity coordinated sub-slice), `server/api.py`의 `_repo_payload` 경계
- Test: `tests/test_db.py`, `tests/test_pipeline.py`, `tests/test_api.py`, `tests/test_vendor_telemetry.py`

**Design**

`policy_mode()`와 `_repo_payload()`의 중복 계산을 하나의 `PolicyDecision`으로 합친다. `review_run`에는 최초 실행 시 requested/effective scope·dedupe mode, reason, selection source, 2차원 cohort key, policy/config hash, benchmark attestation hash를 nullable/additive migration으로 snapshot한다. legacy row는 현재 설정으로 역추론하지 않고 `unknown`으로 표시한다.

retry는 vendor runner 호출 **전에** 저장된 run/vendor execution metadata에서 아래 retry identity를 구성하고 현재 값과 exact match를 확인한다.

- vendor, model, effort, protocol version, chunker version, scope/dedupe policy decision·hash
- prompt hash, harness/tool/sandbox configuration hash, adapter name/version and adapter input/config hash
- exact CLI version과 event-schema version
- diff hash, context hash와 각 retry chunk hash

하나라도 없거나 달라지면 external runner call count 0으로 `new_full_run_required`를 반환한다. 동일할 때만 최초 run의 policy snapshot을 재사용하며, retry는 현재 repo/settings/env policy를 다시 계산하지 않는다. model/effort/prompt/harness/CLI/schema/diff/context 변경 실행은 새 full run만 허용한다.

**Acceptance**

- API 표시와 pipeline 적용이 같은 decision 함수를 사용한다.
- 실행 후 env/repo 설정이 바뀌어도 과거 run cohort와 retry policy가 변하지 않는다.
- **각 identity별** vendor, model, effort, prompt, harness/tool/sandbox, adapter name/version/input, CLI/event-schema, protocol/chunker/policy, diff/context/각 chunk hash의 mismatch 또는 absence 테스트가 external runner call count 0과 `new_full_run_required`를 검증한다.
- 이 Task가 완료·검토되기 전에는 Milestone 2 E2E를 시작하지 않는다.

**Implementation evidence (2026-07-23)**

- API와 pipeline은 `resolve_policy_snapshot()`의 동일한 frozen `PolicyDecision` pair를 사용한다. 신규 run은 requested/effective mode, reason, selection source, 2차원 cohort, decision/config hash와 optional benchmark attestation hash를 INSERT 시점에 저장한다.
- nullable additive migration은 legacy/partial snapshot을 `unknown`으로 유지하며 현재 설정으로 backfill하지 않는다.
- execution envelope v2는 vendor/model/effort, exact prompt/harness/adapter/CLI/schema/protocol/chunker/policy/diff/context identity를 root에 고정하고, random prompt fence nonce와 per-chunk hash를 attempt에 저장한다.
- retry는 stored policy와 원 prompt nonce를 재사용하고 current config/runner identity를 external vendor call 전에 exact 비교한다. missing/mismatch는 `new_full_run_required`로 fail-closed한다.
- root identity와 각 retry chunk identity의 mismatch/absence matrix는 모든 경우 vendor runner call count 0을 검증한다. current model/effort/harness/adapter/CLI/schema/diff/prompt/policy drift도 같은 pre-call rejection을 검증한다.
- focused Task 0.4 tests와 Python full gate(775 collected, 2 skipped, 0 failed), web 105 tests, production build, `git diff --check`가 통과했다.
- fresh review가 발견한 schema hint/review-only argv binding 누락을 동일 wire-input builder와 normalized runtime-path placeholder로 수정했고, targeted follow-up review는 Blocker/High 없음으로 통과했다.
- M1.1 bounded preflight/signature hardening과 M1.2 live attestation은 아직 후속 gate이며 live probe는 실행하지 않았다.

---

## Milestone 1 — Vendor telemetry 성공 계약

### Task 1.1: bounded, vendor-isolated preflight

**Files**

- Modify: `scripts/review-cli-telemetry-preflight.py`
- Modify: `server/review/vendors.py`
- Modify: `server/review/vendor_telemetry.py`
- Modify: `server/review/harness.py`
- Modify: `server/pipeline.py`, `server/review/verify.py`, `server/review/gh_deps.py`, `server/wiki.py`
- Test: `tests/test_vendor_telemetry.py`, `tests/test_vendors.py`, `tests/test_harness.py`, `tests/test_pipeline.py`, `tests/test_verify.py`, `tests/test_prescreen.py`, `tests/test_wiki.py`

**Steps**

1. preflight의 unbounded `subprocess.run(..., capture_output=True)`를 production bounded stream reader 또는 같은 cap을 공유하는 helper로 교체한다.
2. stdout/stderr/event byte·event count·wall-clock cap을 적용하고 초과 시 `stream_truncated=true`와 safe error만 반환한다.
3. `--vendor`에 따라 필요한 auth만 준비한다.
4. sanitized report의 schema를 고정한다.
   - vendor, public CLI version
   - exit code, allowlisted safe error
   - **exact-version allowlist에 있는** event type/key signature와 count
   - unknown key는 이름을 출력하지 않고 `unknown_key_count` 숫자로만 표현
   - final output/usage/tool-call 존재 여부
   - telemetry status, truncation 여부
5. event key 길이, signature 수, unknown key 수, report 전체 byte cap을 적용한다. 동적 key 이름에 path/secret을 넣은 malicious fixture로 유출이 없음을 검증한다.
6. prompt, response, command, path, stdout/stderr/event body는 report와 fixture에 넣지 않는다.
7. unknown CLI/schema는 legacy/unavailable로 degrade하며 review 자체를 실패시키지 않는다.

**Acceptance**

- 대형/무한 event fixture에서 memory와 output cap이 지켜진다.
- vendor별 auth isolation과 cleanup이 테스트된다.
- sanitized report만 stdout 또는 명시적 `--output`에 기록된다.

**Implementation evidence (2026-07-23)**

- telemetry preflight의 version/vendor subprocess는 production과 같은 bounded drain 계열을 사용한다. version은 4 KiB, vendor stdout/stderr는 각각 10 MiB, wall clock은 15/180초로 제한된다. Claude keychain read도 64 KiB/15초와 process-group cleanup을 적용한다.
- event parser는 10 MiB/20,000 events, public signature는 64개/64-character key, unknown count는 capped numeric only, 전체 report는 32 KiB로 제한된다.
- exact-version public schema에 없는 event type/key 이름은 출력하지 않으며 malicious path/secret-shaped dynamic key fixture가 report에 나타나지 않음을 검증한다.
- output/final-message overflow는 raw/partial body 없이 `output_limit`, `stream_truncated=true`의 fixed schema-v2 report로만 표현하고 preflight는 non-zero로 종료한다. Production legacy/structured adapter도 stdout/stderr/final-file truncation을 실패 처리한다.
- `--vendor`별 runtime credential context와 cleanup을 유지하며 stdout 또는 explicit `--output`에는 sanitized report만 기록한다.
- focused M1.1 tests와 Python full gate(794 collected, 2 skipped, 0 failed), web 105 tests, production build, `git diff --check`가 통과했다.
- fresh review의 truncation, unknown-CLI, process-group, schema/version binding, keychain bound finding을 반영했고 async cancellation descendant 회귀 테스트까지 추가했다. targeted follow-up 이후 부모 focused/full gate가 통과했다.
- live telemetry probe는 실행하지 않았고 M1.2 attestation 상태는 `not_run`이다.

### Task 0.5: Slice 1A integrated offline gate·fresh review·commit approval

**Task 0.5 precondition:** Task **0.1, 0.2, 0.3, 0.4 및 M1.1**을 모두 구현·focused 검증한 뒤에만 Slice 1A를 하나의 integration 대상으로 검증한다. `.venv/bin/pytest -q`, web test/build, `git diff --check`, telemetry/retry focused tests를 실행하고 fresh-context correctness/regression과 security/privacy reviewers가 **현재 diff**를 직접 검토한다. blocker/high disposition과 affected rerun 뒤 Slice 1A commit series의 파일/hunk 목록과 commit message를 사용자에게 제시한다.

명시적 사용자 승인 후에만 Slice 1A를 stage/commit한다. push/rebase는 별도 승인 대상이다. M1.2 live attestation, Slice 1B E2E, benchmark live run은 이 gate의 일부가 아니다.

**Acceptance**

- Task 0.1–0.4의 inventory/ownership/retry basis와 M1.1 telemetry contract가 같은 offline full gate와 fresh review를 통과한다.
- 승인된 intended change가 commit series에 누락 없이 포함되고 generated/private/unknown 파일은 제외된다.
- 각 commit은 최소 import/schema/focused test가 성립하고 최종 series는 full gate가 녹색이다.
- 승인되지 않은 pre-existing 변경을 수정·되돌림·stage하지 않는다.

**Task 0.5 offline evidence (2026-07-23):**

- fresh correctness/security review에서 CLI cache identity, bounded shutdown, webhook body cap, prescreen argv/tool boundary, snapshot pre-write quota, MSSQL response streaming의 High 6건을 식별해 모두 수정했다.
- targeted follow-up에서 Codex legacy fallback version binding, cancellation-suppressing shutdown deadline, explicit `--tools ""`, MSSQL total response deadline의 High 4건을 추가로 식별해 회귀 테스트와 함께 수정했다.
- CLI version은 identity 계산과 실제 invocation 직전에 다시 확인하며 invocation 내부 argv/parser에는 한 값을 고정한다. mismatch는 vendor runner 호출 전에 `new_full_run_required`다.
- background shutdown은 grace와 cleanup deadline 합계가 lease TTL보다 짧고, deadline 초과 시 lease를 release하지 않은 채 명시적으로 실패한다.
- GitHub/Slack webhook은 secret·필수 signature header를 body 전에 검사하고 Content-Length/chunked stream 모두 1 MiB 기본 상한을 적용한다.
- prescreen은 source prompt를 stdin으로만 전달하고 `--tools ""`/slash-command disable, bounded output, process-group timeout을 적용한다.
- snapshot은 `ls-tree` blob/file budget preflight 후 byte-limited archive stream을 생성하며, MSSQL Gateway는 Content-Length/chunk cap과 독립 total deadline을 적용한다.
- 최종 offline gate는 Python 805 collected, 2 skipped, 0 failed, web 105 tests, production build, `compileall`, `git diff --check`를 통과했다.
- 실제 candidate tree 검증 결과 C1의 `api.py`/`config.py` shutdown·webhook security hunk가 C2 lifecycle과 겹치므로 승인 후보를 **C0 → G1(C1–C5 integrated) → C6**로 축소한다. split-owned backend/test path는 부분 stage하지 않고 G1 whole-file로 승격한다.
- **C0 (4 paths), `docs: record production readiness inventory`:** `.gitignore`와 이 sprint의 plan/review/inventory 문서 3개. `git diff --check`, base compile, `.playwright-mcp` ignore 확인 통과.
- **G1 (100 paths), `feat: harden review pipeline production contracts`:** 현재 117-path universe에서 C0 4개와 아래 C6 13개를 제외한 모든 path. Python full gate, C1 core web 4 tests, production build, compileall/diff check 통과.
- **C6 (13 paths), `feat: wire review pipeline rollout experience`:** `README.md`, `benchmarks/review_pipeline/fixtures/synthetic_scope_dedupe.json`, `docs/review-pipeline-rollout.md`, `scripts/review-pipeline-benchmark.py`, `tests/test_review_benchmark.py`, `web/src/sections/{HarnessSection.tsx,LearnSection.tsx,LearnSection.test.tsx,ReviewSection.tsx,ReviewSection.test.tsx,SettingsSection.tsx,SettingsSection.test.tsx,WikiSection.tsx}`. 최종 Python 805/web 105/build/compileall/diff check 통과.
- 위 candidate 검증은 detached temporary worktree에서 수행하고 정리했으며 실제 index/stage/commit은 변경하지 않았다.
- live/vendor/GitHub/Gateway 호출 없이 offline gate만 수행했으며 staged 파일은 없다.

### Task 1.2: live probe와 attestation evidence (Slice 1A tooling 완료와 별도)

Task 1.1과 Task 0.4의 offline tooling 완료는 live attestation pass를 뜻하지 않는다. 이 Task는 승인된 clean VM/dedicated OS account에서만 별도 evidence를 만든다.

**External-effect gate — 실행 직전 사용자 확인 필요**

다음을 제시하고 확인받는다.

- vendor와 exact model
- 최대 호출 수와 예상 subscription/cost 영향
- intended input은 synthetic `probe.txt`뿐이지만 cwd 밖 read는 기술적으로 containment되지 않음
- clean VM/dedicated OS account에 회사 checkout·credential·secret이 없음을 operator가 확인함
- keychain/auth 접근과 임시 credential materialization
- raw provider output을 저장하지 않음

**Commands after approval**

```bash
.venv/bin/python scripts/review-cli-telemetry-preflight.py \
  --live --vendor codex --codex-model <approved-model> \
  --output /tmp/codex-telemetry-attestation.json

.venv/bin/python scripts/review-cli-telemetry-preflight.py \
  --live --vendor claude --claude-model <approved-model> \
  --output /tmp/claude-telemetry-attestation.json
```

**Pass conditions per vendor**

- exit code 0
- `final_output_present=true`
- `usage_present=true`
- `telemetry_status=ok`
- expected exact-version event signature
- Claude는 `tool_calls >= 1`인 real tool call
- raw content/path/command leakage 0

Claude가 통과해도 M1.2는 reviewed event signature, `tool_calls >= 1` evidence, synthetic parser fixture를 **sanitized attestation artifact에만** 기록한다. production exact-version allowlist는 계속 legacy-disabled로 남는다. activation은 Task 0.5 이후 별도 tested/fresh-reviewed commit과 그 commit의 별도 사용자 승인으로만 가능하다.

**Stop conditions**

- schema mismatch, unknown event contract
- final output 또는 usage 부재
- truncation
- unexpected secret/path/content field
- auth/keychain GUI prompt 또는 cleanup 잔존
- Claude 성공 미확인

실패 시 해당 vendor는 legacy/unavailable 상태로 남기고 E2E에서 structured telemetry 완료라고 주장하지 않는다.

**Current evidence status (2026-07-23):** Codex `not_run`, Claude `not_run`. 현재 checkout은 inventory상 `.env`, runtime DB/raw/clone roots가 존재하는 일반 개발 계정이며 clean VM/dedicated OS account precondition을 충족하지 않는다. 따라서 vendor/model/call-cost manifest를 승인받더라도 이 환경에서는 probe를 실행하지 않는다. Codex telemetry와 Claude production structured activation은 각각 legacy/unavailable 상태로 유지한다.

---

## Milestone 2 — Sandbox GitHub E2E rehearsal

### Slice 1B no-write external-effect approval — Task 2.1/2.2 직전

M2.1/M2.2를 실행하기 직전에 사용자에게 다음 exact manifest를 제시하고 한 번의 **no-write rehearsal 승인**을 받는다: canonical repo/PR/head, independent operator allowlist의 immutable SHA-256, isolated read-credential capability attestation/fingerprint/installation ID/permission set/expiry, vendor/model, 최대 vendor call·token·cost, clean VM/dedicated OS account 확인, 완전 pagination 비교 기준과 expected remote mutation 0. 이 승인에는 post, push, webhook delivery, worker job execution, commit/push가 포함되지 않는다.

### Task 2.1: fail-closed live rehearsal harness

**Files**

- Create: `scripts/sandbox-e2e.py`
- Modify: `tests/test_e2e_smoke.py`
- Modify: `tests/test_e2e_diagnostics.py`
- Test: `tests/test_e2e_smoke.py`, `tests/test_e2e_diagnostics.py`, 관련 API/worker/post/webhook tests
- Modify: `README.md`

**Required inputs**

- exact sandbox `owner/repo`
- exact PR number
- optional expected head SHA
- selected vendor/model
- phase: `review`, `retry`, `post-verify`, `webhook-verify`
- explicit write flag for post phase

**Fail-closed guards**

1. repo와 PR은 독립 operator-owned default-deny allowlist의 canonical `owner/repo#PR` entry와 정확히 일치해야 한다. runner가 allowlist를 생성·수정할 수 없다.
2. review/retry에는 새 0700 `GH_CONFIG_DIR`의 isolated read credential만 inject한다. ambient GitHub token variables/native `gh` auth는 strip하고 credential fingerprint/installation ID, canonical repo/PR, immutable operator-allowlist hash, permission set, expiry를 attestation과 exact match시킨다. mismatch·expiry·write capability 또는 cleanup failure는 시작을 거부한다.
3. PR 번호가 없거나 열린 PR 전체 조회로 fallback하지 않는다.
4. 임시 E2E DB를 사용하고 production `almighty.db`를 열지 않는다.
5. 기본적으로 post/push/webhook configuration/Slack을 금지한다.
6. review/retry phase는 independently attested read-only GitHub capability를 사용한다. capability scope를 API/credential metadata로 확인할 수 없으면 시작을 거부한다; write-capable/ambient credential은 모두 거부한다. read credential은 모든 success/failure/timeout/cancel exit에서 zero-residue cleanup을 검증한다.
7. 실행 전후 PR review, inline review comment, conversation comment, head ref를 **완전 pagination**해 각각 snapshot하고 remote mutation 0건을 검증한다. GitHub API cap/페이지 상한/응답 truncation에 닿으면 비교를 통과시키지 않고 preflight를 거부한다.
8. `ALMIGHTY_POST_BANNER`와 rehearsal operation marker를 강제한다.
9. source fixture는 synthetic/non-proprietary 변경만 허용한다. intended input은 synthetic이지만 cwd 밖 read가 containment되지 않으므로 clean VM/dedicated OS account 조건을 다시 확인한다.
10. live run의 raw transcript를 저장하지 않는다.

### Task 2.2: review와 partial retry 시나리오

**Scenario A — sync → review, remote write 0**

1. exact PR만 sync한다.
2. 정확히 한 PR/head job이 enqueue됐는지 확인한다.
3. worker를 실행한다.
4. job/run이 `done`인지 확인한다. 기존처럼 `failed` terminal job을 pass로 인정하지 않는다.
5. 선택 vendor의 성공 result, execution envelope, chunk status, snapshot protocol을 확인한다.
6. findings가 있으면 schema와 posting eligibility를 확인한다. 알려진 synthetic defect를 놓치면 품질 gate 실패로 기록한다.
7. PR reviews, inline review comments, conversation comments, head ref가 실행 전후 모두 동일함을 확인한다.

**Scenario B — deterministic partial → retry**

1. model/effort identity는 바꾸지 않고 E2E-only fail-once adapter 또는 runner fault injection으로 특정 vendor/chunk를 partial/failed 상태로 만든다.
2. fault를 해제한 뒤 retry API/worker를 실행한다.
3. 실패 vendor/chunk만 재실행되고 성공 chunk는 재사용되는지 확인한다.
4. retry는 외부 vendor 호출 **전에** 기존 `vendor_result.execution_meta`와 run snapshot에서 vendor/model/effort/protocol/chunker/policy identity를 로드한다.
5. 현재 repo/settings/env identity가 저장 identity와 다르면 vendor를 호출하지 않고 `new_full_run_required`로 거부한다. 새 identity 실행은 새 run으로만 처리한다.
6. 저장 identity가 일치하면 기존 run policy decision을 사용하며 현재 repo/env policy를 다시 계산하지 않는다.
7. `attempts[]`가 overwrite되지 않고 append되며 model/effort, diff/context/chunk hash가 일치하는지 확인한다.
8. 최초 run 뒤 **model, effort, prompt, harness/tool/sandbox, adapter, CLI/event-schema, protocol/chunker/policy, diff/context/각 chunk hash**를 각각 하나씩 바꾸거나 제거한 pre-call tests에서 external runner call count 0과 새 run 요구를 검증한다.

**Acceptance**

- exact repo/PR 이외 job 0
- failed terminal job을 성공으로 오판하지 않음
- retry 대상과 attempt append가 결정적으로 검증됨
- remote write 0
- runtime/snapshot cleanup 잔존 0

### Task 2.3: post와 webhook 검증

**Task 2.3 server-enforced posting-policy scope (implementation before post phase)**

- Modify: `server/api.py`, `server/github/gh.py`, `server/formatter.py`, `server/repos/posted_repo.py`, `server/repos/post_operation_repo.py` 및 필요한 post operation boundary
- Test: `tests/test_post.py`, `tests/test_api.py`, `tests/test_gh.py`, `tests/test_security.py`
- Server가 approved operation identity, create/update/fallback/inline allowlist, write credential separation, marker/body hash와 second-replay no-op payload을 강제한다. preview는 설명용일 뿐 server-side policy를 우회하거나 broaden할 수 없다.

**External-write gate — 단계별 사용자 확인 필요**

#### Post phase

1. 대상 repo/PR과 **operation identity**(canonical repo/PR/head, run/vendor, marker, canonical body hash, policy/review identity)를 포함한 exact mutation manifest를 사용자에게 보여 준다: create/update/fallback 여부, vendor별 PR review 수, inline/conversation comment 수, request-ID, banner/marker, 앱 Slack 비활성 상태.
2. 전용 sandbox가 GitHub Action/webhook/relay 등 downstream integration을 비활성화했음을 확인한다. 비활성화할 수 없으면 예상 side effect를 별도로 승인받는다.
3. post 승인 뒤에만 short-lived least-privilege **write** credential을 별도 새 0700 `GH_CONFIG_DIR`에 inject한다. credential fingerprint/installation ID, canonical repo/PR, permission set, expiry, immutable operator-allowlist hash를 manifest와 exact match한다. read credential을 승격/재사용하지 않는다.
4. rehearsal 기본값은 기존 Almighty review update와 update-404→create fallback을 거부한다. inline posting은 기본 비활성화하며 필요 시 각 inline body에 operation marker를 포함하고 preview에 모두 표시한다.
5. 승인 후 `--allow-post`로 server-enforced mutation manifest 그대로 한 번 게시하고 operation identity, first response, persisted DB state, transport-level mutating HTTP method+endpoint+request-ID counter를 저장한다.
6. GitHub에서 review ID, inline IDs, exact marker/body hash를 다시 조회한다.
7. idempotency key는 exact operation identity다. 두 번째 **successful** replay는 first response와 같을 필요가 없지만 stable explicit no-op payload `{operation_id, replayed: true}`를 반환해야 한다. DB state는 바뀌지 않고 transport-level mutating HTTP method+endpoint+request-ID counter는 모두 0이어야 한다. crash-gap, adoption, concurrent replay는 recording fake로만 검증한다.
8. 예상 밖 mutation이면 즉시 write credential을 revoke/disable하고 operation/request/review/comment ID를 보고한다. cleanup은 자동 수행하지 않으며 사용자가 승인한 수동 cleanup만 한다. write credential은 모든 success/failure/timeout/cancel exit에서 revoke/zero-residue cleanup을 검증한다.

#### Webhook phase

Slice 1B의 기본 완료 조건은 **offline/in-process** side-effect-free signed payload replay integration이다. replay profile은 temp DB, background poller/worker/retention/notification 0, public network listener/ingress 0, request-body cap test, webhook route handler만 호출, vendor call count 0·GitHub write call count 0을 assert한다. replay는 enqueue 결과도 소비하지 않는다. trusted-proxy, HTTPS assertion, public listener/probe는 Slice 1D dedicated ingress profile에만 속하며 Slice 1B replay의 조건이 아니다.

**Actual webhook delivery는 Slice 1D 이후로 이동한다.** M2에는 actual delivery를 포함하지 않는다. 1D 뒤 위 dedicated ingress profile이 구현·offline 검증되고 별도 승인된 경우에만: 사용자가 이미 구성/승인한 sandbox webhook과 synthetic push를 사용하고, consumer stopped 상태로 delivery ID와 `synchronize` action 및 정확히 한 job enqueue만 확인한다. 그 exact job의 worker 실행은 다시 별도 사용자 승인을 요구한다.

서버가 branch/push/webhook을 자동 생성·변경·삭제하지 않는다. signed replay만 실행한 경우 결과를 `integration_passed`, actual delivery/job execution은 각각 `not_run`으로 구분한다.

**Acceptance**

- Slice 1B에는 sync → review → retry → post idempotency → side-effect-free signed replay 결과가 단계별 artifact로 남는다.
- actual webhook delivery/new-head enqueue는 Slice 1D dedicated ingress profile 뒤 별도 artifact로만 남긴다.
- 실제 실행하지 않은 단계는 통과로 표시하지 않는다.
- sandbox marker 외 원격 변경이 없다.

**Current offline evidence status (2026-07-23):** `scripts/sandbox-e2e.py` preflight/credential/allowlist/temporary-DB/paging/snapshot guards, server-enforced recording post transport/replay policy, and in-process signed webhook replay tests are implemented and pass offline. Exact target/head, hash-pinned separate credential attestation, actual token fingerprint, strict 0700 GitHub config, new private DB, complete pagination, legacy operation reconciliation, single/multi replay schema, inline marker, Slack suppression과 vendor/GitHub-write/worker call count 0을 검증한다. Full Python gate는 824 collected, 1 skipped, 0 failed이며 compileall/diff check가 통과했다. 이 evidence는 live artifact를 만들거나 실행을 승인하지 않는다. Sandbox review, partial retry against an external vendor, GitHub post idempotency against GitHub, and actual webhook delivery remain `not_run`; the latter is still deferred to Slice 1D.

---

## Milestone 3 — Adjudicated benchmark collection foundation

### Task 3.1: provenance와 label 분리 schema

**Files**

- Create: `benchmarks/review_pipeline/README.md`
- Create: `benchmarks/review_pipeline/schema/manifest.schema.json`
- Create: `benchmarks/review_pipeline/schema/adjudication.schema.json`
- Create: `benchmarks/review_pipeline/schema/prediction.schema.json`
- Create: `benchmarks/review_pipeline/schema/run-result.schema.json`
- Create: `benchmarks/review_pipeline/schema/benchmark-report.schema.json`
- Modify: `.gitignore`
- Test: `tests/test_review_benchmark.py`

**Manifest requirements**

- public/synthetic source identifier와 immutable revision SHA
- source URL, license/SPDX, redistribution permission
- patch/content SHA-256
- PR size stratum: small/medium/large
- model-visible input 위치
- provenance approval 상태
- proprietary/Jira/DB/private context 포함 여부는 반드시 false

**Adjudication requirements**

- manifest와 별도 파일
- stable case/issue ID
- issue별 allowed `(file, line range)`, accepted category set, canonical claim rubric/evidence tokens
- known-clean range
- explicit issue-pair label: `same_issue_duplicate | distinct_issue_hard_negative`
- adjudicator pseudonymous ID
- independent verdict, date, disagreement/resolution 상태
- raw personal data 금지

**Canonical scoring contract**

1. `claim_normalization_version`을 schema에 고정한다. scorer는 Unicode NFKC → casefold → line-ending/whitespace collapse → allowed punctuation tokenize → configured stop-token 제거 → stable sorted token sequence를 적용한다. claim rubric은 같은 versioned algorithm으로 만든 **허용 token sequence 목록**이다. prediction은 해당 normalized token sequence가 issue rubric의 한 sequence와 **정확히 동일**할 때만 match하며 fuzzy, subset, similarity threshold를 쓰지 않는다. algorithm/tokenizer hash 불일치는 benchmark invalid다.
2. quality gate용 primary candidate run은 각 case/revision에서 실행 **전에** manifest가 정한 vendor/model/identity, primary repetition index, seed/schedule로 지정한다. primary candidate의 prediction만 quality numerator/denominator에 사용한다. baseline과 추가 repetition은 stability/cost evidence로 별도 report하며 quality denominator에 합치지 않는다.
3. prediction은 primary case ID + allowed location + accepted category + exact allowed token sequence를 모두 만족할 때 issue candidate다. answer의 scope/posting oracle은 versioned diff ownership function + fixed chunker input으로 결정적으로 recompute한다. materialized oracle을 보관할 수 있으나 recomputation과 exact match하지 않으면 benchmark invalid다.
4. 정확히 한 issue와 매칭되면 issue TP, 어느 issue와도 매칭되지 않으면 issue FP다. 여러 issue와 매칭되는 prediction은 unresolved ambiguity이며 `prediction_issue_resolution`이 없으면 benchmark invalid다. resolution 후에도 no issue면 unmatched FP다. 매칭 prediction이 없는 expected issue는 issue FN이다.
5. 같은 issue에 매칭된 복수 prediction은 issue precision/recall에 TP 1건만 세고 나머지는 duplicate prediction으로 audit한다. duplicate candidate-pair universe는 **primary candidate run의** 동일 vendor·normalized file·owner line·category를 가진 모든 unordered prediction pair다. canonical claim-key equality는 universe를 줄이는 조건이 아니라 proposal-positive 판정이다. scorer는 universe 밖 pair를 생성하지 않는다.
6. duplicate TP는 universe 안에서 canonical claim-key가 같아 proposal-positive이고, 두 prediction이 모두 resolve되어 **동일 resolved issue ID**이며 answer의 `same_issue_duplicate` label과 일치할 때만 성립한다. 한쪽이라도 unmatched는 duplicate FP, resolved issue가 다르거나 label과 모순돼도 duplicate FP다. true duplicate paraphrase가 canonical claim-key가 달라 proposal-negative이면 duplicate FN이다. duplicate FN universe는 expected `same_issue_duplicate` label의 unordered prediction-ID pair 중 primary candidate-pair universe에 속하는 모든 pair에서 proposal-positive/TP가 아닌 pair다.
7. hard-negative 30개는 `distinct_issue_hard_negative` label이고 broader primary candidate-pair universe 안에 실제 emitted pair로 존재해야 한다. scorer는 hard-negative와 true paraphrase proposal-negative pair를 private audit artifact에 각각 emitted한다. universe 밖 hard-negative는 gate denominator에 넣지 않는다.
8. scope/posting accuracy는 primary candidate의 모든 prediction-level oracle row를 denominator로 하며 unknown/ambiguous/missing oracle은 제외하지 않고 benchmark invalid다. scorer는 TP/FP/FN, duplicate TP/FP/FN, scope/posting match/mismatch의 stable ID 목록·numerator·denominator를 private audit artifact에 남긴다.

**Artifact separation**

1. model-visible manifest
2. private prediction/run artifact
3. private adjudication answer
4. sanitized aggregate benchmark report

Private prediction에는 stable case ID, prediction ID, file/line, category, normalized claim matching key 또는 사전 정의된 claim ID, scope/posting 상태, duplicate relation을 포함한다. raw model stdout/stderr와 full rationale은 저장하지 않는다. private workspace는 directory `0700`, file `0600`, 명시적 TTL/삭제 절차를 적용하고 Git에서 ignore한다.

Adjudicator 정보는 재식별 가능한 mapping을 수집하지 않는 pseudonymous ID와 date-level 시간만 사용한다. README에 접근 책임자, retention, 삭제·철회·교정 절차, aggregate 재식별 검토를 기록한다.

**Acceptance**

- provenance, license, hash, label 분리, prediction 계약, adjudication이 불완전하면 lint가 거부한다.
- private manifests/predictions/answers/results 디렉터리는 ignore되고 accidental add 검사 대상이다.
- checked-in synthetic smoke는 계속 외부 모델을 호출하지 않는다.

### Task 3.2: collect/lint/blind-run/score 도구

**Files**

- Create: `scripts/review-benchmark-collect.py`
- Create: `scripts/review-benchmark-lint.py`
- Create: `scripts/review-benchmark-run.py`
- Create: `scripts/review-benchmark-score.py`
- Modify: `server/review/rollout.py`
- Test: `tests/test_review_benchmark.py`

**Steps**

1. Slice 1C MVP는 operator가 provision한 **local approved bundle + immutable hash**를 우선 사용한다. remote collector는 default-deny separate operator-owned exact trusted-origin allowlist를 읽기만 하며 runner가 추가·완화할 수 없다.
2. deferred remote collector를 구현할 경우 public/synthetic source만 local private workspace로 가져오고 canonicalized origin이 allowlist와 exact match하는지, redirect의 매 hop도 같은 canonicalization/allowlist를 만족하는지 검증한다. HTTPS-only, URL credential·fragment 거부, loopback/private/link-local IP 거부, download/archive file·byte·time cap, safe extraction을 적용한다. 환경 proxy를 비활성화하고, 검증한 IP에 DNS-pinned connection을 맺되 원래 hostname의 Host/SNI와 TLS 인증서를 검증하며 connected peer IP가 승인 집합과 일치하는지 확인한다. DNS rebinding·redirect·proxy-env 우회 fixture를 포함한다.
3. remote collect 실행 전 source URL/origin, license, 예상 download 크기와 local destination을 보여 주고 사용자 승인을 받는다.
4. lint는 model manifest, private prediction, adjudication answer가 같은 input tree에 들어가지 못하게 한다.
5. runner는 label 없이 동일 head에서 baseline/candidate paired repetitions을 실행한다.
6. live runner 실행 전 exact vendor/model/version, fixture hashes, 최대 호출·token·비용, 전송 데이터, timeout/cancel 기준을 보여 주고 별도 승인을 받는다.
7. private result에는 model/effort/protocol/prompt/diff/context/chunker/CLI/adapter version hash, tokens/tool calls/duration/timeout/partial 상태와 blinded prediction records를 기록한다. Git에 남는 것은 aggregate report뿐이다.
8. scorer는 private prediction과 answer key를 offline join하여 issue-level precision/recall, scope/posting accuracy, duplicate pair precision/recall, issue-level duplicate suppression 영향, strata, partial/timeout coverage, cost regression을 계산한다.
9. 같은 input/hash와 paired repetition·seed·ordering schedule hash를 기록해 baseline/candidate 비교를 재현한다.

**Metric and cost contract**

Report는 각 metric에 `numerator`, `denominator`, `point_estimate`, `wilson_95_lower_bound`, `threshold`, `passed`, `required_sample_shortfall`를 기록한다. Wilson LB는 scoreable Bernoulli metric의 **two-sided 95% Wilson score interval lower endpoint**이며 `z=1.959963984540054`, integer numerator/denominator로 계산하고 unrounded 값으로 gate와 비교한다. rendered rounding은 display-only다. N=0 또는 gate LB를 달성할 최소 denominator가 아직 없으면 `insufficient_sample`로 locked하고 required sample shortfall을 양수로 기록한다.

| Metric | Numerator / denominator | Gate |
|---|---|---|
| issue precision | resolved issue TP / (TP + FP) | point ≥ 0.995; Wilson 95% LB ≥ 0.99 |
| issue recall | resolved issue TP / (TP + FN) | point ≥ 0.95; Wilson 95% LB ≥ 0.90 |
| duplicate precision | duplicate TP / (duplicate TP + duplicate FP) | point = 1.0; denominator ≥ 30; Wilson 95% LB ≥ 0.88 |
| duplicate recall | duplicate TP / (duplicate TP + duplicate FN) | point ≥ 0.95; expected pair denominator ≥ 30; Wilson 95% LB ≥ 0.85 |
| scope accuracy | scope oracle match / all prediction oracle rows | point ≥ 0.995; Wilson 95% LB ≥ 0.99; denominator = `finding_count` |
| posting accuracy | posting oracle match / all prediction oracle rows | point ≥ 0.995; Wilson 95% LB ≥ 0.99; denominator = `finding_count` |

`finding_count`는 primary candidate/baseline paired repetitions을 합친 수가 아니라, unique case/revision의 primary candidate prediction-level oracle rows 수다. primary candidate가 non-invoked setup/preflight failure이면 expected issue는 모두 FN으로 기록하고 run/quality gate 전체를 invalid/locked한다; quality denominator에서 제거하거나 0으로 회피하지 않는다. `issue_count`는 unique adjudicated expected issue ID 수, pair denominators는 위에 정의한 primary candidate universe 또는 expected same-issue pair universe로 독립 보고한다. **이 표에 명시된 Wilson 95% LB만 rollout gate를 구성**하며, N=0은 `insufficient_sample`로 locked다. 100 findings는 더 엄격한 LB에 여전히 부족할 수 있으므로 scorer는 metric별 현재 denominator, required sample shortfall, 달성 가능한/필요한 LB를 report한다.

Cost는 report의 pinned `cost_model_version`으로 계산한다. 각 invocation의 input/cached-input/output/reasoning token을 versioned price table의 normalized token-cost unit(NTCU)로 환산한다. terminal status와 무관하게 **실제로 vendor를 호출했고 token consumption이 있으면** done/partial/timeout/failed 모두 cost에 포함한다. invoked candidate/baseline arm 중 하나라도 token telemetry가 없으면 해당 pair와 전체 cost gate를 `cost_locked`로 만든다; unavailable token을 0으로 대체하지 않는다. vendor가 호출되지 않은 setup/preflight failure는 `not_invoked_setup_failure`로 invocation cost 밖에만 별도 보고한다. 이는 quality에서 expected issue를 FN/invalid/locked로 처리하는 규칙을 완화하지 않는다. 같은 case·seed·schedule의 candidate/baseline cost를 paired aggregate하며 `cost_regression_ratio = sum(candidate_pair_NTCU) / sum(baseline_pair_NTCU)`이고 baseline total 0 또는 scoreable pair 0이면 locked다. gate는 ratio ≤ 1.10이다.

**Sprint-done acceptance**

- synthetic/public test corpus에서 collect → lint → blind run fixture → offline score가 결정적으로 동작한다.
- label leakage와 proprietary input 회귀 테스트가 있다.
- `evaluate_scope_dedupe_rollout()`의 모든 입력 지표를 scorer가 명시적 numerator/denominator와 함께 생성한다.

**Rollout-unlock acceptance — 스프린트 완료와 별도**

- adjudicated finding 100개 이상
- small/medium/large 모두 포함
- partial/timeout case 10개 이상
- adjudicated duplicate true pair 30개 이상
- 같은 위치/카테고리의 nonduplicate hard-negative pair 30개 이상
- predicted duplicate pair 30개 이상
- duplicate metric 분모가 0이면 통과가 아니라 locked
- 각 metric denominator와 Wilson lower bound가 report에 포함
- 독립 2인 판정 또는 documented resolver 완료
- 모든 precision/recall/CI/cost gate 통과

표본이 부족하면 스프린트 tooling은 완료할 수 있지만 rollout은 계속 잠긴다.

### Task 3.3: benchmark attestation binding

**Files**

- Modify: `server/config.py`
- Modify: `server/review/rollout.py`
- Modify: `server/review/finding_policy.py`
- Test: `tests/test_config.py`, `tests/test_review_benchmark.py`, `tests/test_pipeline.py`

**Design**

기존 `ALMIGHTY_REVIEW_POLICY_ENFORCEMENT_UNLOCKED=1`만으로는 충분하지 않다. enforce는 다음을 모두 요구한다.

1. explicit unlock env
2. local sanitized benchmark report 경로
3. `benchmark-report.schema.json` 검증 통과
4. canonical UTF-8 JSON bytes(키 정렬, compact separators, trailing newline 없음)의 configured SHA-256와 report hash 일치
5. report에 threshold schema version, 생성 시각, validity deadline, metric별 numerator/denominator/Wilson LB, gate reason이 있음
6. report에 clean implementation commit SHA, vendor/model/effort, prompt/protocol/chunker/adapter/CLI/event-schema hash가 있음
7. report에 canonical corpus-manifest hash, privacy-safe adjudication commitment hash, **primary-run selection hash와 paired seed/schedule hash**, scorer/schema hash가 있음
8. effective canary candidate와 report는 **모든** identity가 exact match해야 한다: clean commit, vendor/model/effort, prompt/protocol/chunker/adapter/CLI/event schema, corpus/adjudication commitment, primary-run selection/schedule, scorer/schema hashes. 동일 identity set은 run snapshot과 Operations UI에도 표시한다.
9. report `can_enforce=true`
10. 빈 failure reason
11. schema/version 호환

이 binding은 local operator가 report를 위조하는 것을 막는 서명 체계가 아니라, 어떤 benchmark 결과로 policy를 열었는지 재현하기 위한 tamper-evident traceability다.

**Acceptance**

- report 누락·hash mismatch·schema mismatch·expired validity·candidate identity mismatch·dirty implementation·`can_enforce=false`에서 effective mode는 항상 observe다.
- valid report여도 repo canary 선택과 kill switch는 계속 필요하다.
- report hash와 clean commit, vendor/model/effort, prompt/protocol/chunker/adapter/CLI/event schema, corpus/adjudication commitment, **primary selection/schedule**, scorer/schema identity의 동일 set은 run snapshot과 Operations UI에 표시된다.
- Slice 1C는 tooling만 완료한다. final paired run·sanitized attestation은 Slice 1D의 candidate code/UI/schema가 clean commit으로 고정된 뒤에만 생성한다.

**Current Slice 1C offline evidence (2026-07-24):** strict provenance/adjudication/prediction/run/report schemas, local immutable-bundle collect, physical-separation lint, label-blind paired offline fixture runner, exact issue/duplicate/scope/posting scorer, Wilson/sample/cost locks, private audit, canonical sanitized report, clean-commit/all-identity attestation binding을 구현했다. Prediction artifact filename+content commitments, full paired run commitment, production-equivalent path/claim/emission-order dedupe oracle, private ambiguity resolution, primary repetition, coverage evidence, duplicate-key JSON rejection을 포함한다. Python full gate는 838 collected, 1 skipped, 0 failed이며 compileall/diff check가 통과했다. Remote collector, live model benchmark, two-person adjudication, final paired report/attestation, rollout unlock은 모두 `not_run`/`locked`다.

---

## Milestone 4 — Canary metrics/query UI

### Task 4.1: bounded aggregate/query API (requested/effective PolicyDecision snapshot은 Slice 1A Task 0.4에서 완료)

**Files**

- Create: `server/repos/canary_repo.py`
- Create: `server/routes/operations.py`
- Modify: `server/api.py` 또는 app router registration 경계
- Modify: `server/config.py`, `server/http_security.py`, `server/main.py`
- Test: `tests/test_canary_metrics.py`, `tests/test_api.py`, `tests/test_config.py`, `tests/test_security.py`

**Dedicated ingress profile (Slice 1D)**

actual webhook delivery 전에 temp DB, background poller/worker/retention/notifications 0, webhook route만 exposed, request-body cap, management API unavailable 또는 authenticated를 강제하는 profile을 구현한다. trusted-proxy CIDR/direct-peer TLS와 public probe를 검증하지 못하면 actual delivery는 `not_run`이다.

**Endpoints**

```text
GET /api/operations/review-policy/summary?repo_id=&days=&cohort=&vendor=&status=&baseline_days=
GET /api/operations/review-policy/runs?repo_id=&days=&cohort=&vendor=&status=&cursor=&limit=
```

**Summary metrics**

- requested/effective scope/dedupe mode와 reason
- observe/enforce/unknown run 수
- comparison baseline은 별도 control cohort가 아니라 **같은 repo + 같은 2차원 policy cohort의 직전 `baseline_days` window**로 정의
- vendor done/partial/timeout/failed 비율
- telemetry ok/partial/unavailable coverage
- final vendor result 비율과 attempt 비율을 별도 denominator로 표시
- attempt 1은 initial review, 같은 `phase=review`의 attempt >1은 retry, `phase=verify`는 verify로 분류
- token/tool/duration 합계와 분포
- owned/reassigned/would-reject/rejected 수
- posting eligible/suppressed 수
- exact duplicate group/원본 수(운영 관측치이며 truth/precision으로 간주하지 않음)
- human adjudication coverage
- would-reject 중 approved/edited/dismissed 비율
- live duplicate precision은 계산하지 않고 benchmark report의 duplicate precision/denominator만 표시
- benchmark report hash, clean commit, vendor/model/effort, prompt/protocol/chunker/adapter/CLI/event schema, corpus/adjudication commitment, primary-run selection/schedule, scorer/schema identity, sample size, gate reasons

**Bounds/privacy**

- `days`, `baseline_days`, `limit` 상한과 summary `max_runs` hard cap
- response에 `truncated`, `as_of`, `sampled_through`, denominator를 표시
- opaque cursor는 version, immutable `as_of`, normalized filter hash, bucket을 포함한다. non-NULL row는 `(started_at DESC, id DESC)`의 **exclusive continuation**이며 `as_of` 이후 생성 row는 제외한다. legacy `started_at IS NULL` row는 non-NULL bucket 뒤 id DESC의 explicit null bucket으로 정렬한다. invalid/mismatched filter/as_of cursor는 400이다.
- cursor pagination과 query-plan/index 테스트
- 최근 bounded run만 parsing
- 원문 transcript, context text, prompt, claim, rationale, file path, command/event body 제외
- pending finding은 precision denominator에서 제외하고 coverage로 별도 표시

**Acceptance**

- summary와 run query가 동일한 repo/date/cohort/vendor/status filter 계약을 사용하고 합계가 일치한다.
- baseline window와 current window의 최소 denominator가 응답에 표시된다. 첫 canary window 또는 baseline denominator 미달은 비교 경고 대신 `insufficient_baseline` 상태와 부족한 run 수를 반환한다.
- operations endpoint는 기존 management bearer auth를 통과해야 하며 token 설정 시 unauthenticated 요청이 거부되는 테스트가 있다.
- 외부 proxy/tunnel 운영은 `ALMIGHTY_EXTERNAL_MODE=1` 같은 명시적 배포 gate 아래 admin token(최소 32자), HTTPS termination, allowed origin이 없으면 startup/preflight가 실패한다. HTTPS assertion은 direct peer TLS 또는 configured trusted-proxy CIDR에서 온 forwarded proto만 수용하고 arbitrary `X-Forwarded-Proto`는 신뢰하지 않으며 public probe로 검증한다.
- legacy unknown cohort가 별도 표시된다.
- empty/partial telemetry에서도 API가 degrade한다.
- raw/sensitive field가 response schema에 없다.

### Task 4.2: read-only 운영 화면

**Files**

- Create: `web/src/sections/OperationsSection.tsx`
- Create: `web/src/sections/OperationsSection.test.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/api.ts`

**UI**

- nav: `운영` / route: `/operations`
- repo/date/cohort/vendor/status filters
- current requested/effective mode, reason, unlock/attestation, canary membership, kill switch, restart 필요 여부
- run coverage, scope outcome, duplicate outcome, partial/timeout, telemetry coverage, tokens/tools/duration cards
- benchmark sample/gate reasons와 마지막 판정 시각
- rollback 경고:
  - 승인된 finding이 would-reject/suppress됨
  - benchmark duplicate precision 100% 미만 또는 denominator 미달
  - benchmark issue precision/recall/CI 미달
  - current window partial/timeout이 같은 repo/cohort의 직전 baseline window 대비 5%p 초과 또는 2배 초과(각 window 최소 20 runs)
  - telemetry `ok` coverage가 95% 미만 또는 같은 repo/cohort baseline 대비 5%p 초과 하락(각 window 최소 20 runs)
  - cost regression > 1.10
  - would-reject 중 approved/edited 1건 이상
- enforce를 바꾸는 버튼은 제공하지 않는다.

**Acceptance**

- filter 상태와 API query가 일치한다.
- loading/empty/error/unknown cohort 접근성 상태가 테스트된다.
- UI는 benchmark gate가 잠겨 있으면 명확히 `observe 유지`를 표시한다.
- web tests와 production build가 통과한다.

---

## Milestone 5 — 스프린트 종료 검증과 인계

### Task 5.1: offline full gate

```bash
.venv/bin/pytest -q
cd web && npm test -- --run && npm run build
cd .. && git diff --check
.venv/bin/python scripts/review-pipeline-benchmark.py \
  --output /tmp/review-pipeline-benchmark.json
```

### Task 5.2: 독립 완료 체크리스트

**A. Code/tooling slice done**

- 해당 delivery slice offline gates와 review 통과
- live 항목은 `passed/failed/not_run`으로 사실대로 기록
- `not_run`이어도 tooling slice는 완료할 수 있으나 live readiness를 주장하지 않음

**B. Live rehearsal passed**

- Codex/Claude 중 운영할 vendor telemetry pass 또는 명시적 legacy 제한
- sandbox no-write review와 partial retry pass
- post/webhook은 실제 사용 범위에 해당하는 단계가 pass
- unexpected remote mutation 0

**C. Rollout unlock approved**

- valid benchmark attestation과 모든 denominator/gate pass
- Operations UI/API pass
- sandbox live rehearsal pass
- kill switch 설정 → controlled restart → effective observe 확인을 15분 이내 수행
- 사용자/operator가 exact repo canary와 observation window를 별도 승인

A, B, C는 서로 대체하지 않는다.

### Task 5.3: live evidence matrix

실행 여부와 결과를 다음처럼 분리한다.

| Evidence | 상태 값 |
|---|---|
| Codex telemetry success | passed / failed / not_run |
| Claude telemetry success | passed / failed / not_run |
| Sandbox review | passed / failed / not_run |
| Partial retry | passed / failed / not_run |
| GitHub post idempotency | passed / failed / not_run |
| Signed webhook replay | passed / failed / not_run |
| Actual webhook delivery | passed / failed / not_run |
| Benchmark tooling | passed / failed |
| Rollout sample gate | passed / locked |
| Canary operations UI | passed / failed |

`not_run`을 통과로 간주하지 않는다.

### Task 5.4: final independent review

- correctness/regression/data migration/API contract reviewer
- security/privacy/live-side-effect/rollout reviewer
- UI/operability/metric-semantics reviewer

모든 blocker/high finding을 disposition하고 affected gates를 재실행한다. commit/push/release는 별도 사용자 승인 후에만 수행한다.

**Final offline evidence (2026-07-24):** implementation candidate `3423c009ad58fbe3282583ef5e85e67afe0b6687`에서 Python 858 collected(1 skipped, 0 failed), web 117 tests, production build, `compileall`, `git diff --check`, synthetic benchmark smoke가 통과했다. Smoke는 external model을 호출하지 않았고 label을 노출하지 않았으며 finding 3개로 `can_enforce=false`와 insufficient-sample/quality/coverage/cost reasons를 반환했다. correctness/data/API, security/privacy/side-effect, UI/operability의 final 3-way review가 발견한 benchmark Blocker 2건과 pagination/operations High 4건을 모두 수정했고, fresh targeted follow-up은 Blocker/High 없음으로 판정했다. live vendor, sandbox review/retry/post, public webhook delivery, paired benchmark와 two-person adjudication은 실행하지 않았으므로 각각 `not_run`; rollout sample gate는 `locked`, effective policy는 `observe`다. 상세 matrix는 `docs/review-pipeline-rollout.md`를 따른다.

## 6. 전체 실행 순서

1. **Slice 1A:** Milestone 0.1–0.3 → Task 0.4 PolicyDecision/run snapshot/retry basis → M1.1 bounded telemetry tooling 순으로 구현한다.
2. Task 0.5의 Slice 1A integrated offline gate와 fresh review를 끝내고, **별도 사용자 승인** 후에만 Slice 1A commit series를 만든다.
3. **사용자 확인** — clean VM/dedicated account 조건, paid/authenticated M1.2 telemetry probes를 승인한다. 승인 전에는 M1.2 live attestation을 실행하지 않는다.
4. M1.2 live evidence를 `passed/failed/not_run`으로 기록한다. Slice 1A tooling 완료와 분리한다.
5. **Slice 1B:** M2.1–2.2 직전 no-write external-effect approval을 받고 sandbox no-write review/retry를 실행한다.
6. **사용자 확인** — GitHub post mutation manifest를 승인한다. 승인 뒤 M2.3 post idempotency와 side-effect-free signed webhook replay만 실행한다.
7. **Slice 1C:** Milestone 3 schema/local approved bundle/lint/blind runner/scorer tooling을 구현한다. remote downloader는 별도 승인된 deferred work다.
8. **Slice 1D:** Operations aggregate/API/UI와 dedicated ingress profile을 구현·offline 검증하고 final candidate를 clean commit으로 고정한다.
9. final candidate에 대해 Milestone 3 paired benchmark run·sanitized attestation binding을 생성하고, 그 뒤 Milestone 5 final gates/review/handoff를 실행한다.
10. **사용자 확인** — Slice 1D 뒤 actual sandbox push/webhook delivery를 dedicated ingress profile + stopped consumer에서 검증한다. exact job worker execution은 다시 별도 승인한다.

한 writer가 순차 구현하며 live 실행과 code mutation을 동시에 진행하지 않는다. final attestation은 Slice 1C tooling 완료와 다르며 Slice 1D clean candidate 전에 생성하지 않는다.

## 7. Stop conditions

다음 중 하나라도 발생하면 enforce/canary 확대를 중단하고 observe로 유지한다.

- benchmark `can_enforce=false` 또는 gate reason/denominator 미달 존재
- benchmark report 누락/hash mismatch/schema mismatch, vendor/corpus/adjudication-commitment/primary-selection/schedule/scorer identity mismatch
- 승인/수정된 finding이 would-reject 또는 suppress된 사례가 1건이라도 원인 분석 없이 존재
- benchmark duplicate precision 100% 미만
- benchmark issue precision/recall/95% CI gate 위반
- 14일 current window와 같은 repo/cohort의 직전 14일 baseline window가 각 20 runs 이상일 때 partial/timeout이 5%p 초과 또는 2배 초과
- telemetry `ok` coverage가 95% 미만 또는 같은 baseline 대비 5%p 초과 하락
- all-failed 1건, CLI schema mismatch, cleanup 잔존은 즉시 hard stop
- cost regression ratio > 1.10
- runtime credential 또는 snapshot cleanup 잔존
- synthetic sandbox 밖의 path/content가 provider output·report·API에 노출
- admin token/TLS/origin 제한 없는 외부 접근
- 예상하지 않은 GitHub/Slack write
- kill switch 후 effective observe와 UI reason을 확인할 수 없음

## 8. 잔여 위험

- strong filesystem read containment는 이 스프린트에서 해결되지 않는다. synthetic sandbox 외 live review는 계속 금지한다.
- Claude 성공 telemetry는 계정/모델 접근 상태와 real tool call에 의존하며 코드만으로 통과시킬 수 없다.
- 두 명의 독립 adjudicator를 확보하지 못하면 tooling 완료와 무관하게 rollout unlock은 계속 잠긴다.
- GitHub webhook actual delivery는 Slice 1D dedicated ingress profile, 외부 설정·터널, stopped consumer 조건에 의존한다. signed replay만 통과한 경우 live delivery 완료로 주장하지 않는다.
- current diff의 commit 분할은 shared schema/API/pipeline hunk 의존성 때문에 예상 slice와 달라질 수 있다. 중간 상태의 buildability와 rollback 가능성을 분할 개수보다 우선한다.

## 9. Sprint 완료 기준

- 현재 변경 inventory와 blocker disposition 완료
- Claude unverified structured mode가 잠기고 vendor별 auth/cleanup 계약이 테스트됨
- telemetry preflight가 bounded/content-safe함
- sandbox no-write review/retry가 성공 강제 assertion으로 동작함
- post와 signed webhook replay는 explicit gate/evidence 상태를 가지며 actual delivery는 Slice 1D 이후 dedicated ingress profile에서만 별도 판정됨
- benchmark provenance/adjudication/blind-run/scorer tooling이 모든 rollout metric을 생성함
- valid benchmark attestation과 그 clean commit, vendor/model/effort, prompt/protocol/chunker/adapter/CLI/event schema, corpus/adjudication/primary schedule/scorer/schema identity exact match 없이는 enforce가 기술적으로 잠김
- 과거 run의 requested/effective policy/cohort와 retry 적용 정책이 실행 시점 snapshot으로 재현됨
- bounded canary API와 read-only operations UI가 구현됨
- offline full gate 통과
- code/tooling done, live rehearsal, rollout unlock 체크리스트가 독립적으로 판정됨
- live 미실행/실패 항목과 rollout lock이 숨김없이 인계됨
