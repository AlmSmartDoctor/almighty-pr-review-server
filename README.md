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
- `gh` 로그인(`gh auth status`).
- Claude 또는 Codex 중 사용할 vendor CLI를 최소 하나 설치하고 로그인한다. 설치하지 않은 vendor는 레포 설정에서 끌 수 있다.
- 리뷰 대상 레포의 로컬 clone은 선택 사항이다. `local_path`를 비우면 서버가 서비스 전용 clone을 만들고 격리 worktree 소스로 사용한다.

설정 화면에서 레포를 등록하면 GitHub 접근, 로컬 Git 경로, 하네스, 활성 vendor CLI를 자동 검사한다. `리뷰 준비 완료`가 아니면 표시된 원인을 수정한 뒤 `준비 상태 다시 검사`를 실행한다. `GitHub PR 지금 동기화`로 폴링 주기를 기다리지 않고 Open PR을 가져올 수 있으며 마지막 성공 시각·Open PR 수·최근 오류도 카드에서 확인한다. 리뷰 상황판의 `GitHub PR 전체 동기화`는 모든 활성 레포를 한 번에 갱신한다. 레포 이름은 카드에서 수정할 수 있고, 진행 중인 리뷰나 Wiki 생성이 없을 때 등록 레포와 저장 데이터를 삭제할 수 있다.

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
특정 PR만 smoke하려면 작은 PR 기준으로 `ALMIGHTY_E2E_PR=2414`처럼 추가한다.

## Sandbox E2E rehearsal (offline tooling)

`scripts/sandbox-e2e.py` is a **fail-closed preflight only**. It does not make GitHub, vendor, Slack, webhook, or Gateway calls. It requires an operator-owned immutable JSON allowlist (`["owner/repo#PR"]` or `{"targets":[...]}`), an exact target/head/vendor/model manifest, a separate credential-attestation file plus its operator-approved canonical SHA-256, the actually injected credential whose fingerprint matches that attestation, and a new DB inside a mode-0700 `almighty-e2e-*` disposable workspace. Its output is sanitized evidence and reports `live: not_run`.

The script creates a new `0700` `GH_CONFIG_DIR`, strips ambient GitHub token variables, and accepts only the supplied credential; a future separately approved executor must use that strict environment. Review/retry reject write-capable credentials. `post-verify` requires explicit `allow_post`. A dedicated server process activates posting only with `ALMIGHTY_REHEARSAL_POST_ENABLED=1`, exact target/head and credential fingerprint settings; create/update/fallback/inline/Slack remain independently default-deny. A one-operation replay returns `{operation_id, replayed: true}`; a multi-vendor replay uses the documented plural `{operation_ids, replayed: true}` form without another mutation.

Offline coverage (no lifespan/background worker and no public listener) includes signed webhook handler replay into a temp DB, duplicate-delivery single queued/unconsumed job, and body-cap coverage. Actual webhook delivery, worker execution, sandbox review/retry, GitHub post, and all external calls remain **not_run** pending their separate approvals and clean-account environment.

```bash
.venv/bin/pytest -q tests/test_e2e_smoke.py tests/test_e2e_diagnostics.py tests/test_e2e_posting_policy.py
```

## 관리 API 보안

기본 개발 서버는 loopback 전용이다. 터널/리버스 프록시로 외부에 노출할 때는 관리 API 토큰을 반드시 설정한다.

```bash
ALMIGHTY_ADMIN_TOKEN=<충분히 긴 임의 토큰>
ALMIGHTY_ADMIN_ALLOWED_ORIGINS=https://review.example.com
```

토큰이 설정되면 웹 UI가 시작할 때 토큰을 요청하고 브라우저 `sessionStorage`에만 보관한다. `/api/health`와 GitHub/Slack 웹훅 경로는 관리 토큰에서 제외되지만 각 웹훅의 HMAC 검증은 계속 필수다. 가능하면 프록시의 공개 ingress에는 `/api/webhooks/github`와 `/api/webhooks/slack`만 열고 나머지 관리 API는 사내 접근으로 제한한다.

운영 관련 환경 변수:

```bash
ALMIGHTY_JOB_TIMEOUT_SEC=1800         # 리뷰 job 전체 wall-clock 상한
ALMIGHTY_WIKI_VENDOR_TIMEOUT_SEC=1800 # Ground Truth 벤더 호출 상한(30분)
ALMIGHTY_WORKER_IDLE_MAX_SEC=30       # idle claim backoff 최대값
ALMIGHTY_BACKGROUND_SHUTDOWN_GRACE_SEC=10
ALMIGHTY_BACKGROUND_CLEANUP_TIMEOUT_SEC=20
ALMIGHTY_WEBHOOK_MAX_BODY_BYTES=1048576
ALMIGHTY_CURSOR_HMAC_SECRET=<32자 이상 임의 시크릿> # 외부/다중 프로세스 환경 권장
ALMIGHTY_RETENTION_DAYS=0             # 0=비활성, 양수=오래 닫힌 PR 이력/raw 정리
ALMIGHTY_DIAGNOSTIC_CLEANUP_ENABLED=0 # 1일 때만 raw/context TTL cleanup 시작
ALMIGHTY_DIAGNOSTIC_RETENTION_DAYS=7
ALMIGHTY_CONTEXT_PAYLOAD_RETENTION_DAYS=7
```

리뷰 목록 cursor는 서버가 HMAC으로 서명한다. `ALMIGHTY_CURSOR_HMAC_SECRET`가 없으면 관리 토큰을 사용하고, 둘 다 없는 loopback 개발 환경에서는 프로세스별 임시 키를 사용한다. 임시 키를 쓰거나 시크릿을 교체한 뒤 서버를 재시작하면 기존 cursor는 400으로 무효화되며 UI에서 첫 페이지부터 새로고침하면 복구된다.

동시성 설정은 저장 즉시 UI에 반영되지만 실제 CLI pool과 worker lane에는 서버 재시작 후 적용된다. deep health는 background task 종료와 가장 오래된 queued job age를 함께 표시하고, `/api/telemetry`는 bounded 집계만 제공한다. Diagnostic cleanup은 raw 파일과 저장된 context payload를 비가역적으로 지우므로 기본 off이며, 대상·TTL·backup/restore 상태를 확인한 뒤에만 명시적으로 활성화한다.

## GitHub 웹훅 (푸시 트리거)

폴링(pull) 대신 GitHub `pull_request` 웹훅(push)으로도 리뷰를 트리거할 수 있다. 폴러와 동일한 게이트를 그대로 적용한다: 등록·`enabled`·`trigger_mode=auto` 레포에 한해, 벤더가 켜져 있고 head sha가 새로우면 리뷰 job을 enqueue한다(폴링과 중복돼도 `UNIQUE(pr_id, head_sha)`로 한 번만 실행). `opened`/`synchronize`/`reopened` action만 대상이다.

공유 시크릿은 **env-only**(sqlite 금지):
```bash
ALMIGHTY_GITHUB_WEBHOOK_SECRET=<GitHub 웹훅과 동일한 임의 시크릿>
```
GitHub 레포/조직 설정 → Webhooks → Add webhook:
- **Payload URL**: `https://<서버 공개주소>/api/webhooks/github` (외부 노출을 위해 리버스 프록시/터널 필요)
- **Content type**: `application/json`
- **Secret**: 위 `ALMIGHTY_GITHUB_WEBHOOK_SECRET`와 동일 값
- **Events**: "Let me select individual events" → **Pull requests**만 선택

검증은 `X-Hub-Signature-256`(HMAC-SHA256, raw body 기준) 상수시간 비교다. 시크릿과 필수 서명 헤더를 body보다 먼저 확인하며 Content-Length/chunked body 모두 기본 1 MiB 상한을 적용한다. 시크릿 미설정이면 수신 자체를 거부한다(503). 서명 불일치는 401, 대상 아님(다른 이벤트·action, 미등록/`manual` 레포)은 2xx로 무시한다.

## Slack 반응 학습 (서브프로젝트 C)

리뷰를 게시할 때(대시보드 "포스팅") 서버가 Slack 채널에도 요약을 올리고, 팀이 그 메시지에 남기는 👍/👎 반응을 학습 신호로 수집한다. `finding.status`(승인/기각/수정)로는 포착되지 않는, 대시보드를 쓰지 않는 팀원의 평가까지 반영해 다음 리뷰 프롬프트를 보정한다(부정 반응이 우세하면 지적 강도를 낮춘다). 반응 집계는 `/learn` 탭에도 노출된다.

라이브 연결은 **env-only**(sqlite 금지). 미설정이면 게시가 자동 비활성이고, 나머지 기능은 그대로 동작한다:
```bash
ALMIGHTY_SLACK_BOT_TOKEN=<xoxb-... chat:write 권한 봇 토큰>
ALMIGHTY_SLACK_SIGNING_SECRET=<Slack 앱 Signing Secret>
ALMIGHTY_SLACK_CHANNEL=<게시할 채널 ID 또는 #채널명>
```
Slack 앱 설정:
- **OAuth & Permissions** → Bot Token Scopes에 `chat:write` 추가, 워크스페이스에 설치 후 봇을 대상 채널에 초대.
- **Event Subscriptions** → Request URL을 `https://<서버 공개주소>/api/webhooks/slack`로(외부 노출을 위해 리버스 프록시/터널 필요). URL 검증 챌린지는 자동 응답한다.
- **Subscribe to bot events**에 `reaction_added`, `reaction_removed` 추가.

수신 검증은 `X-Slack-Signature`(`v0:{timestamp}:{body}` HMAC-SHA256, raw body 기준) 상수시간 비교다. Signing Secret 미설정이면 수신 거부(503), 서명 불일치는 401, 우리가 게시하지 않은 메시지·관심 밖 이모지는 2xx로 무시한다. 반응은 우리가 게시한 메시지에만 귀속되며(다른 채널 메시지는 무시), 레포별로 격리된다. 봇 토큰/서명 시크릿은 실패 로그에서도 제거(redact)된다. PR 봇 relay와 채널이 겹치면 알림이 중복될 수 있으니 반응 수집용 채널을 분리하는 걸 권장한다. 상세: `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md`.

## LLM Wiki — 레포 Ground Truth

`/wiki`는 특정 PR의 finding을 모으는 화면이 아니라, **해당 레포 자체의 Ground Truth**를 관리한다. 레포별 `Ground Truth 생성`을 실행하면 활성 Claude/Codex CLI가 격리된 read-only worktree에서 README·docs·코드·모델/엔티티·마이그레이션을 탐색하고, 설정된 `db_schema_path`의 DDL과 프로젝트 문서를 함께 분석한다.

결과는 도메인 지식·시스템 구조·데이터 모델·주요 흐름·비즈니스 불변식을 사실 단위로 저장하며, 모든 사실에 코드 파일/심볼·문서·DB 테이블/컬럼 근거가 필수다. 코드 근거는 snapshot 내부 파일과 실제 라인 범위 또는 주요 언어의 선언 심볼을 확인하고, 문서 근거는 snapshot 내부 파일 존재 여부를 확인하며, DB 근거는 설정된 정적 DDL에서 실제 `table.column`이 존재하는지 검증한다. 무효 근거를 제거한 뒤 근거가 남지 않는 사실은 저장하지 않는다. 근거가 부족한 내용은 추측하지 않고 `확정할 수 없는 항목`으로 분리한다. 생성 시점의 commit SHA와 분석 출처도 함께 보존한다. rate limit·timeout·일시적 연결 장애는 2분, 4분 간격으로 최대 3회 자동 재시도하며 서버 재시작 후에도 대기 상태를 이어간다. 최신 스냅샷은 `wiki_page`에 저장되고 재생성 실패 시 기존 Ground Truth를 유지한다.

기본 DB 분석 범위는 레포에 체크인된 마이그레이션/ORM 코드와 설정된 정적 DDL이다. Resolver-backed MSSQL Query Gateway가 있으면 설정 화면의 레포별 `Live DB` 대상 ID를 통해 테이블·컬럼 메타데이터만 추가할 수 있다. DB 주소·계정·비밀번호는 Almighty 서버나 벤더에 노출하지 않는다.

```bash
ALMIGHTY_MSSQL_GATEWAY_URL=https://gateway.internal
ALMIGHTY_MSSQL_GATEWAY_TOKEN=<32자 이상 bearer token>
ALMIGHTY_MSSQL_GATEWAY_TARGET_FIELD=hospitalId  # generic Gateway는 targetId
```

외부 로컬 스킬 경로에 의존하지 않는다. 검증된 Safe-DB SQL Gateway read 경로 중 필요한 SQL 분류·parameter/TOP 검증·동시성 잠금·Gateway v2 계약 검증·SQL hash 감사 로직만 `server/safe_db/sql_gateway.py`에 이식했다. Gateway는 별도로 read-only credential·SHOWPLAN·streaming cap·timeout/TDS cancellation을 강제한다. 서버는 고정된 `INFORMATION_SCHEMA` base-table 메타데이터 SELECT만 실행하며 HTTP 실패 시 명시적 cancel을 시도한다. 실패하면 라이브 소스만 비우고 리뷰와 Wiki는 계속 진행한다. 업무 데이터 row 조회와 자동 페이지네이션은 지원하지 않는다. 상세: `docs/superpowers/specs/2026-07-20-live-mssql-introspection.md`.

## 외부 컨텍스트 / Jira 연동

### Static 컨텍스트 (외부 의존 0)
레포 내 지정한 `.md` 파일(설계 노트·리뷰 규약 등)을 리뷰 프롬프트의 `## 외부 컨텍스트`에 주입한다. 설정 화면 → 리뷰 대상 레포 테이블 → **컨텍스트 오버라이드** 셀에서:
- `Static` 토글을 `켜짐`으로(또는 전역 `Static 컨텍스트` 기본값 상속)
- `경로` 입력에 파일 경로를 넣는다. 경로는 레포의 `local_path` 하위로 제한된다(base-dir allowlist, 임의 절대경로 차단). 토글만 켜고 경로가 비어 있으면 프로바이더는 등록되지 않는다.

### 자가 학습 (팀 피드백, 외부 의존 0)
이 레포의 과거 리뷰에서 **사람이 finding에 내린 판단**(승인/기각/수정)을 요약해 이후 리뷰 프롬프트에 "팀이 이런 지적을 이렇게 판단해 왔다"는 보정 신호로 주입한다. 학습 데이터는 이미 서버 DB의 `finding` 테이블(대시보드에서 승인/기각/수정한 결과)에 있으므로 별도 설정이나 외부 연동이 필요 없다. 설정 화면 → 외부 컨텍스트 → **자가 학습(팀 피드백)** 토글(또는 레포별 오버라이드 셀의 `피드백`)만 켜면 된다. 레포별로 격리되며(다른 레포 피드백은 섞이지 않음), 결정이 3건 미만이면 신뢰할 패턴이 아니라 주입하지 않는다.

`/learn`의 **승인형 리뷰 규칙**은 같은 카테고리에서 기각이 3건 이상이고 전체 판단의 2/3 이상일 때 규칙을 제안한다. 제안은 자동 적용되지 않으며 사람이 승인한 `active` 규칙만 해당 레포의 다음 리뷰에 주입된다. 적용 중인 규칙은 언제든 비활성화하거나 다시 활성화할 수 있다. Slack 반응까지 신호로 쓰려면 위 **Slack 반응 학습** 섹션을 참고한다. 상세: `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md`.

### Jira 연동
Jira 이슈를 리뷰 프롬프트에 주입하려면 필수 env 3개와 필요시 수용기준 필드를 설정한다(sqlite에는 절대 저장하지 않음, env-only). 레포별 `jira_project_keys`는 같은 오버라이드 셀의 `Jira키` 입력으로 설정한다:
```bash
ALMIGHTY_JIRA_BASE_URL=https://<org>.atlassian.net
ALMIGHTY_JIRA_EMAIL=<jira-account-email>
ALMIGHTY_JIRA_API_TOKEN=<dedicated Jira Cloud API token>
ALMIGHTY_JIRA_ACCEPTANCE_CRITERIA_FIELD=customfield_12345  # optional
```
- 이 토큰은 HTTP Basic 인증용 **전용 Jira Cloud API 토큰**이다. Atlassian MCP의 OAuth 토큰과는 다르며 서로 대체할 수 없다.
- base URL은 Jira Cloud의 `https://<org>.atlassian.net` 형식만 허용한다.
- 필수 세 값이 모두 설정되고, 전역/레포별 `context_jira_on` 토글이 켜져 있으며, 레포별 `jira_project_keys` allowlist가 있어야 프로바이더가 등록된다. 하나라도 없으면 비활성화된다.
- `jira_project_keys`는 콤마/공백 구분(예: `PROJ,ABC`)이며 필수다. PR 작성자가 다른 프로젝트 이슈를 서비스 계정 권한으로 조회하지 못하도록 Jira 계정 자체도 이 프로젝트들에만 최소 권한을 부여한다.
- 수용기준이 별도 Jira 커스텀 필드에 있으면 `ALMIGHTY_JIRA_ACCEPTANCE_CRITERIA_FIELD=customfield_<숫자>`를 설정한다. 미설정 시 summary와 description만 사용한다.

opt-in 실제 왕복 테스트(네트워크 필요):
```bash
ALMIGHTY_JIRA_E2E=1 ALMIGHTY_JIRA_E2E_KEY=PROJ-123 \
  ALMIGHTY_JIRA_BASE_URL=... ALMIGHTY_JIRA_EMAIL=... ALMIGHTY_JIRA_API_TOKEN=... \
  pytest tests/test_jira.py -v
```
