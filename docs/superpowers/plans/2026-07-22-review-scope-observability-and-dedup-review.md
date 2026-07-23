# Review — 탐색 범위·청크 정합성·관측성 개선 플랜

**대상:** `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup.md`
**리뷰 방식:** fresh-context reviewer 2개 병렬 실행

- Reviewer A: architecture/correctness/implementation feasibility
- Reviewer B: security/privacy/rollout/evaluation quality

**최종 판정:** **GO WITH REQUIRED CHANGES**

초안의 방향(관측성 우선, 하드 tool cap 금지, chunk ownership, 컨텍스트 block화, 선택 검증)은 타당했다. 다만 초안 그대로 구현하면 changed-line 불변식 위반, 서로 다른 결함의 오병합, 원문 진단 노출, retry telemetry 유실, snapshot 격리 과장 문제가 있어 필수 수정 후에만 실행 가능하다.

## 반영한 blocker/high finding

### R1. ownership에 `commentable_lines()`를 쓰면 unchanged context line까지 허용됨

- **판정:** 수용, blocker
- **코드 근거:** `server/review/diff_filter.py:commentable_lines`는 GitHub 댓글 가능 범위를 위해 `+`와 context line을 함께 반환한다.
- **수정:** 별도 `changed_lines(full_diff)`를 만들고 실제 추가 라인만 ownership에 사용하도록 D1/Task 2.1 수정.

### R2. oversized hunk의 임의 문자 분할은 독립 chunk line ownership을 복원할 수 없음

- **판정:** 수용, blocker
- **코드 근거:** 현재 `chunk_by_budget`은 매우 큰 단일 file block을 문자 단위로 자르므로 continuation 조각에 `@@` line state가 없다.
- **수정:** `ReviewChunk(text,index,owned_changed_lines,hash)` structured 반환, 원본 hunk line counter 유지, 완전한 줄 단위 분할과 adjusted hunk header를 계획에 추가.

### R3. `(vendor,file,line,category)` destructive dedupe는 같은 줄의 서로 다른 결함을 오병합함

- **판정:** 수용, blocker
- **실험 근거:** PR #2663에서 동일 file/line/category에 서로 다른 V000 결함 두 건이 나왔다.
- **수정:** destructive dedupe 폐기. canonical claim까지 완전히 같은 exact duplicate만 shadow group으로 묶고 모든 원본을 보존. confidence는 grouping에 사용하지 않음. 별도 observe/enforce flag 도입.

### R4. scope observe/enforce 도입 시점과 posting 정책이 모순됨

- **판정:** 수용, high
- **문제:** 초안 Task 2.2는 즉시 reject, Task 6은 나중에 observe를 도입했다. summary formatter는 persisted finding을 모두 본문에 넣을 수 있다.
- **수정:** guard mode를 Task 2.2로 이동. `posting_eligible`을 summary/inline 공통 게이트로 사용. observe candidate와 게시 finding을 구분. 최종 불변식도 posting-eligible 기준으로 수정.

### R5. telemetry와 finding transaction 경계가 분리될 위험

- **판정:** 수용, high
- **코드 근거:** 현재 vendor result 종료는 `_persist`/`finish_run` transaction보다 먼저 commit된다.
- **수정:** successful-run execution meta는 finding/run/head barrier와 같은 `BEGIN IMMEDIATE`에 저장. 전원 실패만 별도 failure 경로. Task 1.3에 명시.

### R6. 단일 `chunks[]` metadata는 retry/verify attempt를 덮어씀

- **판정:** 수용, high
- **수정:** `attempts[].phase(review|verify).chunks[]` envelope로 변경. CLI/version/schema/status/error/truncation 포함. retry append 계약 추가.

### R7. partial chunk 실패가 조용히 완전 성공으로 보이고 재시도되지 않음

- **판정:** 수용, high
- **코드 근거:** 현재 `_run_vendor`는 일부 청크 성공 시 error를 버리고 vendor success로 반환한다.
- **수정:** Task 1.5 추가. vendor `partial`, failed chunk hash 기반 선택 재시도, context/chunker mismatch 시 전체 재리뷰.

### R8. chunk별 context selection은 현재 단일 `review_run.context_text`와 retry로 재현 불가

- **판정:** 수용, high
- **수정:** block은 한 번 수집하되 chunk 확정 후 렌더. chunk hash별 selected block manifest/hash를 저장하고 retry에서 일치 검증.

### R9. verify independence가 DB/API/UI에서 유실됨

- **판정:** 수용, high
- **수정:** `finding.verify_independent`, `verify_evidence_status`를 DB/repository/API/web까지 배선하도록 Task 5.1 파일 범위 확대. same-vendor self-check는 confirmed 금지.

### R10. `.git` 포인터 제거는 filesystem containment가 아님

- **판정:** 수용, critical
- **문제:** 모델 tool이 absolute path, `git -C`, 인접 clone, auth file을 읽을 가능성을 막지 못한다. read-only sandbox는 write 제한이지 read allowlist 증명이 아니다.
- **수정:** `.git` 제거 방식을 plain tracked snapshot으로 변경하되 이것도 우발적 history 접근 감소용 defense-in-depth로만 규정. persistent clone/adjacent worktree/symlink/auth read preflight와 residual risk 명시. 강한 격리는 path-enforcing tool broker/container가 증명될 때만 주장.

### R11. raw stdout/stderr/error/context/API 노출 정책이 불완전함

- **판정:** 수용, critical
- **코드 근거:** 성공 stdout은 `.raw`, raw endpoint와 `raw_path`가 API에 노출되고 실패 stderr는 error로 저장될 수 있다.
- **수정:** Task 1.4 추가. safe error enum, raw endpoint 기본 비활성, raw_path API 제거, 짧은 기본 TTL, opt-in/auth/audit 정책, 민감 context는 hash/manifest만 저장.

### R12. telemetry runner가 exit/CLI schema/stream cap을 표현하지 못함

- **판정:** 수용, high
- **수정:** `VendorExecution`에 status, safe error, exit code, CLI/version/schema, truncation 추가. incremental stream parse와 byte/event cap, exact version fallback 추가. Claude effort 계약 재검증 포함.

### R13. 3개 PR/20개 run으로 recall 비열화를 증명할 수 없음

- **판정:** 수용, high
- **수정:** issue-level adjudicated oracle, 허용 line range와 matching rubric, known-clean, paired repetitions, current-review context 비활성화, PR size strata, CI/non-inferiority margin으로 benchmark/rollout 수정.

### R14. 기능별 canary/kill switch가 없음

- **판정:** 수용, high
- **수정:** scope와 dedupe에 독립 observe/enforce flag, repo cohort, kill switch, rollback 기준, nullable schema 호환을 추가.

## 선택 반영한 medium finding

### R15. ContextBlock에 trust/sensitivity/retention이 필요

- **판정:** 수용
- **수정:** `trust_class`, `sensitivity`, `retention` 필드 추가. current PR comment는 author authorization 확인 전 untrusted이며 평가 label로 사용하지 않음.

### R16. 재현성 hash가 부족함

- **판정:** 수용
- **수정:** model/effort/protocol 외 prompt hash, diff hash, context manifest hash, chunker/CLI/adapter version 기록.

### R17. worktree cleanup 실패가 숨겨짐

- **판정:** 수용
- **수정:** snapshot Task 3.3에서 worktree remove returncode, prune/recheck, active exception 보존 규칙을 추가.

## 보류한 사항

### S1. 서로 다른 file/line의 semantic duplicate 자동 제거

- **판정:** 보류
- **이유:** 실험에서는 필요성이 확인됐지만 자동 의미 병합은 recall 위험이 크다. 초기에는 exact shadow grouping만 도입한다. semantic reducer는 `duplicate_of` 제안만 만들고 원본을 보존하는 별도 실험으로 분리한다.

### S2. 강한 OS-level read containment 즉시 구현

- **판정:** 별도 spike
- **이유:** 현재 CLI가 auth를 읽는 주체와 model tool subprocess의 filesystem 권한을 분리할 수 있는지 증명되지 않았다. 이 플랜에서는 plain snapshot과 preflight로 우발적 scope drift를 줄이고 위험을 명시한다. 강한 containment는 tool broker/API 기반 runner 또는 검증된 container architecture가 필요하다.

## 리뷰 후 실행 우선순위

1. synthetic structured chunk fixture
2. versioned execution telemetry + privacy lock-down
3. added-line ownership observe mode
4. non-destructive duplicate shadow grouping
5. evidence-v1 prompt
6. plain snapshot 및 containment preflight
7. per-chunk context manifest
8. verification independence
9. adjudicated benchmark와 cohort rollout

## 최종 리뷰 체크

- [x] 기존 코드의 changed-line/commentable-line 차이를 반영함
- [x] oversized hunk ownership을 structured chunk로 해결함
- [x] destructive dedupe를 제거함
- [x] observe/enforce와 posting eligibility를 분리함
- [x] retry/verify/partial attempt를 데이터 모델에 반영함
- [x] raw diagnostic/context privacy를 범위에 포함함
- [x] snapshot을 보안 경계로 과장하지 않음
- [x] benchmark label leakage와 표본 한계를 반영함
- [x] 구현 전 필수 preflight와 stop condition을 명시함

**리뷰 결론:** 필수 수정이 플랜에 반영됐으므로 구현 착수 가능. 단, Task 3.2 containment preflight가 강한 격리를 증명하지 못하면 plain snapshot을 보안 기능으로 표시해서는 안 되며, scope/dedupe enforce는 adjudicated benchmark와 canary 기준 전까지 금지한다.
