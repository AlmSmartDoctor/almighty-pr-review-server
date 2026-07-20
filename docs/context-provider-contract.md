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

## 레포 참조 문서

- `context_static_on`이 켜지면 PR 변경 파일마다 해당 디렉터리에서 레포 루트까지 올라가며 `AGENTS.md`, `CLAUDE.md`, `.claude/CLAUDE.md`를 탐색한다. 루트 문서는 변경 파일 신호가 없어도 탐색한다.
- 여러 변경 파일이 같은 문서를 참조하면 한 번만 포함하고, 블록에 적용되는 변경 파일 경로를 표시한다. 렌더 순서는 루트에서 하위 디렉터리 순이며, 예산 선택 시 기존 `static_context_path` 고정 문서와 변경 파일에 가까운 문서를 우선한다.
- `static_context_path`는 자동 탐색을 대체하지 않는 선택적 고정 문서다. 설정하면 변경 경로와 관계없이 자동 탐색 결과와 함께 포함한다.
- 파일당 3,000자, static 소스 전체는 `MAX_CONTEXT_CHARS_PER_SOURCE`로 제한한다. 모든 경로와 심볼릭 링크는 PR-head worktree의 레포 root 하위로 realpath 봉쇄한다(B-INV-9).

## 사내 DB 스키마 (B9)

- 인터페이스: `DBSchemaProvider`는 `schema_source(req) -> str`를 주입받는다 — 변경 파일(`req.changed_files`)로부터 관련 테이블의 DDL을 돌려주는 함수다. 소스 미주입 시 `status="skipped"`; 주입된 소스가 실패/도달 불가하면 `status="empty"`, `text=""`로 degrade한다.
- **소스 #1 — 정적 스키마 파일(구현됨).** 레포별 비밀-아님 컬럼 `db_schema_path`가 레포 안에 체크인된 DDL 덤프(예: `db/structure.sql`, pg_dump/mysqldump/수기 `CREATE TABLE …;`)를 가리킨다. 경로는 `static_context_path`와 동일하게 **레포 root 하위로 realpath 봉쇄**(임의 절대경로 exfil 차단, B-INV-9). 미설정이면 소스 미주입=skipped.
- **"변경 파일 경로 → 관련 테이블" 매핑 규칙(확정):** 스키마 파일을 `CREATE TABLE` 문 단위로 파싱해 `{테이블명 → 전체 DDL}` 순서맵을 만든다. 변경 파일 경로를 소문자화 후 비영숫자로 토큰화하고 각 토큰을 trailing-`s` 단수화한다(`users`↔`user` 대칭 매칭). 테이블명(단수화)이 어떤 변경 파일의 토큰 집합에 whole-token으로 포함되면 그 테이블을 "관련"으로 본다. 관련 테이블의 DDL을 스키마 파일 순서로 이어붙이되 최대 `_MAX_TABLES`개까지(초과분 절단), 총량은 downstream per-source 캡(`MAX_CONTEXT_CHARS_PER_SOURCE`)이 처리한다. `changed_files`가 비면(신호 없음) 전체 스키마를 덤프하지 않고 `""`로 degrade한다.
- **소스 #2 — 라이브 read-only introspection(유예).** `~/.claude/db-connections.yml`의 인라인 자격은 (a) 다수 커넥션이 SSM 터널 게이트 뒤, (b) 프로덕션 RDS 혼재, (c) provisioning 미확정 → 유예. 접근 시엔 db-inspector 계열 **read-only** 경로만, **프로덕션 RDS raw 접속 금지**, 터널 미가동 시 `""` degrade. 동일한 `schema_source` seam에 드롭인한다.

## Graphify — 프로젝트 컨텍스트 애그리게이터 (B9+)

리뷰 대상 레포와 관련된 정보(레포 개요 → DB 특징 → 전체 프로젝트 진행상황)를 **점진적으로** 담는 애그리게이터. `graph_source(req)->str` seam을 주입받아 렌더한다(소스 미주입=skipped, 실패=empty, NEVER raises).

- **소스 #1 — 정적 프로젝트 문서(구현됨).** 레포별 비밀-아님 컬럼 `graphify_path`가 레포 안에 체크인된 프로젝트 문서(예: `docs/PROJECT.md` — 진행상황·아키텍처·도메인 개요)를 가리킨다. 경로는 `static_context_path`/`db_schema_path`와 동일하게 **레포 root 하위로 realpath 봉쇄**(임의 절대경로 exfil 차단, B-INV-9). 변경 파일과 무관하게 **문서 전체를 항상 주입**한다(DBSchema의 변경파일→테이블 필터링과 다름). 미설정이면 소스 미주입=skipped. per-source 캡은 downstream(`render_context`)이 처리한다.
- **소스 #2 — 서버 보유 오픈 finding 요약(구현됨, `server_data_source.open_findings_source`).** 앱 DB에서 **다른 열린 PR의 미결(`pending`) 지적**을 레포 스코프로 읽어 카테고리별 건수 + 대표 예시로 요약 주입한다(중복 제기 방지·일관성 참고용). `done` 런의 미결만 보되 `(PR, file, line, claim)`로 중복 제거 — 전체 재리뷰가 같은 지적을 여러 런에 재발행해도 1건으로 합치고, 증분 리뷰가 델타만 훑어 이전 런의 미결을 다시 싣지 않아도 그 미결을 보존한다("최신 done 런만" 필터는 후자를 누락시켜 미사용). 현재 리뷰 중인 PR은 자기-에코 방지로 제외, `state='open'`만, 레포명 `COLLATE NOCASE`. read-only SELECT + short-lived 커넥션, 없으면 `""`.
- **~~소스 #3 — 최근 오픈 PR 목록~~ / ~~소스 #4 — 리뷰 활동 현황~~ (제거됨).** 오픈 PR 목록·리뷰 실행 활동 통계는 결함 탐지 신호가 아니라 프롬프트를 희석하므로 LLM 경로에서 제거했다(코드·유닛 테스트 모두 삭제). 사람용 `/learn` 탭에도 노출되지 않는다.
- **경로 설정 불필요(소스 #2는 항상 활성):** `context_graphify_on`이 켜지면 문서(있으면, 소스 #1) + 소스 #2를 `_compose_sources`(소스별 예외 격리 후 비어있지 않은 출력만 join)로 애그리게이트한다. 전부 비면 provider `empty`, NEVER raises(B-INV-4).
- **향후 증분(같은 seam에 스택):** 관련 DB 데이터 특징(실접속 시 실데이터 특성) 등을 같은 `graph_source` seam에 순차 추가한다.

## 자가 학습 — 팀 피드백 (서브프로젝트 C)

이 레포의 과거 리뷰에서 **사람이 finding에 내린 판단**(승인/기각/수정)을 요약해 이후 리뷰에 "팀이 이런 지적을 이렇게 판단해 왔다"는 보정 신호로 주입하고(1차), 사람이 열람하는 `/learn` 탭(2차)과 결정 이력 감사(3차)로 확장한다. 상세는 `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md`.

- **학습 코퍼스는 이미 DB에 있다.** 사람 결정은 `finding.status`(`approved|dismissed|edited|posted`) + `finding.edited_text`로 durable하게 저장되므로 별도 이벤트 저장소를 만들지 않고 finding 테이블을 **읽어서** 요약한다. `server/seams.py`의 `NullMemoryStore`(write-side 스텁)는 finding.status로 포착 안 되는 신호(Slack 반응 등)를 위한 후속 증분까지 배선하지 않는다.
- **2차 — `/learn` 웹 탭(구현됨).** `feedback_source.feedback_stats`(순수, 최소 결정 게이트 없음) + `repo_feedback_stats`가 레포별 수용/수정/기각 통계를 만들고 `GET /api/learn`이 노출한다(결정 있는 레포만, 결정 많은 순). 프런트 `LearnSection`이 레포 탭 + 카테고리 표 + 대표 예시로 렌더한다.
- **3차 — 결정 이력 감사(구현됨).** append-only `finding_decision`(id, finding_id, from_status, to_status, decided_at) 테이블에 `finding_repo.set_status`가 **상태가 실제로 바뀔 때만** 1행 append한다(무변경·미존재 finding은 스킵—FK 안전). `feedback_source.recent_decisions(conn, full_name)`가 이 이력을 조인해 레포별 **최근 사람 판단 이벤트**(`approved|dismissed|edited`만, `posted`·`pending` 제외, `fd.id DESC` 최근순, `LIMIT 10`)로 반환하고 `/api/learn`이 `recent_decisions`로 실어 `/learn` 탭 "최근 결정 활동" 타임라인에 표시한다. `decided_by`는 단일 계정 배포에서 가치가 낮아 미기록(team-mode 재개 시 추가).
- **인터페이스:** `FeedbackContextProvider`(`name="team_feedback"`)는 `feedback_source(req) -> str`를 주입받는다. 소스 미주입=`skipped`, 실패=`empty`, 요약 텍스트 있으면 `ok`. NEVER raises.
- **소스 #1 — 앱 DB 조회(구현됨).** `db_feedback_source(*, db_path=config.DB_PATH)`가 `finding → review_run → pull_request → repo`를 조인해 `repo.full_name = req.repo COLLATE NOCASE`로 **현재 레포 결정만** 집계한다(레포 간 격리). 판단 매핑: 기각=`dismissed`, 수정수용=`edited` 또는 `edited_text` 존재, 승인=그 외. `pending`(미결정) 제외. read-only SELECT + short-lived 커넥션(worker 진행 중에도 WAL 하 안전).
- **캡·플로어:** `_MAX_DECISIONS=400`(최근순 스캔), `_MIN_DECISIONS=3`(미만이면 미주입), `_MAX_EXAMPLES=5`(기각/수정 대표 예시, claim 중복 제거), `_MAX_CLAIM_CHARS=160`. per-source·총합 캡과 nonce 펜스는 downstream `render_context`가 처리.
- **비밀 표면 0.** 읽는 건 같은 레포의 비밀-아님 finding 컬럼(claim/rationale/edited_text)뿐이며 이미 대시보드 `/api/runs/{id}/findings`로 공개된 데이터다. 자기 레포 데이터가 자기 리뷰에 머무르므로 exfiltration이 아니다.
- **per-repo 경로 컬럼 없음.** 소스가 앱 DB를 읽으므로 토글(`context_feedback_on`)만으로 켜고 끈다. 결정이 없으면 소스가 `""`를 반환해 자동 미주입.

## 비밀 처리 규약 (전 소스 공통 — 보안 불변식)

- 프로바이더 자격증명은 오직 `server/config.py`/env에만 존재한다. sqlite에 절대 넣지 않는다.
- DB 컬럼에는 **비밀이 아닌 참조/키만** 저장한다(예: jira_project_keys, static_context_path). 이유: 대시보드 API가 설정/레포를 `SELECT *`로 노출하므로 **모든 신규 DB 컬럼은 대시보드 공개로 간주**한다.
- 컨텍스트 수집은 부모 서버 프로세스에서, 격리 워커/worktree 진입 이전에만 실행한다. 격리 워커 env allowlist(`AUTH_ENV_KEYS`)에 프로바이더 secret을 절대 추가하지 않는다.
