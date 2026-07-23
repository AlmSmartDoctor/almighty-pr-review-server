# Review — Production Readiness Sprint 1 Plan

- **Plan:** `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1.md`
- **Date:** 2026-07-23
- **Method:** fresh-context reviewer 3개 병렬 리뷰 + material fix 후 targeted follow-up reviewer 1개
- **Scope:** correctness/feasibility, security/privacy/external effects, operability/sizing/metric semantics
- **Project files modified by reviewers:** 없음

## 1. 판정 이력

| Round | 판정 | 요약 |
|---|---|---|
| Initial parallel review | **NO-GO** | benchmark 채점 계약 부재, retry identity 충돌, 과거 policy requested 상태 미보존, live duplicate truth 부재, shared credential runtime, 불완전한 GitHub mutation accounting, benchmark 외부 호출/SSRF gate 부재, 모호한 canary threshold, 과대 sprint |
| Targeted follow-up | **NO-GO** | canonical issue/pair scoring, retry pre-call identity pinning, control cohort 정의, attestation candidate binding, complete GitHub pagination, DNS rebinding/proxy 방어가 추가로 필요 |
| Parent disposition after follow-up | **Required changes incorporated** | 아래 모든 blocker/high finding을 수정본 계획에 반영. review cap에 따라 추가 reviewer round는 실행하지 않음 |

따라서 마지막 독립 reviewer의 당시 판정은 NO-GO였고, 그 후 계획 문서가 다시 수정됐다. 수정본은 **한 번에 전체 실행하는 계획이 아니라 Slice 1A–1D로 분리된 조건부 실행 계획**이다. 구현은 Slice 1A 범위와 외부-effect gate를 사용자가 승인한 뒤 시작한다.

## 2. Initial review disposition

| Finding | Severity | Disposition |
|---|---|---|
| benchmark run artifact에 prediction matching 정보가 없어 issue/duplicate 채점 불가 | Blocker | `prediction.schema.json`과 private prediction artifact 추가. case/prediction ID, location/category/claim key, scope/posting, duplicate relation을 answer와 분리 |
| invalid model로 partial을 만든 뒤 정상 model로 retry하면 envelope identity와 충돌 | High | model identity를 바꾸지 않는 fail-once fault injection으로 변경. identity 변경은 새 run만 허용 |
| requested policy mode가 run snapshot에 없음 | High | `requested_mode`, effective mode, reason, selection source, 2차원 cohort를 저장하도록 수정 |
| retry가 현재 repo/env policy를 재계산 | High | 기존 run snapshot policy만 사용하고 설정 변경 시 external call 전 `new_full_run_required`로 거부 |
| benchmark aggregate report schema/canonical hash 부재 | High | `benchmark-report.schema.json`과 canonical UTF-8 JSON hash 규칙 추가 |
| API/UI filter 계약 불일치 | High | summary/runs 모두 repo/date/cohort/vendor/status filter를 공유하도록 통일 |
| would-reject 중 posted 비율은 구조적으로 0 | Medium | approved/edited/dismissed human adjudication 비율로 변경 |
| live duplicate precision truth 저장소가 없음 | Blocker | live UI에서는 group count만 관측하고 precision으로 표현하지 않음. duplicate precision은 benchmark-only로 제한 |
| shared runtime에 양 vendor credential materialization | High | review/retry/verify/prescreen/Wiki 전 호출부에 vendor별 runtime ownership과 cleanup 적용 계획 추가 |
| provider event key 이름 자체가 secret/path를 포함할 수 있음 | High | exact-version allowlist key만 출력, unknown 이름은 숨기고 count만 표시, key/signature/report cap 추가 |
| no-write E2E가 comment count만 비교 | High | PR review, inline, conversation comment, head ref를 각각 비교하도록 확대 |
| post preview가 create/update/fallback/downstream side effect를 포괄하지 않음 | High | exact mutation manifest, update/fallback 기본 거부, inline 기본 off/marker, GitHub relay/action 승인 추가 |
| benchmark collector/model runner 외부 호출 승인 gate 부재 | High | collect와 paid model run에 각각 source/cost/data/budget 승인 gate 추가 |
| collector SSRF/download guard 부재 | High | HTTPS trusted origin, URL credential/private IP/redirect 차단, bounded download/safe extraction 추가 |
| duplicate denominator 0도 통과 가능 | High | true pair/hard-negative/predicted pair 각각 30개 최소, 분모 0 locked, Wilson denominator 기록 |
| canary stop threshold가 실행 불가능하게 모호 | High | 14일 current/baseline, window별 20 runs, 5%p/2x, telemetry 95%, cost 1.10, hard stop/SLO 정의 |
| operations external exposure acceptance 부재 | Medium | management bearer auth 테스트와 explicit external-mode token/TLS/origin startup/preflight gate 추가 |
| adjudicator governance/retention 부재 | Medium | pseudonym 최소화, 0700/0600, TTL, 접근/삭제/철회/재식별 검토 추가 |
| sprint-done/live/rollout unlock 경계 상충 | Blocker | code/tooling, live rehearsal, rollout unlock 체크리스트를 독립 분리 |
| 전체 범위가 normal solo sprint보다 큼 | High | 20–30 engineer-days로 추정하고 Slice 1A–1D로 분할 |

## 3. Follow-up review disposition

| Finding | Severity | Disposition after follow-up |
|---|---|---|
| answer schema에 canonical issue mapping과 explicit duplicate/nonduplicate pair label이 없음 | Blocker | allowed location/category/claim rubric, explicit issue-pair labels, ambiguous resolver, TP/FP/FN 및 duplicate pair canonical scoring 규칙 추가 |
| retry identity mismatch를 vendor 호출 후에야 알 수 있음 | High | stored envelope/run identity를 pre-invocation에 로드하고 mismatch 시 runner call 0으로 새 run 요구 |
| control cohort 의미가 없음 | High | 별도 control cohort를 제거. 같은 repo + 같은 2차원 policy cohort의 직전 baseline window로 비교 정의 |
| attestation이 평가한 candidate identity와 enforce candidate를 묶지 않음 | High | clean commit SHA, model/effort, prompt/protocol/chunker/adapter/CLI/event schema, validity deadline을 report에 넣고 exact match 요구 |
| GitHub mutation snapshot이 pagination cap에서 누락 가능 | High | complete pagination/total 검증을 요구하고 cap/truncation 도달 시 preflight fail-closed |
| collector의 DNS precheck가 rebinding/proxy를 막지 못함 | High | proxy env 비활성, DNS-pinned transport, Host/SNI/TLS 유지, connected peer 검증, rebinding fixture 추가 |

## 4. Review로 고정된 핵심 경계

1. **Scope/dedupe enforce는 계속 잠김.** valid benchmark report, denominator, candidate identity, repo canary, kill switch가 모두 필요하다.
2. **Synthetic sandbox만 live 대상.** strong read containment가 증명되지 않았으므로 민감 레포 canary는 금지한다.
3. **외부 effect는 단계별 승인.** paid telemetry, source collection, paired model benchmark, GitHub post, sandbox push/webhook delivery를 각각 확인한다.
4. **운영 지표와 benchmark truth를 혼동하지 않는다.** live duplicate group count는 precision이 아니다.
5. **`not_run`은 pass가 아니다.** code/tooling 완료, live rehearsal, rollout unlock은 독립 판정한다.
6. **전체를 한 번에 실행하지 않는다.** Slice 1A 완료·리뷰 후 1B, 1C, 1D 순으로 승인한다.

## 5. 잔여 위험

- 마지막 follow-up 이후 수정본에 대한 두 번째 독립 follow-up은 수행하지 않았다.
- strong path-level read containment는 여전히 별도 spike 대상이다.
- Claude successful telemetry는 계정/모델 접근 상태에 따라 계속 `not_run` 또는 `failed`일 수 있다.
- 두 명의 독립 adjudicator와 충분한 duplicate denominator를 확보하지 못하면 rollout은 계속 locked다.
- GitHub actual webhook delivery는 외부 sandbox 설정과 사용자가 승인한 push에 의존한다.

## 6. 최종 권고

- **Plan adoption:** Revised after NO-GO — pending acceptance (독립 GO 아님)
- **전체 범위 일괄 실행:** NO-GO
- **다음 승인 단위:** **Slice 1A — 현재 diff inventory/안정화 + telemetry 안전 계약**
- **Commit/push/live probe:** 별도 사용자 승인 전 금지

## 7. Round 1 parent-orchestrated review trace

- **Reviewed plan SHA-256:** `5ec3ecc26c771a4664f0afe0d25782f227aee6dd3a0f61f5fa3d18117e3ea7c9`
- **Scope:** `2026-07-23-production-readiness-sprint-1.md`와 이 review artifact만. 코드·테스트·runtime 설정은 수정하지 않았다.
- **Baseline accounting:** 최초 플랜의 untracked 37개와 Round 1에서 추가된 plan/review 문서 2개를 구분해 현재 39개로 기록하도록 수정했다.

| Round 1 finding | Severity | Accepted plan fix |
|---|---|---|
| Slice 1B retry assertion이 Slice 1D snapshot 구현보다 앞서 실행 불가 | Blocker | 당시 PolicyDecision/schema/run snapshot/retry basis를 Slice 1A Task 0.6으로 이동했고, Round 2에서 Task 0.4로 재번호화해 M2 전에 완료하도록 고정 |
| Slice 1C attestation이 Slice 1D 변경 전 candidate에 묶여 invalid | High | 1C는 tooling만 완료하고 final paired benchmark/attestation은 1D clean candidate 후 생성 |
| retry identity가 model/policy 수준이라 prompt/harness/CLI/diff drift를 놓침 | Blocker | vendor/model/effort/protocol/chunker/policy, prompt, harness/tool/sandbox, adapter, CLI/event schema, diff/context hash exact pre-call identity로 확대; mismatch는 runner call 0과 `new_full_run_required` |
| benchmark claim/scope/posting/duplicate scoring과 confidence/cost denominator가 비결정적 | Blocker | versioned normalization, prediction oracle/ownership, candidate pair universe, metric별 numerator/denominator/Wilson fields, NTCU paired cost formula로 고정 |
| Claude expected schema와 production activation이 순환하고 real tool evidence가 없음 | High | expected schema fixture는 preflight-only, production은 attestation 전 legacy; live pass에 `tool_calls >= 1` 추가 |
| E2E live safety, GH credential trust, replay isolation이 부족 | Blocker | clean VM/dedicated account 요구, isolated `GH_CONFIG_DIR`/ambient auth strip/operator allowlist/read-only attestation, replay profile side-effect assertions, stopped-consumer delivery와 별도 job execution 승인 추가 |
| remote collector trust boundary가 실행자에게 열려 있음 | High | operator-owned exact origin allowlist와 local approved immutable bundle MVP를 명시; remote downloader는 deferred |
| Operations cursor/baseline semantics 불명확 | Medium | `(started_at,id)` cursor와 legacy NULL ordering, `insufficient_baseline` 상태를 명시 |

**Round 1 verdict:** 수정 전 전체 계획은 **NO-GO**였다. 수정 후에도 독립 reviewer의 GO를 주장하지 않는다. **Task 0.1은 GO**이며, **Slice 1A tooling은 수정된 순서와 scope를 승인한 뒤에만 조건부 실행 가능**하다. live attestation, E2E write, benchmark collection, commit/push, rollout unlock은 각각 별도 승인·evidence gate를 유지한다.

## 8. Round 2 parent-orchestrated review trace

- **Pre-edit reviewed plan SHA-256:** `5ec3ecc26c771a4664f0afe0d25782f227aee6dd3a0f61f5fa3d18117e3ea7c9`
- **Scope:** 동일한 두 markdown 문서만 수정했다. 코드, 테스트, runtime 설정, stage/commit/PR은 변경하지 않았다.

| Round 2 finding | Severity | Accepted plan disposition |
|---|---|---|
| Slice 1A integrated review/commit가 PolicyDecision/retry 및 M1.1 구현보다 앞섬 | Blocker | Task 0.4 → M1.1 → Task 0.5 integrated offline gate/fresh review/separate commit approval 순으로 재정렬 |
| retry identity coverage가 일부 field만 테스트 | Blocker | model/effort/prompt/harness-tool-sandbox/adapter/CLI-event-schema/protocol-chunker-policy/diff-context-chunk 각각 mismatch·absence pre-call runner=0 요구 |
| quality scoring이 repetitions을 합쳐 claim/duplicate/scope truth가 흐림 | Blocker | preselected primary candidate만 quality gate, exact versioned token-sequence equality, fixed ownership+chunker oracle, exact pair/FN universe로 고정 |
| duplicate CI/cost/attestation identity가 rollout gate에 불충분 | High | duplicate precision Wilson LB ≥0.88, N=0 lock/shortfall, all invoked terminal status cost, missing telemetry lock, vendor/corpus/adjudication/primary-selection/schedule/scorer identity 추가 |
| Slice 1B no-write 승인과 credential boundary가 불완전 | Blocker | exact external-effect manifest, read/write credential 분리, attestation/fingerprint/permission/expiry/allowlist binding, zero-residue 요구 |
| post idempotency 및 webhook ingress proof가 실제 side effect를 충분히 계수하지 않음 | High | operation identity/second DB-response/transport counter, fake-only crash/adoption/concurrency, 1D dedicated ingress 후 actual delivery, trusted-proxy/public probe 규칙 추가 |
| inventory/cursor/policy terminology이 재현성에 부족 | Medium | porcelain `-z` SHA before/after, ignored root/rule+denylist 분리, requested/effective terminology, as_of/filter-bound exclusive cursor, initial `insufficient_baseline` 명시 |

**Round 2 verdict:** 수정 전 계획은 **NO-GO**였다. 이 disposition은 parent-approved 문서 수정이며, 수정 후 독립 reviewer의 GO 또는 모든 blocker/high 해결을 주장하지 않는다. 다음 실행 권고는 여전히 **Task 0.1만**이며, Slice 1A의 나머지는 Task 0.4/M1.1/Task 0.5 gate와 사용자 승인 뒤 조건부로 진행한다. live/commit/push/rollout unlock은 각각 별도 승인과 evidence를 요구한다.

## 9. Round 3 parent-orchestrated review trace — cap reached

- **Round:** 3 / 3 (maximum cap reached; this is the final writer pass)
- **Pre-edit reviewed plan SHA-256:** `6c299d342a8362166a72fe124982534f422c00f9b005bdcdf43b10fd68497314`
- **Scope:** 동일한 plan/review markdown 두 파일만 수정했다. 코드, 테스트, runtime 설정, stage/commit/PR은 변경하지 않았다.
- **Independent-review status:** 아래 disposition 전의 독립 판단은 전체 계획 **NO-GO**, Slice별 조건부 진행 가능이었다. 이번 수정 후 독립 GO를 주장하지 않으며, cap에 따라 추가 독립 reviewer round는 따르지 않는다.

| Round 3 finding | Severity | Parent disposition in final writer pass |
|---|---|---|
| inventory baseline이 task-owned write와 concurrent mutation을 구분하지 못하고 collapsed/all-path count가 혼재 | Blocker | immutable B0 → intended-write B1 expected-transition → read-only B2 stability window와 B0∪B1 classification universe, collapsed 39/all-path 48을 명시 |
| Task 0.3/0.4 ownership과 Task 0.5 precondition이 겹치거나 누락 | High | all `prepare_runtime()` caller credential isolation은 0.3 coordinated sub-slice, retry envelope/adapter inputs는 0.4 coordinated sub-slice로 고정; 0.5는 0.1–0.4+M1.1을 명시적으로 선행 요구 |
| Claude attestation이 production allowlist activation으로 오해될 수 있음 | High | M1.2는 sanitized evidence와 real `tool_calls >= 1`만 만들고 production은 legacy-disabled 유지; activation은 별도 tested/fresh-reviewed commit 및 별도 승인 |
| duplicate universe가 claim key equality로 좁아 paraphrase FN/hard-negative를 잃음 | Blocker | universe를 vendor/file/owner-line/category 모든 unordered pair로 확장하고 canonical claim equality는 proposal-positive만 판정; true paraphrase proposal-negative는 FN, hard-negative는 emitted universe pair로 고정 |
| non-invoked primary candidate가 quality denominator/cost에서 함께 빠질 수 있음 | Blocker | quality는 expected issue FN 및 invalid/locked로 fail-closed, cost만 non-invoked invocation 밖에 별도 보고 |
| attestation/run/UI identity set 및 Wilson/retry test 정의가 불완전 | High | clean commit부터 scorer/schema까지 전체 identity exact-match/snapshot/UI 표기, exact z의 unrounded two-sided 95% Wilson LB/shortfall, vendor 포함 모든 retry identity absence/mismatch runner=0 요구 |
| Slice 1B replay가 public ingress assertion을 포함하고 post idempotency가 preview 수준 | High | 1B는 offline/in-process/temp DB/no network/call-count-0 replay로 축소, trusted-proxy/HTTPS/public listener는 1D로 이동; post policy file/test scope와 server-enforced no-op replay payload을 명시 |

**Round 3 final disposition:** max review-round cap에 도달했다. 수정 전 독립 verdict는 **NO-GO / Slice별 conditional**이었고, 이번 final writer pass는 parent-synthesized fixes만 반영했다. 수정 후 독립 GO는 없으며, 다음 행동은 사용자 acceptance 후 Task 0.1의 B0 read-only capture부터다. live probe, E2E write, benchmark collection/run, commit/push, ingress exposure, rollout unlock은 이전과 같이 별도 승인·evidence gate를 유지한다.
