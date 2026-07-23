# Almighty PR Review — 탐색 범위·청크 정합성·관측성 개선 Implementation Plan

> **실행 원칙:** 각 Task는 실패 테스트 → 실패 확인 → 최소 구현 → 관련 테스트 → 전체 테스트 순으로 진행한다. 기존 리뷰 결과를 조용히 삭제하거나 리뷰 실패로 승격하지 않는다. 한 Task의 전체 테스트가 녹색이 되기 전 다음 Task로 넘어가지 않는다.
>
> **구현 상태 (2026-07-22):** Milestone 1–5 구현과 독립 재리뷰 수정을 완료했다. 최종 게이트는 Python 686개 수집(2 skip, 실패 0), Vitest 98개 통과, production build, `git diff --check`, synthetic offline benchmark 통과다. benchmark 표본 수 gate는 의도적으로 잠겨 있으며 adjudicated 비독점 표본과 운영자 unlock 전에는 effective enforce가 되지 않는다.

## 목표

실제 3개 PR A/B 실험에서 확인한 다음 문제를 해결한다.

1. 리뷰 모델이 PR base/head를 넘어 다른 PR ref·unreachable commit·광범위한 이력을 탐색한다.
2. 대형 PR의 독립 청크가 서로의 변경 파일을 다시 조사해 비용과 중복 finding을 만든다.
3. 운영 환경에서는 성공한 벤더 실행의 토큰·도구 사용·범위 이탈을 관측할 수 없다.
4. 컨텍스트 총량 제한이 의미 블록이 아닌 문자열 중간을 자르며, 재조회 불가능한 외부 데이터의 보존을 보장하지 않는다.
5. 모델 self-confidence가 독립 검증 여부와 분리돼 있지 않다.

## 실험 근거

평가 산출물: `.pi-subagents/evals/review-context-20260722-110521/report.md`

| 방식 | 호출 | 도구 실행 | 토큰 | 모델 시간 | raw finding |
|---|---:|---:|---:|---:|---:|
| 현재 | 4 | 91 | 612,476 | 1,490.14초 | 14 |
| bounded evidence-first | 4 | 49 | 295,344 | 807.68초 | 10 |

고정 12-command 제한은 비용을 절반가량 줄였지만 독립적으로 확인된 결함을 놓쳤다. 따라서 이 플랜은 **하드 도구 횟수 제한을 도입하지 않는다.** 관측성, 변경 라인 소유권, plain snapshot 기반 우발적 history 접근 감소, 소프트 탐색 규약을 먼저 도입하고 실제 precision/recall 데이터로 예산을 조정한다.

> **독립 리뷰 반영 (2026-07-22):** 두 reviewer의 architecture/correctness 및 security/privacy/rollout 리뷰를 반영했다. 주요 수정은 `commentable_lines` 재사용 폐기, structured chunk ownership, destructive dedupe 폐기, observe/enforce 조기 도입, attempt/phase telemetry와 transaction 경계 명시, partial chunk retry, raw diagnostics 차단, plain snapshot의 보안 경계 과장 금지, per-chunk context retry persistence, verify independence 영속화, adjudicated paired benchmark/cohort rollout이다. 상세 disposition은 `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup-review.md`에 있다.

## 비목표

- 모든 컨텍스트를 범용 전처리 LLM으로 요약하지 않는다.
- 1차 구현에서 언어별 완전한 AST/호출 그래프를 만들지 않는다.
- semantic dedupe를 이유로 서로 다른 finding을 자동 삭제하지 않는다.
- 리뷰 모델에 Jira·DB·GitHub 자격증명이나 MCP를 제공하지 않는다.
- 모델이 레포 파일을 직접 읽는 기능은 제거하지 않는다.
- 이번 플랜에서 GitHub 자동 게시 정책을 변경하지 않는다.

---

## 확정 설계

### D1. 청크 소유권은 모델 출력이 아니라 **unsplit 원본 diff의 추가 라인**으로 결정한다

현재 `commentable_lines()`는 GitHub 댓글 가능 범위를 위해 추가 라인뿐 아니라 unchanged context line도 포함하므로 ownership에 재사용하지 않는다. 별도 `changed_lines()` 파서를 만들고, chunker가 원본 diff를 순회할 때 각 출력 조각에 ownership metadata를 함께 부착한다.

- structured chunk는 `text`, `index`, `owned_changed_lines`, `hash`를 가진다.
- oversized hunk도 임의 문자 절단 뒤 재파싱하지 않는다. 원본 hunk line counter를 유지하며 완전한 줄 단위로 나누고 각 continuation chunk에 유효한 hunk header를 다시 만든다.
- 모델은 현재 청크 밖의 레포 파일을 근거 확인용으로 읽을 수 있다.
- finding 기준 위치가 전체 PR의 실제 추가 라인이면 서버가 owner chunk로 재귀속한다.
- 삭제 전용 hunk, unchanged context line, 안전하지 않은 경로, 전체 PR 추가 라인이 아닌 위치는 posting-eligible finding이 아니다.
- observe 모드에서는 candidate와 scope 판정을 감사 저장하되 게시하지 않고, enforce 전환 후에만 최종 finding 저장 집합에서 제외한다.

### D2. dedupe는 초기에는 **비파괴 shadow grouping**만 수행한다

`(vendor,file,line,category)`는 같은 위치의 서로 다른 결함을 합칠 수 있고 model confidence도 교정되지 않았으므로 대표 선택·삭제에 사용하지 않는다.

- 1차 exact group은 `(vendor, normalized_file, owner_line, category, canonical_claim)`이 모두 같은 경우만 만든다.
- `canonical_claim`은 Unicode/공백/문장부호 정규화까지만 하며 의미 추론을 하지 않는다.
- 모든 원본 finding을 보존하고 `duplicate_group_id`, `duplicate_suggested`만 기록한다.
- posting suppression은 별도 `REVIEW_DEDUPE_MODE=observe|enforce`로 canary 검증 후 활성화한다.
- 서로 다른 vendor 결과는 consensus 계산을 위해 항상 유지한다.
- 서로 다른 파일/라인 또는 paraphrase 의미 중복은 자동 삭제하지 않는다. 후속 reducer도 `duplicate_of` 제안만 만들고 사람 감사 없이 원본을 삭제하지 않는다.

### D3. 탐색 범위 제어는 성능·정합성 방어이며 보안 경계로 과장하지 않는다

- 시스템 프롬프트: 현재 청크 추가 라인만 finding 기준점으로 사용, 직접 caller/callee·매핑·설정·테스트 중심, 후보별 반례 확인, 다른 PR/ref/history 조사 금지.
- 모델 cwd는 `git archive`로 만든 tracked-file plain snapshot을 우선 검토한다. snapshot에는 `.git`과 다른 refs가 없으므로 **우발적** history 탐색을 줄인다.
- plain snapshot만으로 absolute-path read, `git -C <persistent clone>`, symlink escape, runtime auth file read를 막을 수 있다고 주장하지 않는다. 현재 CLI의 read-only sandbox는 read containment 계약이 아니다.
- production 전 preflight에서 persistent clone, 인접 worktree, symlink escape, runtime auth/config에 대한 실제 tool read 가능성을 검사한다. 차단을 증명하지 못하면 이를 residual risk로 문서화하고, 강한 격리가 필요한 배포는 path-enforcing tool broker/container runner 설계 없이는 활성화하지 않는다.
- snapshot 생성은 symlink와 archive path를 검증하고 크기·파일 수·추출 시간을 cap한다. 실패 시 기존 worktree로 조용히 fallback하지 않고 해당 기능 flag를 disable/degrade한다.

### D4. 성공 stderr 원문은 영속화하지 않고 구조화 telemetry만 저장한다

원문 transcript에는 코드·경로·잠재적 비밀이 포함될 수 있으므로 DB/API에 저장하지 않는다.

`vendor_result.execution_meta` JSON에는 overwrite되는 단일 집계가 아니라 attempt/phase별 실행 envelope를 저장한다.

```json
{
  "schema_version": 1,
  "protocol_version": "evidence-v1",
  "cli": {"name": "codex", "version": "...", "event_schema": "..."},
  "model": "gpt-5.6-sol",
  "effort": "high",
  "attempts": [{
    "attempt": 1,
    "phase": "review|verify",
    "chunks": [{
      "index": 0,
      "status": "done|failed|timeout",
      "safe_error_code": null,
      "duration_ms": 1234,
      "input_tokens": null,
      "cached_input_tokens": null,
      "output_tokens": null,
      "total_tokens": null,
      "tool_calls": null,
      "paths_touched": null,
      "scope_reassigned": 0,
      "scope_rejected": 0,
      "duplicate_groups": 0,
      "stream_truncated": false,
      "telemetry_status": "ok|partial|unavailable"
    }]
  }]
}
```

- CLI가 제공하지 않는 값은 `null` + `telemetry_status=partial|unavailable`로 기록한다.
- 명령문, 파일 내용, 외부 컨텍스트 본문, stdout/stderr 원문, 토큰 원문은 telemetry에 저장하지 않는다.
- stdout/stderr/event stream은 전체 buffering하지 않고 byte/event cap 아래 incremental parse한다.
- JSON event contract를 exact CLI version별 실기기 preflight로 먼저 확인한다. 확인 전에는 stderr 정규식 파싱을 프로덕션 계약으로 삼지 않는다.
- successful-run telemetry는 findings/run/head commit transaction 안에서 함께 저장한다. 전원 실패처럼 findings transaction이 없는 경우만 failure attempt를 별도 안전 경로로 기록한다.

### D5. 컨텍스트는 재조회 가능성과 의미 블록을 기준으로 선택한다

장기적으로 `ContextResult.text` 하나를 바로 자르지 않고 trust/sensitivity/retention을 가진 `ContextBlock` 목록을 수집한다. chunk가 결정된 뒤 chunk별로 렌더하고 deterministic chunk hash/index에 연결된 manifest를 영속화해 retry도 같은 선택을 재사용한다.

우선순위:

1. 권한 있는 사람의 현재 PR 리뷰와 미해결 댓글
2. Jira acceptance criteria/summary
3. 승인된 review rule과 팀 피드백
4. live DB schema 사실
5. 레포에서 재조회 가능한 정적 문서·프로젝트 문서·체크인 DDL

현재 PR 댓글은 작성자 권한/봇 여부를 판정하지 못하면 일반 untrusted context로 취급하고 평가 ground truth로 사용하지 않는다. 문자열 중간 절단 대신 완전한 블록을 선택한다. 선택되지 않은 블록은 본문 없이 `original_chars`, `selected_chars`, `omitted_blocks`, `reason`만 meta에 기록한다. 선택 본문 저장 여부도 sensitivity/retention 정책으로 결정한다.

### D6. 검증은 선택적이고 상태를 confidence와 분리한다

`verify_status`를 `unverified|confirmed|refuted|contested|degraded`로 명시한다.

- high/critical 중 단일 벤더 finding을 우선 검증 대상으로 한다.
- 다른 활성 vendor가 있으면 다른 vendor를 우선한다.
- 같은 vendor 자기검증은 `independent=false`로 기록하고 `confirmed`를 만들지 않는다. 결과는 `supported_self|refuted|contested|degraded` 중 하나로 표시한다.
- 다른 vendor도 독립 ground truth가 아니라 `independent_model_check=true`일 뿐임을 UI에 명시한다.
- 검증 실패는 원 finding을 삭제하지 않고 `degraded`로 둔다.

---

## Milestone 0 — 실험을 회귀 기준으로 고정

### Task 0.1: 평가 보고서와 프로토콜 fixture 고정

**Files**
- Create: `tests/fixtures/review_eval/README.md`
- Create: `tests/fixtures/review_eval/chunk-overlap.patch`
- Modify: `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup.md`

- [ ] 실제 PR 원문을 fixture로 복사하지 않고 합성 diff로 다음 현상을 재현한다.
  - 두 청크
  - 청크 1 모델이 청크 2의 변경 라인을 반환
  - 동일 vendor가 같은 변경 위치를 두 번 반환
  - 전체 PR 변경 라인이 아닌 위치를 반환
- [ ] README에 실험의 공개 가능한 집계치만 기록하고 Jira/코드 본문은 포함하지 않는다.
- [ ] 전체 테스트 baseline을 기록한다: `.venv/bin/pytest -q`.

**Acceptance**
- proprietary diff/context가 테스트 fixture에 포함되지 않는다.
- 후속 ownership/dedupe 테스트가 외부 모델 없이 재현 가능하다.

---

## Milestone 1 — 관측성 계약과 저장

### Task 1.1: CLI telemetry preflight

**Files**
- Create: `scripts/review-cli-telemetry-preflight.py`
- Modify: `docs/vendor-cli-contract.md`
- Test: `tests/test_vendor_telemetry.py`

- [ ] 격리 runtime에서 Claude/Codex 각각 한 번씩 최소 read-only probe를 실행한다.
- [ ] Codex `--json`/`--output-last-message`, Claude `--output-format stream-json`의 실제 이벤트를 exact CLI version별로 확인한다.
- [ ] 최종 답변 추출, tool event, token usage, exit status/safe error code, per-chunk 성공/실패, truncation 형태를 문서화한다.
- [ ] Claude effort 플래그의 현재 코드와 `docs/vendor-cli-contract.md` 불일치를 실기기로 재검증하고 한쪽을 수정한다.
- [ ] preflight 출력은 이벤트 key/type과 숫자만 남기고 prompt/response/command/file content를 출력하지 않는다.
- [ ] 지원하지 않는 CLI 버전/schema는 명확히 `telemetry unavailable`로 판정하며 리뷰 실행 자체는 기존 text 모드로 degrade한다.
- [ ] stdout/stderr/event를 incremental parse하고 byte/event cap 초과 시 final output 보존 정책과 `stream_truncated=true`를 검증한다.

**Acceptance**
- 현재 설치 버전에서 최종 findings 파싱과 telemetry 수집을 동시에 할 수 있음이 실증된다.
- 실증되지 않은 stderr 형식에 프로덕션이 의존하지 않는다.

### Task 1.2: 벤더 실행 결과 타입 도입

**Files**
- Modify: `server/review/vendors.py`
- Modify: `server/pipeline.py`
- Test: `tests/test_vendors.py`, `tests/test_pipeline.py`

- [ ] `VendorExecution`을 추가한다.

```python
@dataclass
class VendorExecution:
    output: str
    status: str
    safe_error_code: str | None
    exit_code: int | None
    cli_name: str
    cli_version: str | None
    event_schema: str | None
    stream_truncated: bool
    telemetry: dict
```

- [ ] runner가 문자열만 반환하던 테스트더블을 한 번에 깨지 않도록 adapter 경계에서 legacy string을 `VendorExecution(..., telemetry unavailable)`로 정규화한다.
- [ ] `review()`는 findings와 execution envelope를 함께 반환하거나 별도 내부 메서드로 묶되, public 호출부는 한 형태로 통일한다.
- [ ] timeout/error에도 duration, per-chunk status, safe error code, telemetry availability를 남긴다.
- [ ] 부분 청크 실패를 vendor success로 숨기지 않고 `partial`로 집계하며 실패 chunk 재시도 또는 전체 재실행 필요 상태를 노출한다.
- [ ] raw command/file content가 telemetry에 들어가지 않는 allowlist validator를 추가한다.

**Acceptance**
- 기존 finding 파싱 결과가 변하지 않는다.
- telemetry 미지원이 리뷰 실패를 만들지 않는다.

### Task 1.3: telemetry 영속화와 조회 API

**Files**
- Modify: `server/db.py`
- Modify: `server/repos/review_repo.py`
- Modify: `server/api.py`
- Modify: `web/src/api.ts`
- Modify: `web/src/sections/ReviewSection.tsx`
- Test: `tests/test_db.py`, `tests/test_api.py`, `web/src/sections/ReviewSection.test.tsx`

- [ ] `vendor_result.execution_meta TEXT` nullable migration을 추가하고 `attempts[].phase/chunks[].status` schema를 검증한다.
- [ ] 저장 전에 schema/version/key/type/size cap을 검증하고 secret redaction을 적용한다.
- [ ] 기존 run은 `execution_meta=null`로 정상 조회한다.
- [ ] successful-run telemetry update는 `_persist`/`finish_run`/head barrier와 같은 `BEGIN IMMEDIATE` transaction에서 `commit=False`로 저장한다.
- [ ] 전원 실패·timeout은 findings transaction이 없으므로 failure envelope만 별도 commit하되 run 실패와 모순되지 않게 테스트한다.
- [ ] retry는 기존 metadata를 overwrite하지 않고 next attempt로 append한다.
- [ ] 리뷰 상세에는 원문 transcript가 아니라 토큰·tool call·scope/duplicate 집계만 표시한다.
- [ ] API 응답 크기 상한을 둔다.

**Acceptance**
- 대시보드와 API에서 prompt/command/file content가 노출되지 않는다.
- successful-run telemetry와 finding/run/head 상태가 한 transaction으로 커밋된다.
- retry와 verify phase가 이전 attempt를 덮어쓰지 않는다.

### Task 1.4: 진단 원문·오류·컨텍스트 privacy 정책 정리

**Files**
- Modify: `server/pipeline.py`, `server/review/vendors.py`, `server/repos/review_repo.py`, `server/api.py`, `server/retention.py`, `server/config.py`
- Test: `tests/test_api.py`, `tests/test_retention.py`, `tests/test_vendors.py`

- [ ] 성공 stdout, 실패 stderr, `vendor_result.error`, verifier output, `review_run.context_text`, `.raw` 파일의 sensitivity/retention/API 노출 정책을 표로 문서화한다.
- [ ] 오류는 raw stderr 대신 allowlisted safe error enum + redacted bounded message만 저장한다.
- [ ] 일반 API에서 `raw_path`를 제거하고 `/api/vendor-results/{id}/raw`는 기본 비활성화한다.
- [ ] 원문 진단이 꼭 필요하면 opt-in, 인증/감사, 짧은 retention, 파일 mode와 크기 cap을 강제한다. 암호화 저장이 구현되지 않으면 장기 보관을 허용하지 않는다.
- [ ] context 본문 저장은 block sensitivity 정책을 따르고, 민감 block은 hash/manifest만 보존한다.
- [ ] retention 비활성 기본값을 그대로 두지 않고 diagnostic artifact에 안전한 기본 TTL을 둔다.

**Acceptance**
- 정상 대시보드/API가 서버 파일 경로나 raw model transcript를 노출하지 않는다.
- 비활성 raw endpoint와 safe error shape가 회귀 테스트로 고정된다.

---

### Task 1.5: partial chunk 상태와 재시도 계약

**Files**
- Modify: `server/pipeline.py`, `server/repos/review_repo.py`, `server/api.py`, `web/src/sections/ReviewSection.tsx`
- Test: `tests/test_pipeline.py`, `tests/test_api.py`, `web/src/sections/ReviewSection.test.tsx`

- [ ] chunk별 `done|failed|timeout`을 execution envelope에 남기고, 일부만 성공하면 vendor status를 `partial`로 저장한다.
- [ ] review run은 다른 성공 결과를 보존하되 UI/API에서 `done with partial coverage`를 명확히 표시한다.
- [ ] manual retry는 `failed|partial` vendor를 선택하고, deterministic chunk hash가 일치하면 실패 chunk만 재실행한다.
- [ ] chunk hash/context manifest가 불일치하면 부분 retry를 거부하고 현재 head 전체 재리뷰를 요구한다.
- [ ] 성공 retry attempt는 이전 failure attempt를 보존하며 finding ownership/dedupe 경로를 다시 거친다.

**Acceptance**
- 한 청크 실패가 조용한 완전 성공으로 보이지 않는다.
- 이미 성공한 청크를 불필요하게 재실행하지 않고 실패 청크를 복구할 수 있다.
- stale head/context/chunker 변경에서는 과거 chunk를 현재 결과에 섞지 않는다.

---

## Milestone 2 — 청크 소유권과 보수적 dedupe

### Task 2.1: 전역 changed-line ownership index

**Files**
- Modify: `server/review/diff_filter.py`
- Test: `tests/test_diff_filter.py`

- [ ] GitHub 게시 가능 라인용 기존 `commentable_lines()`와 분리된 `changed_lines(full_diff)`를 추가한다. 추가 라인만 포함하고 unchanged context/deletion은 제외한다.
- [ ] `chunk_by_budget()`의 장기 반환형을 `ReviewChunk(text,index,owned_changed_lines,hash)`로 바꾸되 legacy wrapper로 기존 호출을 단계적으로 전환한다.
- [ ] 파일 경로를 POSIX 상대경로로 정규화하고 absolute/`..`/NUL/quoted-path edge case를 처리한다.
- [ ] oversized hunk를 완전한 줄 단위로 나누고 continuation에 유효한 adjusted hunk header와 line metadata를 부착한다. 임의 문자 절단으로 line ownership을 잃지 않는다.
- [ ] rename, quoted path, deletion-only hunk, 매우 긴 단일 추가 라인, oversized single-file hunk를 테스트한다.
- [ ] 동일 변경 라인이 복수 청크에 나타나면 deterministic first-owner가 아니라 invariant 오류로 검출한다.

**Acceptance**
- 원본 diff 의미와 hard cap을 보존하고, 각 chunk text는 독립 파싱 가능하다.
- ownership index는 전체 PR의 실제 추가 라인을 정확히 한 번 소유한다.
- unchanged context line은 ownership에 절대 포함되지 않는다.

### Task 2.2: finding scope 재귀속·거부

**Files**
- Modify: `server/models.py`, `server/db.py`, `server/repos/finding_repo.py`
- Modify: `server/pipeline.py`, `server/formatter.py`, `server/api.py`
- Modify: `web/src/api.ts`, `web/src/sections/ReviewSection.tsx`
- Test: `tests/test_db.py`, `tests/test_pipeline.py`, `tests/test_formatter.py`, `tests/test_api.py`, `web/src/sections/ReviewSection.test.tsx`

- [ ] `REVIEW_SCOPE_GUARD_MODE=observe|enforce`를 이 Task에서 도입하고 기본을 `observe`로 둔다. repo cohort override와 kill switch를 지원한다.
- [ ] nullable `finding.source_chunk_index`, `owner_chunk_index`, `scope_status`, `posting_eligible` migration과 legacy-row 정규화를 추가한다.
- [ ] candidate에 `source_chunk_index`, `owner_chunk_index`, `scope_status=owned|reassigned|would_reject|rejected`, `posting_eligible`을 부착하고 persistence/API 경로를 정의한다.
- [ ] owner index에 존재하면 해당 owner로 재귀속한다.
- [ ] file/line이 전체 PR added-line이 아니면 **모든 모드에서 GitHub posting에는 사용하지 않는다.** observe에서는 감사 candidate로 보존하고 enforce에서 최종 finding 집합에서 제외한다.
- [ ] formatter의 summary body와 inline comment 경로가 모두 `posting_eligible`을 공유하도록 한다.
- [ ] 거부/재귀속 수와 safe reason을 execution telemetry에 집계한다.
- [ ] raw output 정책은 Task 1.4를 따르고 ownership 감사의 기본 저장소로 사용하지 않는다.
- [ ] retry 경로도 동일 ownership 함수와 guard mode를 공유한다.

**Acceptance**
- 청크 1이 청크 2 변경 라인을 반환해도 유효 finding을 잃지 않고 owner 2로 귀속된다.
- unchanged line finding은 observe/enforce 모두 GitHub 게시 후보가 되지 않는다.
- observe의 감사 candidate와 실제 게시 finding을 API/UI가 명확히 구분한다.
- 전체 청크가 성공했지만 posting eligible candidate가 0건인 경우는 정상 done이며 telemetry로 구분된다.

### Task 2.3: non-destructive duplicate shadow grouping

**Files**
- Modify: `server/models.py`, `server/db.py`, `server/repos/finding_repo.py`
- Modify: `server/review/merge.py`, `server/pipeline.py`, `server/api.py`
- Test: `tests/test_merge.py`, `tests/test_pipeline.py`, `tests/test_api.py`

- [ ] `REVIEW_DEDUPE_MODE=observe|enforce`를 추가하고 기본 observe, repo cohort override, kill switch를 둔다.
- [ ] consensus 전에 `group_exact_vendor_duplicates()`를 실행한다.
- [ ] key는 `(vendor, normalized_file, owner_line, category, canonical_claim)`로 제한한다. confidence는 grouping/대표 판단에 사용하지 않는다.
- [ ] 같은 file/line/category라도 claim이 다르면 별도 finding으로 보존하는 회귀 테스트를 추가한다.
- [ ] 모든 원본 row에 nullable `duplicate_group_id`, `duplicate_suggested`를 저장한다.
- [ ] observe에서는 모두 표시하되 그룹만 노출한다. enforce에서도 suppress된 원본은 감사 데이터로 보존하고 posting만 대표 1건으로 제한한다.
- [ ] 다른 vendor는 접지 않는다.
- [ ] group 수와 source chunk 목록을 telemetry에 기록한다.
- [ ] merge on/off, retry 모두 같은 grouping 경로를 사용한다.

**Acceptance**
- 같은 위치의 서로 다른 결함은 절대 자동 병합되지 않는다.
- exact duplicate는 shadow group으로 관측 가능하다.
- canary 기준 충족 전 destructive delete/post suppression은 활성화되지 않는다.

---

## Milestone 3 — 탐색 범위 제한

### Task 3.1: evidence-v1 리뷰 프로토콜

**Files**
- Modify: `harness/default/review-system-prompt.md`
- Modify: `server/pipeline.py`
- Modify: `server/config.py`
- Test: `tests/test_pipeline.py`, `tests/test_vendors.py`

- [ ] 다음 규칙을 공통 프롬프트에 추가한다.
  - finding 기준 위치는 현재 diff chunk의 추가/수정 라인
  - unchanged 파일은 근거 검증용으로만 읽기
  - 직접 caller/callee, DTO/entity mapping, configuration, exception handling, focused tests 우선
  - 후보마다 guard/caller/test를 통한 반례 확인
  - 다른 branch/PR ref/reflog/unreachable commit/history 조사 금지
- [ ] 고정 tool-call 하드캡은 추가하지 않는다.
- [ ] `REVIEW_PROTOCOL_VERSION = "evidence-v1"`을 telemetry/run meta에 기록한다.
- [ ] prompt snapshot 테스트로 nonce를 정규화한 뒤 필수 규칙을 확인한다.

**Acceptance**
- 모델이 레포 파일을 읽을 수 있다는 기존 계약은 유지된다.
- 프로토콜 버전을 결과와 함께 재현할 수 있다.

### Task 3.2: plain tracked snapshot·read containment preflight

**Files**
- Create: `server/review/snapshot.py`
- Create: `scripts/review-read-containment-preflight.py`
- Create: `tests/test_review_snapshot.py`
- Modify: `docs/vendor-cli-contract.md`

- [ ] exact head에서 tracked files만 담은 plain snapshot을 만든다. archive entry의 absolute/`..`/NUL과 snapshot 밖을 가리키는 symlink를 거부한다.
- [ ] 파일 수, 총 추출 bytes, 단일 파일 bytes, 생성 시간을 cap하고 timeout/cancel cleanup을 보장한다.
- [ ] snapshot cwd에서 일반 Read/Grep/Glob과 Codex shell search는 성공하고 `.git`, ref, reflog, unreachable object는 존재하지 않음을 테스트한다.
- [ ] 실제 Claude/Codex 도구로 다음 read 시도를 preflight한다: persistent clone absolute path, adjacent worktree, symlink escape, `$CODEX_HOME/auth.json`, Claude credential/config path, `git -C <clone>`.
- [ ] 각 시도의 허용/차단 결과를 content 없이 문서화한다. 현재 sandbox가 차단하지 못하면 `read_containment=unproven`으로 기록한다.
- [ ] OS/container/tool-broker 기반 path allowlist를 별도 spike로 평가하되, CLI 본체의 auth read와 모델 tool child의 auth read를 분리할 수 있는지 증명한다.

**Stop condition**
- plain snapshot 자체의 안전 추출·cleanup을 실증하지 못하면 production 배선을 중단한다.
- read containment를 증명하지 못하면 이 기능을 **보안 경계로 홍보하거나 강한 격리 모드로 활성화하지 않는다.** prompt/snapshot은 우발적 scope drift 감소용 defense-in-depth로만 사용한다.

### Task 3.3: 벤더 cwd를 plain snapshot으로 전환

**Files**
- Modify: `server/pipeline.py`, `server/review/verify.py`, `server/review/snapshot.py`
- Test: `tests/test_pipeline.py`, `tests/test_vendors.py`, `tests/test_verify.py`

- [ ] 컨텍스트 수집과 trusted base 문서 조회는 기존 worktree에서 끝낸다.
- [ ] review/verify vendor cwd만 동일 head의 plain snapshot으로 설정한다.
- [ ] 여러 vendor는 하나의 immutable snapshot을 공유하고, subprocess는 쓰기 불가 상태를 유지한다.
- [ ] snapshot failure에서 기존 history-bearing worktree로 조용히 fallback하지 않는다. feature flag/availability를 명확히 degrade한다.
- [ ] cancel/timeout/all-vendor-failed에 snapshot cleanup 결과를 확인한다.
- [ ] 기존 `prepared_worktree` cleanup도 remove returncode를 검사하고 prune/recheck한다. cleanup 오류는 active exception을 덮지 않으면서 run diagnostic에 남긴다.

**Acceptance**
- 모델 cwd에는 PR-head tracked files만 있고 `.git`/history가 없다.
- accidental `git fsck`/other-ref 탐색은 실패한다.
- absolute path read containment는 preflight에서 증명된 수준만 문서화하며 미증명 위험을 숨기지 않는다.
- 리뷰 완료 후 snapshot과 worktree cleanup, persistent clone 재사용이 정상이다.

**2026-07-23 Task 0.3 update:** plain snapshot cleanup no longer uses
`ignore_errors=True`; writable restoration/rmtree failure raises the content-safe
`snapshot_cleanup_failed` diagnostic and residue is tested. Vendor credential cleanup is
vendor-isolated and tested on cancellation, partial setup failure, and unlink failure.
`prepared_worktree`의 기존 `ignore_errors=True` 제거·prune/recheck는 아직 별도 미완료
항목이며 이번 snapshot/runtime 완료 주장에 포함하지 않는다.

---

## Milestone 4 — 컨텍스트 블록 예산

### Task 4.1: ContextBlock 계약

**Files**
- Modify: `server/context/base.py`
- Modify: all providers under `server/context/`
- Test: `tests/test_context.py`, `tests/test_jira.py`, `tests/test_live_mssql_source.py`

- [ ] `ContextBlock`을 추가한다.

```python
@dataclass(frozen=True)
class ContextBlock:
    source: str
    block_id: str
    text: str
    priority: int
    recoverable_from_repo: bool
    trust_class: str
    sensitivity: str
    retention: str
    relevant_files: tuple[str, ...] = ()
```

- [ ] provider는 기존 text와 함께 blocks를 점진적으로 제공한다. 전환 중 legacy text는 단일 untrusted/sensitive block으로 보수적으로 정규화한다.
- [ ] block_id에는 비밀·본문을 넣지 않고 provider-local stable identifier만 사용한다.
- [ ] Jira는 summary와 acceptance criteria를 description보다 높은 priority로 분리한다.
- [ ] static provider는 문서 단위 block과 path를 제공한다.
- [ ] Current PR 리뷰는 author association/권한을 확인할 수 있는 경우에만 `authorized_human`으로 분류하고, 봇/미확인 작성자는 untrusted로 유지한다.

**Acceptance**
- legacy provider가 한 번에 깨지지 않는다.
- 모든 block은 소스·재조회 가능성·크기를 감사할 수 있다.

### Task 4.2: whole-block selector와 omitted manifest

**Files**
- Modify: `server/context/base.py`
- Modify: `server/context/composite.py`
- Modify: `server/pipeline.py`
- Test: `tests/test_context.py`, `tests/test_pipeline.py`

- [ ] 선택 정렬은 priority → non-recoverable first → relevant-files overlap → stable source/block_id 순으로 한다.
- [ ] 블록이 예산을 넘으면 중간 절단하지 않고 제외한다. 단일 필수 블록이 per-source cap보다 큰 경우 provider가 안전한 문단/항목 단위로 먼저 나누며, 마지막 방어 truncate에는 명시적 marker를 붙인다.
- [ ] source별 최소 reserved budget은 설정값이 아니라 selector 상수와 테스트로 시작한다.
- [ ] context meta에 original/selected chars, selected/omitted block count, omission reason을 저장한다.
- [ ] context block은 한 번 수집한 뒤 chunk가 확정된 후 chunk별로 렌더한다. PR-wide review rules/Jira AC는 모든 청크에 유지한다.
- [ ] `review_run.context_meta`에 sanitized block manifest와 deterministic chunk hash별 selected block ids/hash를 저장한다. 민감 본문은 retention 정책이 허용할 때만 별도 저장한다.
- [ ] retry는 저장된 chunk hash/manifest가 현재 재생성 chunk와 일치할 때 동일 selection을 재사용한다. 불일치 시 기존 단일 `context_text`로 조용히 fallback하지 말고 명시적으로 context를 재수집하거나 retry를 거부한다.

**Acceptance**
- 닫는 nonce fence와 block header가 절단되지 않는다.
- 재조회 불가능한 고우선순위 block이 static 문서보다 먼저 보존된다.
- 청크마다 다른 렌더가 재현 가능하게 저장되고 retry에서도 동일하다.
- UI에서 제외 사실과 sensitivity 때문에 본문 미보존인 사실을 확인할 수 있다.

---

## Milestone 5 — 검증 상태와 선택 검증

### Task 5.1: verify 상태 계약 정리

**Files**
- Modify: `server/models.py`, `server/db.py`, `server/repos/finding_repo.py`, `server/api.py`
- Modify: `server/review/verify.py`, `server/pipeline.py`, `server/formatter.py`
- Modify: `web/src/api.ts`, `web/src/sections/ReviewSection.tsx`
- Test: `tests/test_db.py`, `tests/test_api.py`, `tests/test_verify.py`, `tests/test_pipeline.py`, `web/src/sections/ReviewSection.test.tsx`

- [ ] 미검증 상태를 null 대신 `unverified`로 API에서 정규화하되 기존 DB null과 호환한다.
- [ ] verdict와 finding persistence에 nullable `verify_independent`와 `verify_evidence_status`를 추가한다.
- [ ] 같은 vendor 자기검증은 independent=false이며 `supported_self`까지만 가능하고 confirmed를 만들지 않는다.
- [ ] 다른 vendor 검증도 ground truth가 아니라 independent model check로 표시한다.
- [ ] refuted finding은 삭제하지 않고 confidence 조정과 rationale을 감사 가능하게 유지한다.
- [ ] 사람 UI에서 모델 confidence, verify 상태, 독립 모델 여부를 분리 표시한다.

**Acceptance**
- `confirmed`가 실제 실행되지 않은 경우 붙지 않는다.
- 검증 실패는 review run을 실패시키지 않는다.

### Task 5.2: 선택 검증 정책

**Files**
- Modify: `server/pipeline.py`
- Modify: `server/config.py`
- Modify: settings/API/UI only if an operator control is required
- Test: `tests/test_pipeline.py`, `tests/test_verify.py`

- [ ] 초기 기본은 기존 `verify_singles_on`을 존중한다.
- [ ] 정책 대상은 high/critical + single + owned/reassigned valid finding으로 제한한다.
- [ ] verify prompt에는 전체 diff 대신 finding owner chunk와 최소 manifest를 전달해 비용을 제한한다.
- [ ] refuter는 다른 활성 vendor 우선, 없으면 same-vendor independent=false이며 confirmed 금지.
- [ ] verifier telemetry도 vendor execution meta의 새 attempt/`phase=verify`로 append한다.

**Acceptance**
- verify가 꺼진 레포의 비용은 증가하지 않는다.
- 켜진 경우에도 전체 대형 diff가 finding마다 반복되지 않는다.

---

## Milestone 6 — 벤치마크와 단계적 롤아웃

### Task 6.1: 오프라인 benchmark runner

**Files**
- Create: `scripts/review-benchmark.py`
- Create: `tests/fixtures/review_benchmark/manifest.schema.json`
- Modify: `.gitignore`
- Test: `tests/test_review_benchmark.py`

- [ ] fixture manifest는 synthetic/공개 가능한 patch 경로, issue-level adjudicated defect id, 허용 file/line range, claim matching rubric, known-clean range를 가진다.
- [ ] 실 PR diff·Jira·context는 git에 저장하지 않고 접근 통제된 local private manifest에서만 참조한다.
- [ ] 평가용 run은 current PR review/comment context를 비활성화해 label leakage를 막는다.
- [ ] baseline/candidate를 동일 head에서 반복 paired run하고 model stochasticity를 기록한다.
- [ ] 지표 정의를 고정한다: issue-level precision, recall, changed-line validity, exact/shadow duplicate rate, total tokens, tool calls, duration, timeout/partial rate.
- [ ] model/effort/protocol/prompt hash/diff hash/context manifest hash/chunker version/CLI version/adapter version을 기록한다.
- [ ] benchmark는 기본 test suite에서 외부 모델을 호출하지 않는다. 모델 실행은 명시적 opt-in이다.

### Task 6.2: observe → enforce 롤아웃

**Files**
- Modify: `server/config.py`
- Modify: `server/pipeline.py`
- Modify: `README.md`
- Test: `tests/test_pipeline.py`, `tests/test_config.py`

- [ ] Task 2에서 도입한 scope/dedupe mode를 repo별 cohort로 운영한다: control, observe, canary-enforce. 각 기능은 독립 kill switch를 가진다.
- [ ] schema/API는 flag rollback 뒤에도 신규 nullable 필드를 읽을 수 있어야 하며 downgrade migration을 요구하지 않는다.
- [ ] 최소 표본은 단순 20 run이 아니라 PR size strata별 adjudicated defect 수와 95% CI/non-inferiority margin으로 정한다. 초기 제안값은 benchmark pilot 후 문서에 고정한다.
- [ ] paired baseline/candidate와 충분한 maturation 기간 뒤 전환한다.
- [ ] enforce 전환 기준:
  - issue-level recall 하한이 사전 정의한 non-inferiority margin 이상
  - precision 하한 비열화 없음
  - duplicate posting rate 감소
  - would-reject 후보의 사람 승인 사례가 원인 분석 없이 남아 있지 않음
  - partial/timeout/전체 실패율 허용 범위 이내
- [ ] 자동 rollback 기준: recall/precision guard 위반, partial/timeout 급증, telemetry/schema incompatibility, containment preflight 실패.
- [ ] 기준 미달이면 enforce하지 않고 protocol/context selector만 조정한다.

---

## 구현 순서와 커밋 단위

1. `test: add synthetic structured-chunk ownership fixture`
2. `feat(review): capture versioned sanitized execution envelopes`
3. `fix(review): lock down raw diagnostics and safe errors`
4. `feat(review): assign candidates to added-line chunk owners in observe mode`
5. `feat(review): add non-destructive duplicate shadow groups`
6. `feat(review): add evidence-v1 scoped review protocol`
7. `feat(review): run reviewers from plain tracked snapshots`
8. `feat(context): select and persist per-chunk context block manifests`
9. `feat(review): persist verification independence state`
10. `test(review): add adjudicated opt-in benchmark and cohort rollout gates`

각 커밋 전:

```bash
.venv/bin/pytest -q
cd web && npm test -- --run && npm run build
```

## 최종 성공 기준

- [ ] 모델 cwd는 PR-head tracked snapshot이며 `.git`/다른 ref/unreachable history를 포함하지 않는다. absolute-path read containment의 미증명 위험은 문서와 preflight 상태에 명시된다.
- [ ] 모든 posting-eligible finding은 전체 PR의 실제 추가 라인 owner에 귀속된다. observe 감사 candidate는 별도 상태로 저장된다.
- [ ] exact duplicate는 비파괴 shadow group으로 관측되며 canary 기준 전에는 suppress/delete되지 않는다.
- [ ] 대형 PR에서 source chunk와 owner chunk, 재귀속/거부/duplicate group, partial chunk 수가 telemetry로 보인다.
- [ ] 성공 transcript·명령문·파일 내용·raw stderr와 서버 파일 경로는 일반 DB/API에 저장·노출되지 않는다.
- [ ] successful-run telemetry와 finding/run/head 상태가 한 transaction으로 커밋된다.
- [ ] retry/verify가 attempt/phase metadata를 덮어쓰지 않는다.
- [ ] 컨텍스트가 의미 블록 중간에서 잘리지 않고 chunk별 manifest가 retry 가능하게 남는다.
- [ ] 검증 여부·독립 모델 여부가 confidence와 별도로 영속화·표시된다.
- [ ] 외부 모델 없는 전체 테스트가 결정적으로 통과한다.
- [ ] adjudicated paired benchmark와 cohort 기준을 충족하기 전 scope/dedupe enforce를 켜지 않는다.
