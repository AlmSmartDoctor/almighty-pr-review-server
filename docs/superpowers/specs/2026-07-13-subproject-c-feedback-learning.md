# Subproject C — 자가 학습 (팀 피드백 학습) Spec

**작성일:** 2026-07-13
**상태:** 1차 증분 구현 (서버 데이터 우선, hermetic). Slack·외부 신호는 후속 증분.

## 목표 (한 줄)

리뷰 대상 레포에서 **사람이 과거 finding에 내린 판단**(승인/기각/수정)을 요약해 이후 리뷰 프롬프트에
"팀이 이런 지적을 어떻게 판단해 왔다"는 보정 신호로 주입한다. 리뷰어가 팀이 반복적으로 기각하는
유형(예: 스타일 nitpick)은 낮은 우선순위로, 반복적으로 수용하는 유형(예: correctness)은
집중하도록 스스로 조정한다.

## 핵심 설계 결정 — 학습 코퍼스는 이미 DB에 있다

사람의 결정은 이미 `finding` 테이블에 durable하게 저장된다:

- `finding.status`: `pending → approved | dismissed | edited | posted` (`dismissed` = 기각)
- `finding.edited_text`: 사람이 문구를 다듬어 수용한 경우의 최종 텍스트(그 외 NULL)

따라서 **별도의 학습 이벤트 저장소를 새로 만들면 같은 결정을 중복 기록**하게 된다. 1차 증분은
finding 테이블을 **읽어서** 요약한다(write-side 신설 없음). `server/seams.py`의 `NullMemoryStore`
(write-only 스텁)는 **finding.status로 포착되지 않는 신호**(예: Slack 👍/👎 반응, 채널 규약)를
소비할 후속 증분까지 그대로 둔다 — 지금 배선하지 않는다.

### 판단(verdict) 매핑

finding 한 건의 사람 판단을 다음으로 분류한다:

- **기각(rejected):** `status == 'dismissed'`
- **수정 수용(edited):** `status == 'edited'` 이거나 `edited_text`가 비어있지 않음
  (— `posted`로 덮여도 `edited_text`가 남아 편집 사실이 보존됨)
- **승인(approved):** 그 외(`approved`/`posted` 이며 `edited_text` 없음)
- `pending`(미결정)은 제외한다.

`verify_status`/`verify_rationale`는 AI 반박 패스 결과(기계 신호)이므로 **사람 판단으로 쓰지 않는다**.

### 레포 스코프

`finding`에는 `repo_id`가 없으므로 `finding → review_run → pull_request → repo`로 조인해
`repo.full_name = req.repo COLLATE NOCASE`로 **현재 레포의 결정만** 집계한다(레포 간 격리).

## 읽기 경로 — 기존 컨텍스트 seam 재사용

새 주입 경로를 만들지 않는다. B에서 확립한 ContextProvider seam에 그대로 얹는다.

- `server/context/feedback_provider.py` — `FeedbackContextProvider` (`name = "team_feedback"`).
  DBSchema/Graphify와 동일 계약: `feedback_source(req) -> str`를 주입받아 소스 미주입=`skipped`,
  실패=`empty`, 텍스트 있으면 `ok`. **절대 raise 하지 않는다.**
- `server/context/feedback_source.py` — `db_feedback_source(*, db_path=None)`가 finding을 조회하는
  `source(req)->str`를 만든다. 순수 함수 `summarize_feedback(rows)`가 렌더를 담당(DB 없이 테스트 가능).
- `server/context/registry.py` — `context_feedback_on` 토글이 켜지면 provider를 등록한다. per-repo
  경로 컬럼은 없다(소스가 앱 DB를 읽으므로). 결정이 없으면 소스가 `""`를 반환해 자동으로 미주입된다.
- 렌더는 `render_context`가 담당 → per/total 캡(B-INV-5) + nonce 펜스(B-INV-6) + 신뢰-경계 프리앰블이
  자동 적용된다. 리뷰 프롬프트의 `## 외부 컨텍스트` 블록 안에 `### team_feedback`로 들어간다.

### 캡·플로어 (feedback_source 내부)

- `_MAX_DECISIONS = 400` — 최근 결정만 스캔(비용 상한, `ORDER BY f.id DESC LIMIT`).
- `_MIN_DECISIONS = 3` — 이 미만이면 신뢰할 패턴이 아니므로 `""`(주입 안 함).
- `_MAX_EXAMPLES = 5` — 기각/수정 대표 예시 각 버킷 최대 개수(claim 중복 제거).
- `_MAX_CLAIM_CHARS = 160` — 예시 claim 1줄 절단.

## 토글 (triple-guard, 기존 패턴)

`context_feedback_on` (다른 컨텍스트 토글과 동일):

1. `server/db.py` — `app_settings`(전역 기본 `INTEGER NOT NULL DEFAULT 0`) + `repo`(per-repo override
   `INTEGER`, NULL=상속).
2. `server/repos/repo_repo.py` / `server/repos/settings_repo.py` — `ALLOWED`에 추가.
3. `server/api.py` — `RepoPatch`/`SettingsPatch` 필드 + `*_on` None-reset 루프에 추가(상속 복원).
4. `web/src/sections/SettingsSection.tsx` — 전역 스위치 + per-repo 오버라이드 셀(경로 입력 없음).

## 보안 (B-INV 준수)

- **새 secret 표면 0.** 읽는 것은 같은 레포의 비밀-아님 finding 컬럼(claim/rationale/edited_text)뿐이며,
  이는 이미 대시보드 `/api/runs/{id}/findings`로 공개된 데이터다(B-INV-3). 자기 레포 데이터가 자기
  레포 리뷰 안에 머무르므로 exfiltration이 아니다.
- claim은 LLM 생성, edited_text는 사람 작성(신뢰 소스)이지만 그래도 `render_context`가 데이터로
  펜싱한다(방어적, B-INV-6).
- provider는 절대 raise 하지 않으며 컨텍스트 수집 타임아웃/실패는 리뷰를 차단하지 않는다(B-INV-4/8).
- 소스는 read-only SELECT만 수행하며, worker 진행 중에도 WAL 하에서 별도 short-lived 커넥션으로
  안전하게 읽는다.

## 후속 증분 (같은 seam에 스택)

- **Slack 반응 루프 [구현됨]:** 리뷰 게시(`POST /api/runs/{id}/post`) 직후 `chat.postMessage`로 Slack 채널에
  요약을 올리고(`server/slack/client.py`, run↔channel:ts를 `slack_post`에 저장, 멱등), `POST /api/webhooks/slack`가
  `reaction_added`/`reaction_removed`를 v0 서명 검증 후 verdict로 매핑해 `feedback_signal`에 현재-상태로 적재한다.
  `feedback_source.slack_counts`/`slack_feedback_line`이 이 신호를 LLM 요약과 `/api/learn`에 블렌드한다.
  write-side는 `NullMemoryStore`(seam)가 아니라 코드베이스 컨벤션인 함수 모듈 `server/repos/feedback_repo.py`로
  구체화했다(클래스 seam은 team-mode용으로 미사용 유지). 라이브(봇 토큰/서명 시크릿)는 env-only 게이트, 나머지는 hermetic.
- **Slack 규약 수집:** 채널 메시지/규약을 별도 소스로 추가.
- **결정 메타 강화:** 필요 시 `finding.decided_at`/`decided_by` + append-only `finding_decision` 감사
  테이블을 추가해 시간순·리뷰어별 학습을 가능케 한다(현재는 `set_status`가 in-place 덮어쓰기라
  편집 사실만 `edited_text` 존재로 복원 가능).
- **웹 `/learn` 탭:** 현재 `StubSection` 플레이스홀더를 학습된 피드백 열람 UI로 교체(백엔드 주입과 독립).
