import fcntl
import json
import time

import httpx

from server.context.base import ContextRequest
from server.context.live_mssql_source import (
    LiveMSSQLSchemaClient,
    live_schema_source,
    render_schema,
)
from server.safe_db.sql_gateway import (
    GatewayPolicy,
    GuardedSQLGatewayClient,
    guard_read_sql,
)


TOKEN = "t" * 32
COLUMN_NAMES = (
    "table_schema",
    "table_name",
    "column_name",
    "data_type",
    "max_length",
    "numeric_precision",
    "numeric_scale",
    "is_nullable",
    "ordinal_position",
)
COLUMNS = [{"name": name} for name in COLUMN_NAMES]
ROWS = [
    ["dbo", "users", "id", "bigint", None, 19, 0, "NO", 1],
    ["dbo", "users", "name", "nvarchar", 100, None, None, "YES", 2],
    ["dbo", "orders", "amount", "decimal", None, 10, 2, "NO", 1],
]
POLICY = GatewayPolicy()


def _transport(*, mutate=None, status=200, seen=None):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"canceled": True})
        if seen is not None:
            seen.append(request)
        body = json.loads(request.content)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert body["hospitalId"] == "hospital-7"
        assert body["params"] == {"limit": 1000}
        assert body["maxRows"] == 1000 and body["maxCost"] == 2
        data = {
            "requestId": request.headers["x-query-request-id"],
            "queryHash": "gateway-hash",
            "plan": {"cost": 0.1},
            "durationMs": 12,
            "limitsApplied": POLICY.request_limits(),
            "columns": COLUMNS,
            "rows": ROWS,
            "rowCount": len(ROWS),
        }
        if mutate:
            mutate(data)
        return httpx.Response(status, json=data)

    return httpx.MockTransport(handler)


def _gateway(tmp_path, transport):
    return GuardedSQLGatewayClient(
        base_url="http://127.0.0.1:8080",
        token=TOKEN,
        target_field="hospitalId",
        lock_path=tmp_path / "locks" / "gateway.lock",
        audit_path=tmp_path / "audit.jsonl",
        transport=transport,
    )


def test_repo_local_guard_accepts_fixed_metadata_query_and_blocks_unsafe_sql():
    from server.context.live_mssql_source import _QUERY

    guard_read_sql(_QUERY, {"limit": 1000}, 1000)
    blocked = [
        "SELECT * FROM dbo.Patient",
        "SELECT TOP (1) * FROM x; DELETE FROM x",
        "UPDATE dbo.Patient SET name='x'",
        "SELECT TOP (1) * INTO #x FROM dbo.Patient",
        "SELECT TOP (1) * FROM OPENROWSET('x')",
        "SELECT TOP (1) * FROM x OFFSET 1 ROWS",
        "SELECT TOP (1) * FROM x WHERE name = 'unterminated",
    ]
    for sql in blocked:
        try:
            guard_read_sql(sql, {}, 1000)
            assert False, f"unsafe SQL was accepted: {sql}"
        except ValueError:
            pass


def test_guarded_gateway_enforces_request_contract_and_hash_only_audit(tmp_path):
    from server.context.live_mssql_source import _QUERY

    seen = []
    gateway = _gateway(tmp_path, _transport(seen=seen))
    data = gateway.query(target_id="hospital-7", sql=_QUERY, params={"limit": 1000})

    assert data and data["rowCount"] == 3
    body = json.loads(seen[0].content)
    assert body["sql"] == _QUERY
    assert body["maxPlanRows"] == 100000
    assert body["maxExecutionMs"] == 5000
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"status":"executed"' in audit
    assert '"sql_hash":' in audit
    assert _QUERY not in audit and TOKEN not in audit


def test_guarded_gateway_fails_closed_on_contract_mismatch(tmp_path):
    cases = [
        lambda data: data.update(requestId="wrong"),
        lambda data: data.pop("limitsApplied"),
        lambda data: data["limitsApplied"].update(maxRows=999),
        lambda data: data.update(rowCount=999),
    ]
    from server.context.live_mssql_source import _QUERY

    for index, mutate in enumerate(cases):
        gateway = GuardedSQLGatewayClient(
            base_url="http://127.0.0.1:8080",
            token=TOKEN,
            target_field="hospitalId",
            lock_path=tmp_path / f"lock-{index}",
            audit_path=tmp_path / f"audit-{index}.jsonl",
            transport=_transport(mutate=mutate),
        )
        assert gateway.query(
            target_id="hospital-7", sql=_QUERY, params={"limit": 1000}
        ) is None


def test_gateway_stream_cap_stops_before_full_response_buffering(tmp_path):
    from server.context.live_mssql_source import _QUERY

    seen_chunks = []

    class ChunkStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"1234", b"5678", b"never-read"):
                seen_chunks.append(chunk)
                yield chunk

    def handler(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"canceled": True})
        return httpx.Response(200, stream=ChunkStream())

    gateway = GuardedSQLGatewayClient(
        base_url="http://127.0.0.1:8080", token=TOKEN,
        target_field="hospitalId", lock_path=tmp_path / "stream.lock",
        audit_path=tmp_path / "stream-audit.jsonl",
        policy=GatewayPolicy(max_response_bytes=5),
        transport=httpx.MockTransport(handler),
    )
    assert gateway.query(
        target_id="hospital-7", sql=_QUERY, params={"limit": 1000}
    ) is None
    assert seen_chunks == [b"1234", b"5678"]


def test_gateway_content_length_cap_rejects_before_stream_read(tmp_path):
    from server.context.live_mssql_source import _QUERY

    class MustNotRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("oversized content-length body must not be read")

    def handler(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"canceled": True})
        return httpx.Response(
            200, headers={"content-length": "6"}, stream=MustNotRead()
        )

    gateway = GuardedSQLGatewayClient(
        base_url="http://127.0.0.1:8080", token=TOKEN,
        target_field="hospitalId", lock_path=tmp_path / "length.lock",
        audit_path=tmp_path / "length-audit.jsonl",
        policy=GatewayPolicy(max_response_bytes=5),
        transport=httpx.MockTransport(handler),
    )
    assert gateway.query(
        target_id="hospital-7", sql=_QUERY, params={"limit": 1000}
    ) is None


def test_gateway_total_deadline_interrupts_slow_drip_stream(tmp_path):
    from server.context.live_mssql_source import _QUERY

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                time.sleep(0.04)
                yield b"{}"

    def handler(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"canceled": True})
        return httpx.Response(200, stream=SlowStream())

    gateway = GuardedSQLGatewayClient(
        base_url="http://127.0.0.1:8080", token=TOKEN,
        target_field="hospitalId", lock_path=tmp_path / "deadline.lock",
        audit_path=tmp_path / "deadline-audit.jsonl",
        policy=GatewayPolicy(request_timeout_sec=0.05),
        transport=httpx.MockTransport(handler),
    )
    started = time.monotonic()
    assert gateway.query(
        target_id="hospital-7", sql=_QUERY, params={"limit": 1000}
    ) is None
    assert time.monotonic() - started < 0.09


def test_gateway_rejects_remote_http_short_token_and_unsafe_target(tmp_path):
    from server.context.live_mssql_source import _QUERY

    common = {
        "target_field": "hospitalId",
        "lock_path": tmp_path / "lock",
        "audit_path": tmp_path / "audit.jsonl",
    }
    remote = GuardedSQLGatewayClient(
        base_url="http://gateway.internal", token=TOKEN, **common
    )
    short = GuardedSQLGatewayClient(
        base_url="https://gateway.internal", token="short", **common
    )
    valid = GuardedSQLGatewayClient(
        base_url="https://gateway.internal", token=TOKEN, **common
    )

    assert remote.query(target_id="hospital-7", sql=_QUERY, params={"limit": 1}) is None
    assert short.query(target_id="hospital-7", sql=_QUERY, params={"limit": 1}) is None
    assert valid.query(target_id="hospital/7?secret=x", sql=_QUERY, params={"limit": 1}) is None


def test_connection_lock_blocks_overlapping_gateway_read(tmp_path):
    from server.context.live_mssql_source import _QUERY

    lock_path = tmp_path / "gateway.lock"
    lock_path.touch()
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        gateway = GuardedSQLGatewayClient(
            base_url="http://127.0.0.1:8080",
            token=TOKEN,
            target_field="hospitalId",
            lock_path=lock_path,
            audit_path=tmp_path / "audit.jsonl",
            transport=_transport(),
        )
        assert gateway.query(
            target_id="hospital-7", sql=_QUERY, params={"limit": 1000}
        ) is None


def test_gateway_token_is_covered_by_shared_secret_redactor(monkeypatch):
    from server import config
    from server.context.base import redact_secrets

    monkeypatch.setattr(config, "MSSQL_GATEWAY_TOKEN", TOKEN)
    assert TOKEN not in redact_secrets(f"gateway failed: {TOKEN}")


def test_gateway_error_degrades_without_exposing_secret(tmp_path, capsys):
    from server.context.live_mssql_source import _QUERY

    gateway = _gateway(tmp_path, _transport(status=500))
    assert gateway.query(
        target_id="hospital-7", sql=_QUERY, params={"limit": 1000}
    ) is None
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_schema_adapter_renders_and_filters_live_metadata(tmp_path):
    schema = LiveMSSQLSchemaClient(_gateway(tmp_path, _transport()))
    ddl = schema.fetch_schema("hospital-7")

    assert "CREATE TABLE [dbo].[users]" in ddl
    assert "[name] nvarchar(100) NULL" in ddl
    assert "[amount] decimal(10,2) NOT NULL" in ddl

    source = live_schema_source(target_id="hospital-7", client=schema)
    out = source(
        ContextRequest(
            repo="acme/api", pr_number=1, changed_files=("models/user.py",)
        )
    )
    assert "[dbo].[users]" in out
    assert "[dbo].[orders]" not in out


def test_render_schema_drops_unsafe_identifiers_and_types():
    rows = [
        {
            "table_schema": "dbo",
            "table_name": "users]; DROP TABLE users;--",
            "column_name": "id",
            "data_type": "bigint",
            "is_nullable": "NO",
        },
        {
            "table_schema": "dbo",
            "table_name": "users",
            "column_name": "id",
            "data_type": "bigint); DROP TABLE x;--",
            "is_nullable": "NO",
        },
    ]
    assert render_schema(rows) == ""
