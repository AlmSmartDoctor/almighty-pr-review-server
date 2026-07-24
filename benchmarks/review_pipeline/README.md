# Review-pipeline benchmark artifacts

This directory defines the Slice 1C offline schema and local tooling foundation. The
local collector, physical-separation linter, paired blind fixture runner, scorer, and
attestation binder are implemented. Remote collection and live model execution remain
unimplemented/unauthorized, and no checked-in artifact unlocks rollout policy.

## Artifact boundary

| Artifact | Location | Git policy | Model-visible |
| --- | --- | --- | --- |
| Manifest | approved public/synthetic bundle | reviewed source | yes |
| Adjudication answer | `private/answers/` | ignored, 0700 workspace / 0600 files | no |
| Predictions and run results | `private/predictions/`, `private/runs/` | ignored, 0700 workspace / 0600 files | no |
| Aggregate report | explicitly approved sanitized output | reviewed only after sanitization | no |

`private/` and `results/` are intentionally ignored. Operators must use an explicit
TTL and deletion procedure for every private workspace; raw model stdout/stderr and
full rationales must never be stored. The designated benchmark access owner is
responsible for access approval, retention deletion, withdrawal/correction requests,
and a re-identification review before publishing any aggregate.

## Provenance and privacy

A manifest is model-visible and therefore contains only public or synthetic input.
It requires an immutable source revision, HTTPS source URL, SPDX license,
redistribution permission, content/patch hashes, an approved provenance state, and a
small/medium/large PR stratum. Its proprietary, Jira, database, and private-context
flags are constants set to `false`. Labels, known-clean ranges, expected defects, and
adjudication do not have manifest fields and belong only to the separate private
adjudication answer.

Adjudicator records use only a non-reidentifying `adj-…` pseudonymous ID and an
ISO date (no timestamp). Do not keep a mapping to a person in this repository or in
the benchmark workspace.

## Canonical claim contract

All schemas fix `claim_normalization_version` to `claim-normalization-v1` and the
SHA-256 tokenizer configuration hash to
`9787d268171dfc88884d9961c1ae8608b111eda1eda892656a4db17622937458`.
The hashed canonical UTF-8 JSON configuration is:

```json
{"allowed_punctuation":["-"],"case":"casefold","normalization":"NFKC","sort":"lexicographic","stop_tokens":[],"whitespace":"collapse"}
```

The future scorer must apply Unicode NFKC, casefold, line-ending/whitespace collapse,
allowed-punctuation tokenization, configured stop-token removal, and lexicographic
stable token sorting. A prediction matches a rubric only when its normalized token
sequence exactly equals one allowed sequence. Fuzzy matching, subsets, and similarity
thresholds are prohibited. A normalization version or tokenizer-hash mismatch makes
the benchmark invalid.

The schemas define stable IDs, allowed location ranges, accepted categories, exact
rubric token sequences, production duplicate claim keys/emission order, private
prediction resolutions, versioned ownership/chunker oracle identity, known-clean
ranges, and the two explicit pair labels:
`same_issue_duplicate` and `distinct_issue_hard_negative`. The scorer validates cross-file joins, primary repetition and artifact commitments,
recomputes scope/reassignment/posting ownership, and writes only stable-ID private audit
rows plus a sanitized aggregate report. Every answer requires two distinct
independent adjudicators; unanimous gold answers require two accepting verdicts,
and a disagreement requires a hash-committed resolver record. Unresolved or
single-adjudicator material cannot produce enforceable evidence.

Enforcement still requires a clean final candidate and an operator-pinned canonical
report/identity exact match. Immediately before the review vendor call, production
also derives the actual runtime candidate identity from the selected vendor/model/
effort, harness configuration hash, protocol/chunker versions, chunk budget, adapter
configuration hash, probed CLI version, and event-schema version. All fields must
match the report. Missing or mismatched runtime identity—and multi-vendor runs while
the report describes only one candidate arm—remain `observe`.

## Offline commands

```bash
python scripts/review-benchmark-collect.py --help
python scripts/review-benchmark-lint.py --help
python scripts/review-benchmark-run.py --help
python scripts/review-benchmark-score.py --help
```

These commands never authorize remote download or live model execution. Private inputs,
answers, predictions, run artifacts, and score audits remain under ignored 0700/0600
workspaces with explicit operator TTL/deletion.
