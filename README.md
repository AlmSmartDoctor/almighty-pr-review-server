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
특정 PR만 smoke하려면 작은 PR 기준으로 `ALMIGHTY_E2E_PR=2414`처럼 추가한다.

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

검증은 `X-Hub-Signature-256`(HMAC-SHA256, raw body 기준) 상수시간 비교다. 시크릿 미설정이면 수신 자체를 거부한다(503). 서명 불일치는 401, 대상 아님(다른 이벤트·action, 미등록/`manual` 레포)은 2xx로 무시한다.

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

현재 DB 분석 범위는 레포에 체크인된 마이그레이션/ORM 코드와 설정된 정적 DDL이다. 라이브 DB read-only introspection은 별도 안전 연결 계약이 확정된 뒤 추가한다.

## 외부 컨텍스트 / Jira 연동

### Static 컨텍스트 (외부 의존 0)
레포 내 지정한 `.md` 파일(설계 노트·리뷰 규약 등)을 리뷰 프롬프트의 `## 외부 컨텍스트`에 주입한다. 설정 화면 → 리뷰 대상 레포 테이블 → **컨텍스트 오버라이드** 셀에서:
- `Static` 토글을 `켜짐`으로(또는 전역 `Static 컨텍스트` 기본값 상속)
- `경로` 입력에 파일 경로를 넣는다. 경로는 레포의 `local_path` 하위로 제한된다(base-dir allowlist, 임의 절대경로 차단). 토글만 켜고 경로가 비어 있으면 프로바이더는 등록되지 않는다.

### 자가 학습 (팀 피드백, 외부 의존 0)
이 레포의 과거 리뷰에서 **사람이 finding에 내린 판단**(승인/기각/수정)을 요약해 이후 리뷰 프롬프트에 "팀이 이런 지적을 이렇게 판단해 왔다"는 보정 신호로 주입한다. 학습 데이터는 이미 서버 DB의 `finding` 테이블(대시보드에서 승인/기각/수정한 결과)에 있으므로 별도 설정이나 외부 연동이 필요 없다. 설정 화면 → 외부 컨텍스트 → **자가 학습(팀 피드백)** 토글(또는 레포별 오버라이드 셀의 `피드백`)만 켜면 된다. 레포별로 격리되며(다른 레포 피드백은 섞이지 않음), 결정이 3건 미만이면 신뢰할 패턴이 아니라 주입하지 않는다. Slack 반응까지 신호로 쓰려면 위 **Slack 반응 학습** 섹션을 참고한다. 상세: `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md`.

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
