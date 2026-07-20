# Almighty PR Review Server — 설계 문서

- **Date:** 2026-07-07
- **Status:** 🚧 브레인스토밍 진행 중 (섹션별 확정 중 — 이 문서는 확정된 결정을 누적 기록하는 living doc)

---

## 1. 목적 / 비전

회사 프로젝트 레포에 올라오는 PR을 읽고 멀티벤더(Claude + Codex)로 독립 리뷰하는
**로컬 단일사용자 서버**. 리뷰 결과는 로컬 대시보드에서 트리아지 → 승인 후 GitHub에 포스팅.

장기 비전(이번 v1 범위 아님): 컨텍스트 인식 리뷰 → Slack 기반 자가 학습 → 회사 전반 프로젝트 관리 툴.

## 2. 확정된 결정 (Confirmed)

| 항목 | 결정 |
|------|------|
| 코드 베이스 | **Greenfield** — 기존 cockpit/dashboard 코드 상속 X, 교훈만 상속 |
| 소비 주체 | **v1 = 나 혼자 · 로컬 · 내 구독.** team-mode는 나중에 토글로 확장 (경계/seam만 미리 설계) |
| 엔진 | **claude / codex CLI 헤드리스** (API 키 미사용, 개인 구독 사용) — 서버 본체는 LLM API 어디에도 안 씀 |
| 리뷰 하네스 | 리뷰 워커는 **전용 리뷰 하네스**(격리된 config)로 구동 — 로컬 전역 프로파일(`~/.claude`, `~/.codex`, 전역 CLAUDE.md/skills/MCP) **미상속**. v1은 claude·codex가 **같은 하네스 공유**(각 CLI 포맷으로 어댑팅). per-repo·per-situation·per-vendor 하네스는 추후 |
| v1 리뷰 입력 | **diff + 레포 전체코드만** (외부 컨텍스트 없음). ContextProvider 주입 지점만 뚫어둠(no-op) |
| 벤더 | **claude·codex 둘 다 v1부터**, 각각 **on/off 토글**(전역·레포별) |
| 사전 스크리닝<br>(리뷰 필요성) | 새 PR마다 **가벼운 모델/low effort로 diff만 보고 변경 복잡도 사전 평가**(trivial/moderate/complex + 한줄 근거) → 오버뷰에 노출. (옵션) 임계 미만이면 비싼 풀리뷰 skip/수동전환 = rate-limit 절약. **v1 기능** |
| 병합(consensus) | **옵션. 기본 = OFF (벤더별 리뷰 각각 독립 출력).** 결정론적 병합/합의는 opt-in 토글 |
| 포스팅 형태 | 인라인 라인코멘트 **X** → **PR 코멘트**로. **사람·AI 모두 읽기 좋은 구조화 마크다운**(향후 학습루프가 파싱). 기본 = 벤더별 코멘트 각각 |
| 대시보드 | **라이트 테마 · 확장형 앱 셸.** 좌측 **내비 목록**으로 섹션 전환(리뷰/하네스편집/설정/LLM위키/자가학습). 리뷰 섹션 = **레포별 탭** + **오버뷰 → 디테일(트레이스) 드릴다운(drill 확정)**. **한글 가독성**(FHD 외부 디스플레이 기준) 고려 |
| 아키텍처 | **접근 1 — CLI 위의 얇은 오케스트레이터** (병합은 옵션으로만 흡수) |
| 스택 | **Python (FastAPI) + SQLite + 얇은 프론트(React/Vite)** |
| 안전 | 리뷰 워커는 **read-only 툴만**, write 경로는 서버가 **내 승인 후** 포스팅뿐, **격리 worktree**, 세마포어 동시성 |

## 3. 서브프로젝트 분해 & 빌드 순서

- **A (v1): 리뷰 서버 통합** — PR 감시 · 사전 스크리닝 · 멀티벤더 리뷰 · (옵션)병합 · 대시보드 · 승인 포스팅
- **B: 컨텍스트 주입 레이어** — Jira / 사내 DB / Graphify (플러그블)
- **C: Slack 학습 루프** — 승인·기각 신호 + Slack 논의 → 메모리/규칙 축적 → 프롬프트 진화(GEPA) → LLM wiki
- **비전(서브프로젝트 아님): 회사 PM 툴** — YAGNI, 현재 제외

빌드 순서: **A → B → C**.

## 4. ⭐ Deferred — 추후 추가 필수 (MUST ADD LATER)

> v1에서 의도적으로 뺐지만 **반드시 나중에 추가**해야 하는 것. seam(인터페이스)만 미리 뚫어두고 no-op/기본값으로 출발.

1. **외부 컨텍스트 주입 (서브프로젝트 B)** — v1은 코드만. 리뷰 프롬프트에 `ContextProvider` 주입 지점만 존재.
   추후 추가: **Jira 티켓 본문/수용기준(AC)**, **사내 DB 스키마·의미**, **Graphify 코드그래프(크로스레포/서브시스템 맥락)**.
   → 애초에 "단순 diff는 맥락 부족"이 프로젝트의 핵심 동기였으므로 B는 옵션이 아니라 필수 후속.
2. **하네스 다변화** — v1은 `harness/default` 하나를 두 벤더가 공유. 추후: **레포별**(`harness/repos/<repo>`),
   **상황별**(예: security-focus, perf-focus), **벤더별**(claude/codex 하네스 분리) 하네스 추가.
3. **병합/합의 고도화** — v1은 결정론적 병합(옵션, 기본 OFF)만. 추후: 고위험 SINGLE finding verify(반박) 패스,
   벤더 간 debate, 신뢰도 가중.
4. **성능 / 처리량 개선** — rate-limit 대응 고도화, 리뷰 결과 캐싱, 증분 리뷰(변경 파일만), 큐 백오프,
   벤더별 라우팅(비싼 effort는 필요한 곳만), 레포별 우선순위, **사전 스크리닝 기반 auto-gate 정책 튜닝**.
5. **team-mode** — 인증, per-user 시트(BYO-seat), per-reviewer GitHub 신원, 분산 동시성/큐.
   (v1은 단일 프로세스 세마포어. `Identity` / `RunnerPool` 추상화로 교체 가능하게 설계)
6. **학습 루프 (서브프로젝트 C)** — 승인/기각/수정 신호 + Slack 스레드를 신호로 메모리·리뷰규칙 축적,
   GEPA식 프롬프트 진화(`hermes-agent-self-evolution` 재활용), Graphify식 LLM wiki 누적.
   → 포스팅 코멘트를 "AI도 읽기 좋은 구조"로 만드는 이유가 이것(파싱해서 학습 신호로).
7. **webhook 트리거** — v1은 폴링(터널 불필요, ToS 중립). webhook은 로컬 터널 필요 → 추후.

## 5. 아키텍처 골격 (확정)

```
                       ┌─────────────────────────────────────┐
   GitHub (gh CLI) ◄── │  almighty-review server (FastAPI)   │
        ▲              │                                     │
        │ poll/post    │  ┌────────────┐   ┌───────────────┐ │
        │              │  │ Trigger    │──►│ Pre-screen    │ │
        │              │  │ (poll+수동)│   │ (가벼운 모델)  │ │
        │              │  └────────────┘   └──────┬────────┘ │
        │              │                   ┌──────▼────────┐ │
        │              │                   │ RunnerPool    │ │
        │              │                   │ (semaphore N) │ │
        │              │   isolated worktree (PR head sha)   │
        │              │   전용 리뷰 하네스 (전역 프로파일 격리)  │
        │              │    ├─ claude (read-only tools)      │
        │              │    └─ codex  (read-only tools)      │
        │              │              ┌─────────▼──────────┐ │
        │              │              │ 벤더별 findings 보존  │ │
        │              │              │ (옵션) Merge/Consensus│ │
        │              │              └─────────┬──────────┘ │
        │              │  ┌─────────────┐       │            │
        └──── approve ─│  │ Dashboard   │◄──────┤            │
                       │  │ 오버뷰→디테일 │   SQLite(findings)  │
                       │  │ (레포 탭·트레이스)│               │
                       │  └─────────────┘                    │
                       │  seams(v1=최소): HarnessProfile ·    │
                       │  ContextProvider · MemoryStore ·     │
                       │  Identity · RunnerPool               │
                       └─────────────────────────────────────┘
```

**안전 / 격리 결정 (기존 `web-extension-plan.md` audit이 짚은 지뢰 회피):**
- 리뷰 워커는 **read-only 툴만** (`Read,Grep,Glob`, 읽기용 `gh`). `git push` / `gh pr merge` 전면 차단.
- 유일한 write 경로 = 서버가 **내 승인 후** 코멘트 포스팅.
- **전용 리뷰 하네스** — 전역 `~/.claude`·`~/.codex`·CLAUDE.md·skills·MCP **미상속**. 리뷰 전용 config로만 구동
  (구현: 격리 config dir을 `CLAUDE_CONFIG_DIR` / `CODEX_HOME` 등으로 지정 + 명시적 툴 allowlist + MCP 최소/없음
   + 리뷰 system prompt. *정확한 플래그는 구현 시 각 CLI 문서로 확정.*)
- **병합은 옵션(기본 OFF)** — 기본은 벤더별 독립 리뷰 출력.
- **격리 worktree** — 내 로컬 레포 절대 안 건드림.
- **동시성 = asyncio 세마포어 N** (rate-limit 보호), 인터페이스로 추상화 → 나중 분산큐 교체.
- 모든 포스팅 코멘트에 `<!-- almighty-review [vendor] -->` 마커 → 일괄 관리/삭제 + 파싱.

**rate-limit 현실 경고 & v1 최소 완화책:** claude+codex 둘 다 여러 레포 자동폴링 = 단일 시트 rate limit 빠르게 소진.
v1 내장: **사전 스크리닝으로 풀리뷰 대상 선별**, 레포별 effort 기본값, **새 head sha일 때만 리뷰**,
고volume 레포 수동우선, rate-limit 시 백오프 큐.

## 6. 데이터 모델 (SQLite, v1)

핵심 컬럼만 표기(상세 DDL은 구현 시). seam 필드는 ★ 표시.

- **repo** — 모니터링 레포 + 설정
  `id, full_name, enabled, trigger_mode(auto|manual), poll_interval_sec, default_effort,
   vendor_claude_on, vendor_codex_on, merge_enabled(기본0), auto_post(기본0), ★harness_name(기본'default'), last_polled_at`
- **harness** ★ — 하네스 프로파일(seam; v1 = 'default' 1행)
  `id, name, scope(global|repo|situation), path(디스크 harness 디렉토리), note`
  *실제 내용(리뷰지침/툴 allowlist/MCP/effort)은 DB 아닌 디스크 `harness/<name>/`에 둠.*
- **pull_request** — 추적 PR
  `id, repo_id, number, title, author, head_sha, base_ref, state(open|closed|merged), url, last_reviewed_sha, first_seen_at, updated_at`
- **pre_screen** — PR×head_sha 사전 스크리닝 결과(가벼운 모델)
  `id, pr_id, head_sha, model, complexity(trivial|moderate|complex), score, reason(한줄), duration_ms, decided(review|skip|manual), created_at`
  *오버뷰는 PR별 최신 pre_screen을 **리뷰-필요성 배지**로 표시.*
- **review_run** — PR×head_sha 1회 실행
  `id, pr_id, head_sha, trigger, effort, merge_enabled(스냅샷), status(queued|running|done|failed|canceled), started_at, finished_at, error`
- **vendor_result** — run 내 벤더별 실행
  `id, run_id, vendor(claude|codex), status, duration_ms, tokens, raw_path, error`
- **finding** — 개별 finding
  `id, run_id, vendor_result_id, vendor, file, line, severity(critical|high|medium|low),
   category(bug|security|perf|style|other), claim, rationale, confidence,
   consensus(single|consensus; 병합ON시만), consensus_group_id, status(pending|approved|dismissed|edited|posted), edited_text, created_at`
- **posted_comment** — GitHub 포스팅 기록
  `id, run_id, vendor(claude|codex|merged), github_comment_id, url, marker, body(포스팅한 구조화 마크다운), posted_at`
- **app_settings** — 전역 설정(단일행)
  `default_effort, concurrency_limit(N), default_poll_interval, approval_gate_on, prescreen_model, prescreen_gate_threshold`

seam 반영: `harness_name`(HarnessProfile) · ContextProvider는 v1 테이블 없음(런타임 no-op) ·
Identity는 암묵(=나) · RunnerPool은 런타임(세마포어). team-mode/컨텍스트/학습 확장 시 테이블 추가.

## 7. 리뷰 파이프라인 (확정)

```
1. Trigger      레포별 폴링(간격 설정) → open PR 중 head sha ≠ last_reviewed_sha 만 감지 → 잡 enqueue.
                (+ 대시보드 수동 트리거)

2. Pre-screen   ★ 가벼운 모델/low effort로 **diff만** 보고 변경 복잡도·리뷰 필요성 평가
   (사전스크리닝) (trivial|moderate|complex + score + 한줄 근거). worktree 불필요(gh diff만).
                결과 → pre_screen 저장 → 오버뷰에 리뷰-필요성 배지로 노출.
                (옵션) 임계 미만이면 아래 3~6 skip 또는 수동전환 = rate-limit 절약.

3. Prepare      (진행 결정된 PR만) PR head sha로 격리 worktree 생성(gh PR ref fetch, read-only).
                리뷰 프롬프트 조립: PR 제목/본문/작성자 + 변경파일 + diff + "필요하면 레포 탐색" 지시.
                ※ ContextProvider.gather() → v1은 빈 값(seam만 존재)

4. Review       RunnerPool(세마포어 N)이 **전용 리뷰 하네스**로 두 워커를 병렬 구동(각 벤더 독립):
   (병렬)        · claude : CLAUDE_CONFIG_DIR 등으로 전역 프로파일 격리, read-only 툴, 하네스 리뷰지침/effort
                · codex  : CODEX_HOME 등으로 전역 프로파일 격리, read-only 샌드박스, 동일 하네스 어댑팅
                각 벤더가 공통 스키마 findings 반환
                {file, line, severity, category, claim, rationale, confidence}
                (스키마 위반 시 재시도) — 벤더별 결과는 독립 보존

5. (옵션) Merge  기본 OFF → 벤더별 리뷰 각각 출력. ON이면 결정론적 병합
                ((파일·라인 근접·카테고리)로 CONSENSUS/SINGLE 태깅, LLM 안 씀).

6. Persist      run + 벤더별 findings(status=pending) → SQLite

7. Triage       대시보드에서 approve / dismiss / edit (벤더별 뷰, 병합 켜면 통합 뷰)

8. Post         승인분을 **PR 코멘트**로 포스팅(인라인 X). 사람·AI 모두 읽기 좋은 **구조화 마크다운**:
                요약(벤더·건수·최고 severity) + 항목별 일관 헤더(severity/파일:라인/claim/rationale)
                + 말미에 파싱용 구조 블록(학습루프 소비). 기본=벤더별 코멘트 각각(병합시 통합 1건).
                모든 코멘트에 <!-- almighty-review [vendor] --> 마커.

9. Cleanup      worktree 제거

※ 2~9 전 과정이 각 PR의 **리뷰 트레이스**로 기록되어, 대시보드 디테일 뷰에서 타임라인으로 추적 가능.
```

## 8. 대시보드 (확정 방향) — 확장형 앱 셸

**라이트 테마 · 단일 웹앱.** 좌측 **내비게이션 목록**으로 섹션을 전환하는 확장형 앱 셸(새 섹션 추가가 쉬운 구조).

**내비 섹션(목록):**

| 섹션 | 범위 | 내용 |
|------|------|------|
| 리뷰 대시보드 | v1 | 아래 리뷰 인터랙션(기본 화면) |
| 하네스 편집 | v1 | 웹에서 리뷰 하네스 직접 편집 — 리뷰 system prompt·툴 allowlist·MCP·모델/effort·샌드박스 (v1=`default` 1개) |
| 설정 | v1 | 전역 기본값(effort/동시성 N/폴링/승인게이트/사전스크리닝 모델·임계) + 레포별 설정(트리거·effort·벤더 on/off·병합·auto-post·하네스) |
| LLM Wiki | C (Ground Truth MVP 구현) | 레포 코드·문서·정적 DB DDL을 read-only로 분석해 도메인·구조·데이터 모델·흐름·불변식을 근거와 함께 레포별 스냅샷으로 저장. 라이브 DB introspection·Graphify식 지식 그래프는 후속 |
| 자가 학습 | 🔜 C (v1=스텁) | **기능은 v1 플랜 밖(서브프로젝트 C).** v1은 nav 스텁 "실험 단계"만. 실제: 승인/기각·Slack 신호 기반 학습·규칙 진화 |

**리뷰 섹션 인터랙션(핵심):**
1. **레포별 탭** — 탭(전체 + 레포별)으로 레포 이동. 다중 레포 관리.
2. **오버뷰 → 디테일 드릴다운 (drill 확정):** 전체화면 오버뷰(PR 상황판 + 리뷰-필요성 배지 + severity 요약) ↔
   클릭 시 전체화면 디테일(**리뷰 트레이스 타임라인** + findings 트리아지 + 구조화 코멘트 프리뷰), 뒤로가기 복귀.

**타이포/가독성 (확정 요구):** 실제 화면에 **한글이 많고 MacBook + FHD(1920×1080) 외부 디스플레이** 환경 →
시스템 한글 폰트("Apple SD Gothic Neo" 등, 외부 폰트 금지), 본문 14~15px·줄간격 1.6+·중간 굵기,
저DPI에서 뭉개지지 않게 얇은 폰트/헤어라인 지양 + 충분한 대비.

**확장성:** "nav 목록 + 콘텐츠 영역" 단순 패턴 → 위키·자가학습 등 신규 섹션을 재설계 없이 추가.

**디자인 초안:** `docs/design-drafts/` — `variant-app.html`(앱 셸: drill 리뷰 + 설정 + 하네스편집 + 위키/자가학습 스텁, 한글 타이포).
(이전 초안 split/drill · cockpit/focus/kanban은 참고용 보존.)

## 9. Seam 인터페이스 상세

v1에서 최소/no-op으로 두되 경계만 확정할 인터페이스:
- **HarnessProfile** — 리뷰 실행 환경(리뷰 지침/system prompt, 툴 allowlist, MCP=최소, model/effort, 샌드박스)의
  격리 정의. v1: `harness/default` 하나를 두 벤더가 공유(벤더별 어댑터가 각 CLI 포맷으로 변환).
- **ContextProvider** — 외부 컨텍스트 수집(v1 no-op → B에서 Jira/DB/Graphify).
- **MemoryStore** — 승인/기각 신호·리뷰 산출물 축적(v1 저장만 → C에서 학습).
- **Identity** — 실행 주체/포스팅 신원(v1 = 내 시트/내 gh → team-mode에서 per-user).
- **RunnerPool** — 동시성/실행 스케줄(v1 = 단일프로세스 세마포어 → 분산큐).

## 10. 미해결 질문 (다음 세션 확정)

1. **대시보드** — 레이아웃 = **drill 확정** · **확장형 앱 셸(좌측 nav 목록) 확정**. v1 섹션: 리뷰·설정·하네스편집 / 스텁: LLM위키·자가학습.
2. **데이터 모델(§6) 리뷰** — 빠진 컬럼/테이블 없는지.
3. **기본값** — default_effort(추천 medium), concurrency N(추천 2), poll_interval(추천 60s).
4. **사전 스크리닝** — 어떤 가벼운 모델/effort(예: claude haiku / codex low), 복잡도 임계·auto-gate 정책.
5. **리뷰 하네스 초기 내용** — 리뷰 system prompt 톤/스코프, 툴 allowlist 구체 목록.
6. **구조화 코멘트 포맷** — 사람·AI 겸용 마크다운 + 파싱 블록의 정확한 스펙.
7. **한글 타이포/가독성** — FHD 외부 디스플레이 기준 폰트 스택·크기·굵기 최종 확정.
8. **하네스 웹 편집 범위** — v1에서 어디까지 편집 허용(프롬프트/allowlist/모델)·검증·롤백.
