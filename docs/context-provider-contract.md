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
- **소스 #2 — 서버 보유 오픈 finding 요약(구현됨, `server_data_source.open_findings_source`).** 앱 DB에서 **다른 열린 PR의 미결(`pending`) 지적**을 레포 스코프로 읽어 카테고리별 건수 + 대표 예시로 요약 주입한다(중복 제기 방지·일관성 참고용). `done` 런의 미결만 보되 `(PR, file, line, claim)`로 중복 제거 — 전체 재리뷰가 같은 지적을 여러 런에 재발행해도 1건으로 합치고, 증분 리뷰가 델타만 훑어 이전 런의 미결을 다시 싣지 않아도 그 미결을 보존한다("최신 done 런만" 필터는 후자를 누락시켜 미사용). 현재 리뷰 중인 PR은 자기-에코 방지로 제외, `state='open'`만, 레포명 `COLLATE NOCASE`. read-only SELECT + short-lived 커넥션, 없으면 `""`.
- **소스 #3 — 최근 오픈 PR 목록(구현됨, `server_data_source.open_prs_source`).** 앱 DB에서 **같은 레포에 현재 열려 있는 다른 PR**(번호·제목·작성자)을 읽어 "동시 진행 작업" 목록으로 주입한다(리뷰어가 관련/충돌 가능 작업을 인지). 현재 PR 제외, `state='open'`만, 레포명 `COLLATE NOCASE`, `LIMIT 30`. finding 유무와 무관(소스 #2가 못 잡는, 아직 안 리뷰됐거나 전부 처리된 오픈 PR까지 포괄). read-only SELECT, 없으면 `""`.
- **소스 #4 — 리뷰 활동 현황(구현됨, `server_data_source.activity_source`).** 앱 DB에서 이 레포의 **리뷰 실행 활동**(완료 런 수·리뷰된 PR 수·마지막 리뷰 시각·실패 이력)만 단일 집계로 읽어 "프로젝트 진행 맥락"으로 주입한다. finding 내용·심각도 분포는 **의도적으로 배제** — 소스 #2(미결 지적 내용)·자가 학습(팀의 수용/기각 판단 캘리브레이션)과 겹치지 않고, 모델이 앵커링할 여지가 없는 순수 활동/헬스 신호만 담는다. 레포명 `COLLATE NOCASE`, 완료·실패 이력이 전무하면 `""`. read-only 단일 집계 SELECT.
- **경로 설정 불필요(소스 #2·#3·#4는 항상 활성):** `context_graphify_on`이 켜지면 문서(있으면, 소스 #1) + 소스 #2 + 소스 #3 + 소스 #4를 `_compose_sources`(소스별 예외 격리 후 비어있지 않은 출력만 join)로 애그리게이트한다. 전부 비면 provider `empty`, NEVER raises(B-INV-4).
- **향후 증분(같은 seam에 스택):** 관련 DB 데이터 특징(실접속 시 실데이터 특성) 등을 같은 `graph_source` seam에 순차 추가한다.

## 자가 학습 — 팀 피드백 (서브프로젝트 C 1차)

이 레포의 과거 리뷰에서 **사람이 finding에 내린 판단**(승인/기각/수정)을 요약해 이후 리뷰에 "팀이 이런 지적을 이렇게 판단해 왔다"는 보정 신호로 주입한다. 상세는 `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md`.

- **학습 코퍼스는 이미 DB에 있다.** 사람 결정은 `finding.status`(`approved|dismissed|edited|posted`) + `finding.edited_text`로 durable하게 저장되므로 별도 이벤트 저장소를 만들지 않고 finding 테이블을 **읽어서** 요약한다. `server/seams.py`의 `NullMemoryStore`(write-side 스텁)는 finding.status로 포착 안 되는 신호(Slack 반응 등)를 위한 후속 증분까지 배선하지 않는다.
- **인터페이스:** `FeedbackContextProvider`(`name="team_feedback"`)는 `feedback_source(req) -> str`를 주입받는다. 소스 미주입=`skipped`, 실패=`empty`, 요약 텍스트 있으면 `ok`. NEVER raises.
- **소스 #1 — 앱 DB 조회(구현됨).** `db_feedback_source(*, db_path=config.DB_PATH)`가 `finding → review_run → pull_request → repo`를 조인해 `repo.full_name = req.repo COLLATE NOCASE`로 **현재 레포 결정만** 집계한다(레포 간 격리). 판단 매핑: 기각=`dismissed`, 수정수용=`edited` 또는 `edited_text` 존재, 승인=그 외. `pending`(미결정) 제외. read-only SELECT + short-lived 커넥션(worker 진행 중에도 WAL 하 안전).
- **캡·플로어:** `_MAX_DECISIONS=400`(최근순 스캔), `_MIN_DECISIONS=3`(미만이면 미주입), `_MAX_EXAMPLES=5`(기각/수정 대표 예시, claim 중복 제거), `_MAX_CLAIM_CHARS=160`. per-source·총합 캡과 nonce 펜스는 downstream `render_context`가 처리.
- **비밀 표면 0.** 읽는 건 같은 레포의 비밀-아님 finding 컬럼(claim/rationale/edited_text)뿐이며 이미 대시보드 `/api/runs/{id}/findings`로 공개된 데이터다. 자기 레포 데이터가 자기 리뷰에 머무르므로 exfiltration이 아니다.
- **per-repo 경로 컬럼 없음.** 소스가 앱 DB를 읽으므로 토글(`context_feedback_on`)만으로 켜고 끈다. 결정이 없으면 소스가 `""`를 반환해 자동 미주입.

## 비밀 처리 규약 (전 소스 공통 — 보안 불변식)

- 프로바이더 자격증명은 오직 `server/config.py`/env에만 존재한다. sqlite에 절대 넣지 않는다.
- DB 컬럼에는 **비밀이 아닌 참조/키만** 저장한다(예: jira_project_keys, static_context_path). 이유: 대시보드 API가 설정/레포를 `SELECT *`로 노출하므로 **모든 신규 DB 컬럼은 대시보드 공개로 간주**한다.
- 컨텍스트 수집은 부모 서버 프로세스에서, 격리 워커/worktree 진입 이전에만 실행한다. 격리 워커 env allowlist(`AUTH_ENV_KEYS`)에 프로바이더 secret을 절대 추가하지 않는다.
