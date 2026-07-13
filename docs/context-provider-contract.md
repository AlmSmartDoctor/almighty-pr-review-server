# Context Provider Contract — v2 서브프로젝트 B

**Purpose (한 줄):** v2 서브프로젝트 B의 각 컨텍스트 소스별 헤드리스 접근 계약 · 비밀 처리 규약의 단일 진실원.

---

## Jira (첫 외부 소스, B8에서 구현)

- 헤드리스 접근 = 전용 **Jira Cloud API 토큰**(email + API token, HTTP Basic 인증).
- 엔드포인트: `GET {base}/rest/api/3/issue/{ISSUE-KEY}?fields=summary,description,<AC필드>`. 수용기준 필드는 인스턴스별 `ALMIGHTY_JIRA_ACCEPTANCE_CRITERIA_FIELD=customfield_<숫자>`로 선택하며 미설정 시 생략한다.
- 자격증명은 **env-only**: `ALMIGHTY_JIRA_BASE_URL`, `ALMIGHTY_JIRA_EMAIL`, `ALMIGHTY_JIRA_API_TOKEN`. **base_url·토큰을 DB에 저장하지 않는다**(SSRF 표면을 env로 한정).
- base URL은 `https://<org>.atlassian.net`만 허용한다. 레포별 `jira_project_keys`는 유효한 non-empty allowlist가 필수이며, Jira 서비스 계정도 같은 프로젝트에만 최소 권한을 부여한다. 빈 allowlist에서는 프로바이더를 등록하지 않고 HTTP 호출을 하지 않는다.
- **금지: atlassian MCP OAuth 재사용.** 이유: 이 서버의 벤더 하네스는 mcpOAuth(atlassian/datadog/github)를 제외한다(참조: `docs/vendor-cli-contract.md`). Jira 접근은 반드시 전용 API 토큰을 쓴다.
- 이슈키 추출은 PR의 head_ref → title → body 순으로 정규식 `[A-Z][A-Z0-9]+-\d+` (base_ref는 파싱하지 않는다).

## 사내 DB 스키마 (B9)

- 인터페이스: `DBSchemaProvider`는 `schema_source(req) -> str`를 주입받는다 — 변경 파일(`req.changed_files`)로부터 관련 테이블의 DDL을 돌려주는 함수다. 소스 미주입 시 `status="skipped"`; 주입된 소스가 실패/도달 불가하면 `status="empty"`, `text=""`로 degrade한다.
- **소스 #1 — 정적 스키마 파일(구현됨).** 레포별 비밀-아님 컬럼 `db_schema_path`가 레포 안에 체크인된 DDL 덤프(예: `db/structure.sql`, pg_dump/mysqldump/수기 `CREATE TABLE …;`)를 가리킨다. 경로는 `static_context_path`와 동일하게 **레포 root 하위로 realpath 봉쇄**(임의 절대경로 exfil 차단, B-INV-9). 미설정이면 소스 미주입=skipped.
- **"변경 파일 경로 → 관련 테이블" 매핑 규칙(확정):** 스키마 파일을 `CREATE TABLE` 문 단위로 파싱해 `{테이블명 → 전체 DDL}` 순서맵을 만든다. 변경 파일 경로를 소문자화 후 비영숫자로 토큰화하고 각 토큰을 trailing-`s` 단수화한다(`users`↔`user` 대칭 매칭). 테이블명(단수화)이 어떤 변경 파일의 토큰 집합에 whole-token으로 포함되면 그 테이블을 "관련"으로 본다. 관련 테이블의 DDL을 스키마 파일 순서로 이어붙이되 최대 `_MAX_TABLES`개까지(초과분 절단), 총량은 downstream per-source 캡(`MAX_CONTEXT_CHARS_PER_SOURCE`)이 처리한다. `changed_files`가 비면(신호 없음) 전체 스키마를 덤프하지 않고 `""`로 degrade한다.
- **소스 #2 — 라이브 read-only introspection(유예).** `~/.claude/db-connections.yml`의 인라인 자격은 (a) 다수 커넥션이 SSM 터널 게이트 뒤, (b) 프로덕션 RDS 혼재, (c) provisioning 미확정 → 유예. 접근 시엔 db-inspector 계열 **read-only** 경로만, **프로덕션 RDS raw 접속 금지**, 터널 미가동 시 `""` degrade. 동일한 `schema_source` seam에 드롭인한다.

## Graphify — 프로젝트 컨텍스트 애그리게이터 (B9+)

리뷰 대상 레포와 관련된 정보(레포 개요 → DB 특징 → 전체 프로젝트 진행상황)를 **점진적으로** 담는 애그리게이터. `graph_source(req)->str` seam을 주입받아 렌더한다(소스 미주입=skipped, 실패=empty, NEVER raises).

- **소스 #1 — 정적 프로젝트 문서(구현됨).** 레포별 비밀-아님 컬럼 `graphify_path`가 레포 안에 체크인된 프로젝트 문서(예: `docs/PROJECT.md` — 진행상황·아키텍처·도메인 개요)를 가리킨다. 경로는 `static_context_path`/`db_schema_path`와 동일하게 **레포 root 하위로 realpath 봉쇄**(임의 절대경로 exfil 차단, B-INV-9). 변경 파일과 무관하게 **문서 전체를 항상 주입**한다(DBSchema의 변경파일→테이블 필터링과 다름). 미설정이면 소스 미주입=skipped. per-source 캡은 downstream(`render_context`)이 처리한다.
- **향후 증분(같은 seam에 스택):** 관련 DB 데이터 특징, 서버 보유 데이터(이 레포의 리뷰 이력·열린 finding·최근 PR) 요약 등을 같은 `graph_source` seam에 순차 추가한다.

## 비밀 처리 규약 (전 소스 공통 — 보안 불변식)

- 프로바이더 자격증명은 오직 `server/config.py`/env에만 존재한다. sqlite에 절대 넣지 않는다.
- DB 컬럼에는 **비밀이 아닌 참조/키만** 저장한다(예: jira_project_keys, static_context_path). 이유: 대시보드 API가 설정/레포를 `SELECT *`로 노출하므로 **모든 신규 DB 컬럼은 대시보드 공개로 간주**한다.
- 컨텍스트 수집은 부모 서버 프로세스에서, 격리 워커/worktree 진입 이전에만 실행한다. 격리 워커 env allowlist(`AUTH_ENV_KEYS`)에 프로바이더 secret을 절대 추가하지 않는다.
