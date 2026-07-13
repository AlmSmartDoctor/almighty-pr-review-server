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

## 외부 컨텍스트 / Jira 연동
Jira 이슈를 리뷰 프롬프트에 주입하려면 아래 3개 env를 설정한다(sqlite에는 절대 저장하지 않음, env-only):
```bash
ALMIGHTY_JIRA_BASE_URL=https://<org>.atlassian.net
ALMIGHTY_JIRA_EMAIL=<jira-account-email>
ALMIGHTY_JIRA_API_TOKEN=<dedicated Jira Cloud API token>
```
- 이 토큰은 HTTP Basic 인증용 **전용 Jira Cloud API 토큰**이다. Atlassian MCP의 OAuth 토큰과는 다르며 서로 대체할 수 없다.
- 세 값이 모두 설정되어 있고, 전역/레포별 `context_jira_on` 토글이 켜져 있어야 프로바이더가 등록된다. 하나라도 없으면 조용히 비활성화된다.
- 레포별 `jira_project_keys`(콤마/공백 구분, 예: `PROJ,ABC`)로 조회 대상 프로젝트를 제한할 수 있다.

opt-in 실제 왕복 테스트(네트워크 필요):
```bash
ALMIGHTY_JIRA_E2E=1 ALMIGHTY_JIRA_E2E_KEY=PROJ-123 \
  ALMIGHTY_JIRA_BASE_URL=... ALMIGHTY_JIRA_EMAIL=... ALMIGHTY_JIRA_API_TOKEN=... \
  pytest tests/test_jira.py -v
```
