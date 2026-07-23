# Review scope/dedupe rollout

## Default behavior

Chunk ownership and exact duplicate shadow grouping run in `observe` mode by default. Each finding stores its source/owner chunk, scope status, posting eligibility, and duplicate group. The UI exposes the status; no exact duplicate is silently deleted.

Per-chunk context is selected as complete semantic blocks. Persistable chunk context is hash-bound to the diff chunk and reused for failed-chunk retry; `sensitive`/`manifest_only` payloads are prompt-only and force a full rerun instead of unsafe retry. API metadata contains only the block manifest, hashes, sizes, trust/sensitivity classes, and omission reasons—not per-chunk block text. Plain tracked-file snapshots omit `.git`, but they are defense in depth and **not OS-level read containment**.

Every new `review_run` snapshots the requested/effective scope and dedupe decisions, reason, selection source, two-axis cohort key, policy/config hashes, and optional benchmark attestation hash at creation. Run-history API responses label pre-migration or partial rows as `policy_snapshot.snapshot_status=unknown`; they never infer historical policy from current settings.

A failed-vendor retry reuses the saved policy decision and the original random prompt fence nonce. Before any vendor runner call it requires an exact execution identity match for vendor/model/effort, prompt and harness/tool/sandbox hashes, adapter name/version/config hash, CLI/event-schema versions, protocol/chunker/policy identity, filtered diff/context hashes, and every retry chunk hash. Missing or changed identity fails closed as `new_full_run_required` with zero vendor calls. A benchmark evidence digest may be bound with `ALMIGHTY_REVIEW_BENCHMARK_ATTESTATION_HASH=<sha256>`; the digest is non-secret and an invalid value prevents startup.

Verification stores `verify_independent` and `verify_evidence_status`. Only a distinct vendor may produce `confirmed`; same-vendor support is `supported_self`. Failed verification is visible as `degraded` and never blocks the review.

## Offline benchmark

```bash
.venv/bin/python scripts/review-pipeline-benchmark.py \
  --output /tmp/review-pipeline-benchmark.json
```

The checked-in fixture is synthetic and explicitly marked non-proprietary. This command does not invoke a model, does not read repository source, and does not expose expected labels to a model. The report includes scope/posting accuracy, exact duplicate pair precision/recall, sample size, and a rollout decision.

Default enforcement gates are:

- at least 100 adjudicated findings;
- scope and posting accuracy at least 99.5%;
- duplicate precision 100%;
- duplicate recall and issue-level recall at least 95%;
- issue-level precision at least 99.5% and 95% confidence lower bound at least 99%;
- small/medium/large PR strata plus at least 10 partial/timeout cases;
- cost regression ratio no greater than 1.10.

The small checked-in fixture is a regression smoke test and intentionally cannot satisfy the sample-size gate. External-model quality/cost runs must be a separate explicit opt-in, use scrubbed non-proprietary inputs, withhold labels until scoring, pin model/CLI/schema versions, and record token/tool-call/model-time telemetry.

## Canary and kill switches

Enforcement first requires an operator attestation that the adjudicated benchmark gate passed; without it the effective mode remains `observe` even if a stored setting says `enforce`:

```bash
ALMIGHTY_REVIEW_POLICY_ENFORCEMENT_UNLOCKED=1
```

Global enforcement also does not apply unless the repository is in the corresponding canary list:

```bash
ALMIGHTY_REVIEW_SCOPE_GUARD_MODE=enforce
ALMIGHTY_REVIEW_SCOPE_ENFORCE_REPOS=org/repo-a,org/repo-b
ALMIGHTY_REVIEW_DEDUPE_MODE=enforce
ALMIGHTY_REVIEW_DEDUPE_ENFORCE_REPOS=org/repo-a
```

A per-repository `review_scope_guard_mode=enforce` or `review_dedupe_mode=enforce` is itself an explicit canary selection. Emergency rollback is immediate and takes precedence over global and per-repository settings:

```bash
ALMIGHTY_REVIEW_SCOPE_KILL_SWITCH=1
ALMIGHTY_REVIEW_DEDUPE_KILL_SWITCH=1
```

After changing environment switches, restart the server process. Keep human adjudication and monitor false rejection, duplicate precision, recall, wall time, tokens, and tool calls before expanding the canary.
