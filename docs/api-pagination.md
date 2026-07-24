# Review list pagination

The review-facing list APIs are bounded and return a page envelope rather than a bare array:

- `GET /api/overview?limit=&cursor=&pr_id=`
- `GET /api/prs/{pr_id}/runs?limit=&cursor=`
- `GET /api/runs/{run_id}/findings?limit=&cursor=`

```json
{
  "items": [],
  "pagination": {
    "limit": 25,
    "has_more": false,
    "next_cursor": null
  }
}
```

Findings additionally return an authoritative summary for all findings in the cursor snapshot:

```json
{
  "summary": {
    "total_count": 0,
    "status_counts": {},
    "postable_count": 0
  }
}
```

## Bounds

| Endpoint | Default | Maximum |
|---|---:|---:|
| Overview | 25 | 100 |
| PR run history | 25 | 100 |
| Run findings | 50 | 100 |

Each item query uses an ordering index and stops after `limit + 1` rows to determine `has_more`; query-plan tests reject temporary sorting. Findings use a trigger-maintained per-run high-water/count row that captures membership and summary atomically in O(1). The first-page summary is carried in the signed continuation cursor so later pages do not rescan the run. Summaries contain counts only and never expand the response with unloaded finding content.

## Cursor contract

`next_cursor` is opaque. Clients must not decode, modify, persist indefinitely, or derive ordering from it. It contains a version, resource and parent binding, an ID high-water mark, and the exclusive continuation position; the payload is protected with HMAC-SHA256. It is authenticated, not encrypted, and a findings position can contain a file path, so cursor-bearing URLs must remain inside the authenticated management boundary and be redacted from public logs.

A cursor is rejected with HTTP 400 when it is malformed, modified, too large, from another endpoint, or bound to another PR/run. A cursor cannot be combined with overview `pr_id` direct lookup. Limits outside the documented range return HTTP 422.

The first page fixes a high-water ID. Rows inserted after that point are excluded from continuation pages. This is a membership snapshot, not a historical MVCC snapshot: mutable item fields such as job status may reflect current state when a page is read. The findings count summary is fixed with the first page and carried in the signed cursor; after a triage write the UI rebuilds the loaded window from page one.

Ordering is stable and exclusive:

- overview: immutable `overview_sort_at DESC, pull_request.id DESC`;
- PR runs: `review_run.id DESC`;
- findings: severity, consensus, confidence, file, then finding ID.

## Recovery

Cursor signatures use `ALMIGHTY_CURSOR_HMAC_SECRET`, then the admin token, then a process-local development key. Secret rotation or restarting a process that uses the development key invalidates old cursors. Clients should discard the old window and request the first page again after a cursor 400 response.

`GET /api/overview?pr_id={id}&limit=1` supports direct `/reviews/{pr_id}` navigation when the PR is outside the currently loaded overview pages, including closed or merged PRs linked from operational failure history. Normal overview pagination remains open-PR-only. Direct lookup does not accept a cursor.
