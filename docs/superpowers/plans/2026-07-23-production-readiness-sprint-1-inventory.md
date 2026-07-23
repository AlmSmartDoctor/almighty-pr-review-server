# Production Readiness Sprint 1 — Change Inventory

- **Captured:** 2026-07-23
- **Scope:** Task 0.1 B0/B1 classification universe
- **Rule:** 파일 내용이나 secret 값은 기록하지 않고 path/status/classification만 기록한다.

## 1. Immutable B0

- Command: `git status --porcelain=v1 -z --untracked-files=all`
- Raw snapshot: `/tmp/almighty-sprint1-b0.status` (local evidence; commit 대상 아님)
- SHA-256: `e7d2a77ea6726057ba1769d98830e04b13e3d12976e40f81aa88f460e4007b7c`
- HEAD: `e3447e7daea7339ddb58d7525a9658c9900c7ce0`
- Branch/upstream: `main` / `origin/main` (ahead 0, behind 0)
- Entries: 122 = tracked changed 74 + untracked all-path 48
- Collapsed `git status --short` count: 113 entries; all-path count와 서로 대체하지 않는다.

## 2. Approved Task-owned transition

- Add `.playwright-mcp/` to `.gitignore` after confirming the seven B0 files are generated Playwright MCP console/page artifacts.
- Create this inventory document.
- Expected B0 → B1 status transition:
  - remove seven `.playwright-mcp/*` untracked rows from visible status via the approved ignore rule;
  - add this inventory document as one untracked docs row;
  - preserve every other status/path pair exactly.
- `.gitignore` was already modified in B0; this task added only the `.playwright-mcp/` rule.
- `.gitignore` SHA-256 before Task 0.1 addition: `bd7464d06ffcba24d3edb57a6b7c20a4ee858eca3b9c527ded66f9cbbd374c11`
- `.gitignore` SHA-256 after Task 0.1 addition: `5a31a1505b9ec37784124caf0082b9cac5930dc6fb0fe35371c856292071e4c5`

## 3. Classification summary (B0 ∪ expected B1)

- Classification universe: 123 unique paths
- intended source: 69
- test: 35
- docs: 12
- generated/local artifact: 7
- unknown: 0

Unknown count is zero. Any later path outside this table stops commit planning unless a subsequently approved task names the path before its first write, rechecks the latest stable raw status SHA, and records the exact expected transition plus updated ownership manifest. The historical B0∪B1 universe itself is not rewritten.

## 4. Ignore roots/rules and sensitive denylist

### Existing ignore roots/rules observed without reading secret contents

- `.env` → `.gitignore` `.env` rule
- `almighty.db` → `.gitignore` `*.db` rule
- `.raw/`, `.clones/`, `.venv/`, `node_modules/`, `web/node_modules/`, `web/dist/` → repository `.gitignore` rules
- `.safe-db-locks/`, `.safe-db-audit.jsonl` → repository `.gitignore` rules; currently absent
- `.playwright-mcp/` → approved Task 0.1 generated-artifact rule to add
- No configured `core.excludesfile` was reported.

### Sensitive denylist presence (path existence only)

| Path | State | Commit candidate |
|---|---|---|
| `.env` | present | no |
| `almighty.db` | present | no |
| `.raw/` | present | no |
| `.clones/` | present | no |
| `.safe-db-locks/` | absent | no |
| `.safe-db-audit.jsonl` | absent | no |
| `benchmarks/review_pipeline/private/` | absent | no |
| `benchmarks/review_pipeline/results/` | absent | no |

## 5. Per-path classification

| Status | Path | Classification | First seen |
|---|---|---|---|
| ` M` | `.gitignore` | intended source | B0 |
| ` M` | `README.md` | docs | B0 |
| ` M` | `docs/context-provider-contract.md` | docs | B0 |
| ` M` | `docs/superpowers/specs/2026-07-13-subproject-c-feedback-learning.md` | docs | B0 |
| ` M` | `docs/vendor-cli-contract.md` | docs | B0 |
| ` M` | `harness/default/review-system-prompt.md` | intended source | B0 |
| ` M` | `server/api.py` | intended source | B0 |
| ` M` | `server/config.py` | intended source | B0 |
| ` M` | `server/context/base.py` | intended source | B0 |
| ` M` | `server/context/composite.py` | intended source | B0 |
| ` M` | `server/context/db_schema_source.py` | intended source | B0 |
| ` M` | `server/context/jira_provider.py` | intended source | B0 |
| ` M` | `server/context/registry.py` | intended source | B0 |
| ` M` | `server/context/source_provider.py` | intended source | B0 |
| ` M` | `server/context/static_provider.py` | intended source | B0 |
| ` M` | `server/db.py` | intended source | B0 |
| ` M` | `server/github/gh.py` | intended source | B0 |
| ` M` | `server/models.py` | intended source | B0 |
| ` M` | `server/pipeline.py` | intended source | B0 |
| ` M` | `server/poller.py` | intended source | B0 |
| ` M` | `server/repos/finding_repo.py` | intended source | B0 |
| ` M` | `server/repos/job_repo.py` | intended source | B0 |
| ` M` | `server/repos/posted_repo.py` | intended source | B0 |
| ` M` | `server/repos/pr_repo.py` | intended source | B0 |
| ` M` | `server/repos/repo_repo.py` | intended source | B0 |
| ` M` | `server/repos/review_repo.py` | intended source | B0 |
| ` M` | `server/repos/settings_repo.py` | intended source | B0 |
| ` M` | `server/repos/wiki_repo.py` | intended source | B0 |
| ` M` | `server/review/diff_filter.py` | intended source | B0 |
| ` M` | `server/review/findings_schema.py` | intended source | B0 |
| ` M` | `server/review/gh_deps.py` | intended source | B0 |
| ` M` | `server/review/prescreen.py` | intended source | B0 |
| ` M` | `server/review/vendors.py` | intended source | B0 |
| ` M` | `server/review/verify.py` | intended source | B0 |
| ` M` | `server/review/worktree.py` | intended source | B0 |
| ` M` | `server/wiki.py` | intended source | B0 |
| ` M` | `server/worker.py` | intended source | B0 |
| ` M` | `tests/test_api.py` | test | B0 |
| ` M` | `tests/test_context.py` | test | B0 |
| ` M` | `tests/test_db.py` | test | B0 |
| ` M` | `tests/test_diff_filter.py` | test | B0 |
| ` M` | `tests/test_findings_schema.py` | test | B0 |
| ` M` | `tests/test_gh.py` | test | B0 |
| ` M` | `tests/test_gh_deps.py` | test | B0 |
| ` M` | `tests/test_harness.py` | test | B0 |
| ` M` | `tests/test_health.py` | test | B0 |
| ` M` | `tests/test_job_repo.py` | test | B0 |
| ` M` | `tests/test_overview.py` | test | B0 |
| ` M` | `tests/test_pipeline.py` | test | B0 |
| ` M` | `tests/test_poller.py` | test | B0 |
| ` M` | `tests/test_post.py` | test | B0 |
| ` M` | `tests/test_post_slack.py` | test | B0 |
| ` M` | `tests/test_prescreen.py` | test | B0 |
| ` M` | `tests/test_review_trigger.py` | test | B0 |
| ` M` | `tests/test_vendors.py` | test | B0 |
| ` M` | `tests/test_verify.py` | test | B0 |
| ` M` | `tests/test_wiki.py` | test | B0 |
| ` M` | `tests/test_worker.py` | test | B0 |
| ` M` | `web/src/App.test.tsx` | test | B0 |
| ` M` | `web/src/App.tsx` | intended source | B0 |
| ` M` | `web/src/api.ts` | intended source | B0 |
| ` M` | `web/src/components/env-status.tsx` | intended source | B0 |
| ` M` | `web/src/components/page-head.tsx` | intended source | B0 |
| ` M` | `web/src/components/repo-tabs.tsx` | intended source | B0 |
| ` M` | `web/src/components/status-line.tsx` | intended source | B0 |
| ` M` | `web/src/index.css` | intended source | B0 |
| ` M` | `web/src/sections/HarnessSection.tsx` | intended source | B0 |
| ` M` | `web/src/sections/LearnSection.test.tsx` | test | B0 |
| ` M` | `web/src/sections/LearnSection.tsx` | intended source | B0 |
| ` M` | `web/src/sections/ReviewSection.test.tsx` | test | B0 |
| ` M` | `web/src/sections/ReviewSection.tsx` | intended source | B0 |
| ` M` | `web/src/sections/SettingsSection.test.tsx` | test | B0 |
| ` M` | `web/src/sections/SettingsSection.tsx` | intended source | B0 |
| ` M` | `web/src/sections/WikiSection.tsx` | intended source | B0 |
| `??` | `.playwright-mcp/console-2026-07-22T09-47-17-688Z.log` | generated/local artifact | B0 |
| `??` | `.playwright-mcp/console-2026-07-22T09-47-50-100Z.log` | generated/local artifact | B0 |
| `??` | `.playwright-mcp/console-2026-07-22T09-48-00-494Z.log` | generated/local artifact | B0 |
| `??` | `.playwright-mcp/page-2026-07-22T09-47-18-010Z.yml` | generated/local artifact | B0 |
| `??` | `.playwright-mcp/page-2026-07-22T09-47-39-436Z.yml` | generated/local artifact | B0 |
| `??` | `.playwright-mcp/page-2026-07-22T09-47-50-284Z.yml` | generated/local artifact | B0 |
| `??` | `.playwright-mcp/page-2026-07-22T09-48-00-592Z.yml` | generated/local artifact | B0 |
| `??` | `benchmarks/review_pipeline/fixtures/synthetic_scope_dedupe.json` | intended source | B0 |
| `??` | `docs/review-pipeline-rollout.md` | docs | B0 |
| `??` | `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup-review.md` | docs | B0 |
| `??` | `docs/superpowers/plans/2026-07-22-review-scope-observability-and-dedup.md` | docs | B0 |
| `??` | `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1-review.md` | docs | B0 |
| `??` | `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1.md` | docs | B0 |
| `??` | `docs/superpowers/specs/2026-07-20-live-mssql-introspection.md` | docs | B0 |
| `??` | `scripts/review-cli-telemetry-preflight.py` | intended source | B0 |
| `??` | `scripts/review-pipeline-benchmark.py` | intended source | B0 |
| `??` | `scripts/review-read-containment-preflight.py` | intended source | B0 |
| `??` | `server/context/current_pr_reviews_source.py` | intended source | B0 |
| `??` | `server/context/live_mssql_source.py` | intended source | B0 |
| `??` | `server/context/review_rules_source.py` | intended source | B0 |
| `??` | `server/context/status.py` | intended source | B0 |
| `??` | `server/http_security.py` | intended source | B0 |
| `??` | `server/repos/post_operation_repo.py` | intended source | B0 |
| `??` | `server/repos/process_repo.py` | intended source | B0 |
| `??` | `server/repos/review_rule_repo.py` | intended source | B0 |
| `??` | `server/retention.py` | intended source | B0 |
| `??` | `server/review/finding_policy.py` | intended source | B0 |
| `??` | `server/review/pipeline_contracts.py` | intended source | B0 |
| `??` | `server/review/rollout.py` | intended source | B0 |
| `??` | `server/review/snapshot.py` | intended source | B0 |
| `??` | `server/review/vendor_telemetry.py` | intended source | B0 |
| `??` | `server/routes/__init__.py` | intended source | B0 |
| `??` | `server/routes/harness.py` | intended source | B0 |
| `??` | `server/safe_db/ORIGIN.md` | docs | B0 |
| `??` | `server/safe_db/__init__.py` | intended source | B0 |
| `??` | `server/safe_db/sql_gateway.py` | intended source | B0 |
| `??` | `tests/test_config.py` | test | B0 |
| `??` | `tests/test_live_mssql_source.py` | test | B0 |
| `??` | `tests/test_process_lease.py` | test | B0 |
| `??` | `tests/test_retention.py` | test | B0 |
| `??` | `tests/test_review_benchmark.py` | test | B0 |
| `??` | `tests/test_review_rules.py` | test | B0 |
| `??` | `tests/test_review_snapshot.py` | test | B0 |
| `??` | `tests/test_security.py` | test | B0 |
| `??` | `tests/test_vendor_telemetry.py` | test | B0 |
| `??` | `web/src/components/loading-state.tsx` | intended source | B0 |
| `??` | `web/src/components/repo-tabs.test.tsx` | test | B0 |
| `??` | `web/src/components/route-error-boundary.tsx` | intended source | B0 |
| `??` | `docs/superpowers/plans/2026-07-23-production-readiness-sprint-1-inventory.md` | docs | B1 |

## 6. B1/B2 stability evidence

- B1 SHA-256: `b4c1b2e963c60809b6f7e12ddde78cfae1f8955c9151ee3792c6fe6e7ec99d0b`
- B1 entries: 116 = tracked changed 74 + untracked all-path 42
- B0 → B1 expected transition check: `passed`
  - removed from visible status exactly seven approved `.playwright-mcp/*` generated rows;
  - added exactly this inventory document;
  - every other status/path pair remained unchanged.
- B2 SHA-256: `b4c1b2e963c60809b6f7e12ddde78cfae1f8955c9151ee3792c6fe6e7ec99d0b`
- B1 == B2: `passed` (116 entries, raw bytes identical)
- B3 SHA-256 after final Task 0.1 evidence write: `b4c1b2e963c60809b6f7e12ddde78cfae1f8955c9151ee3792c6fe6e7ec99d0b`
- B2 == B3: `passed` (116 entries, raw bytes identical)
- B3 is the latest documented stable raw-status baseline for subsequent approved task transitions.
- Final inventory file SHA-256 is reported in the Task 0.1 handoff because a file cannot embed its own stable digest.

## 7. Subsequent approved Task 0.3 transition

- Pre-write baseline: B3 SHA-256 `b4c1b2e963c60809b6f7e12ddde78cfae1f8955c9151ee3792c6fe6e7ec99d0b` (116 entries).
- Approved future path: `server/review/harness.py` → C5/G1 whole-path ownership.
- Post-write raw status SHA-256: `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Exact transition: add only ` M server/review/harness.py`; no path removal or other status/path transition.
- Task 0.1 historical B0∪B1 classification remains unchanged; current commit-planning ownership expands to 117 paths.

## 8. Subsequent approved Task 0.4 stability

- Pre-write baseline: Task 0.3 SHA-256 `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Post-write raw status SHA-256: `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Exact transition: no status/path transition; Task 0.4 modified only already-approved C5/G1 split/whole-path ownership.
- Task 0.1 historical B0∪B1 classification and the 117-path current ownership universe remain unchanged.

## 9. Subsequent approved M1.1 stability

- Pre-write baseline: Task 0.4 SHA-256 `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Post-write raw status SHA-256: `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Exact transition: no status/path transition; M1.1 modified only already-approved C5/G1 whole paths.
- Task 0.1 historical B0∪B1 classification and the 117-path current ownership universe remain unchanged.

## 10. Subsequent approved Task 0.5 stability

- Pre-write baseline: M1.1 SHA-256 `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Post-write raw status SHA-256: `fbb0605315c280751952eec68e54a1def95b3f0d797ad405bad9ee74acf9d92c` (117 entries).
- Exact transition: no status/path transition; reviewer findings modified only previously classified/approved source, test, and docs paths.
- Task 0.1 historical B0∪B1 classification and the 117-path current ownership universe remain unchanged.
