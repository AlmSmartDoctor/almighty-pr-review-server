# Context Provider Contract — v2 서브프로젝트 B

**Purpose (한 줄):** v2 서브프로젝트 B의 각 컨텍스트 소스별 헤드리스 접근 계약 · 비밀 처리 규약의 단일 진실원.

---

## Jira (첫 외부 소스, B8에서 구현)

- 헤드리스 접근 = 전용 **Jira Cloud API 토큰**(email + API token, HTTP Basic 인증).
- 엔드포인트: `GET {base}/rest/api/3/issue/{ISSUE-KEY}?fields=summary,description,<AC필드>` (AC 필드명은 인스턴스별 커스텀 필드 → B8에서 확정).
- 자격증명은 **env-only**: `ALMIGHTY_JIRA_BASE_URL`, `ALMIGHTY_JIRA_EMAIL`, `ALMIGHTY_JIRA_API_TOKEN`. **base_url·토큰을 DB에 저장하지 않는다**(SSRF 표면을 env로 한정).
- **금지: atlassian MCP OAuth 재사용.** 이유: 이 서버의 벤더 하네스는 mcpOAuth(atlassian/datadog/github)를 제외한다(참조: `docs/vendor-cli-contract.md`). Jira 접근은 반드시 전용 API 토큰을 쓴다.
- 이슈키 추출은 PR의 head_ref → title → body 순으로 정규식 `[A-Z][A-Z0-9]+-\d+` (base_ref는 파싱하지 않는다).

## 사내 DB 스키마 (B9, 유예)

- `~/.claude/db-connections.yml`에 인라인 자격이 존재하나 (a) 다수 커넥션이 SSM 터널 게이트 뒤에 있고 (b) 프로덕션 RDS가 섞여 있으며 (c) "변경된 diff → 관련 테이블" 선택 규칙이 미정 → **B9로 유예**.
- 접근 시엔 db-inspector 계열 **read-only** 경로만 사용하고 **프로덕션 RDS raw 접속 금지**. 터널 미가동 시 빈 컨텍스트로 degrade.
- 인터페이스: `DBSchemaProvider`는 `schema_source(req) -> str`를 주입받는다 — 변경 파일(`req.changed_files`)로부터 관련 테이블의 DDL을 돌려주는 함수다. "변경 파일 경로 → 관련 테이블" 매핑 규칙과 read-only 접근 경로(db-inspector 스타일, **프로덕션 RDS raw 접속 없음**)의 구체 구현은 read-only DB 접근이 provisioning될 때까지 유예한다. 소스 미주입 시 `status="skipped"`; 주입된 소스가 실패/도달 불가하면 `status="empty"`, `text=""`로 degrade한다.

## Graphify (B9, 스텁)

- 통합 대상 아티팩트/엔드포인트가 현재 전무 → **스텁만** 둔다(B9에서 항상 skipped 반환).

## 비밀 처리 규약 (전 소스 공통 — 보안 불변식)

- 프로바이더 자격증명은 오직 `server/config.py`/env에만 존재한다. sqlite에 절대 넣지 않는다.
- DB 컬럼에는 **비밀이 아닌 참조/키만** 저장한다(예: jira_project_keys, static_context_path). 이유: 대시보드 API가 설정/레포를 `SELECT *`로 노출하므로 **모든 신규 DB 컬럼은 대시보드 공개로 간주**한다.
- 컨텍스트 수집은 부모 서버 프로세스에서, 격리 워커/worktree 진입 이전에만 실행한다. 격리 워커 env allowlist(`AUTH_ENV_KEYS`)에 프로바이더 secret을 절대 추가하지 않는다.
