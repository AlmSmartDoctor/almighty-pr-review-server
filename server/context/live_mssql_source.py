"""Metadata-only schema adapter over the repository-local guarded MSSQL client."""

import re
from collections import OrderedDict

from server import config
from server.context.db_schema_source import related_schema
from server.safe_db.sql_gateway import GuardedSQLGatewayClient, valid_target_id

_QUERY = """SELECT TOP (@limit)
  c.TABLE_SCHEMA AS table_schema,
  c.TABLE_NAME AS table_name,
  c.COLUMN_NAME AS column_name,
  c.DATA_TYPE AS data_type,
  c.CHARACTER_MAXIMUM_LENGTH AS max_length,
  c.NUMERIC_PRECISION AS numeric_precision,
  c.NUMERIC_SCALE AS numeric_scale,
  c.IS_NULLABLE AS is_nullable,
  c.ORDINAL_POSITION AS ordinal_position
FROM INFORMATION_SCHEMA.COLUMNS AS c
JOIN INFORMATION_SCHEMA.TABLES AS t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE t.TABLE_TYPE = 'BASE TABLE'
  AND c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION"""

_MAX_ROWS = 1000
_EXPECTED_COLUMNS = (
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
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,127}\Z")
_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_CHAR_TYPES = {"char", "nchar", "varchar", "nvarchar", "binary", "varbinary"}
_DECIMAL_TYPES = {"decimal", "numeric"}


def _bounded_int(value, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _type_sql(data_type: str, max_length, precision, scale) -> str:
    lowered = data_type.lower()
    if lowered in _CHAR_TYPES:
        length = _bounded_int(max_length, minimum=-1, maximum=1_000_000)
        if length == -1:
            return f"{data_type}(MAX)"
        if length and length > 0:
            return f"{data_type}({length})"
    if lowered in _DECIMAL_TYPES:
        p = _bounded_int(precision, minimum=1, maximum=38)
        s = _bounded_int(scale, minimum=0, maximum=38)
        if p is not None and s is not None and s <= p:
            return f"{data_type}({p},{s})"
    return data_type


def render_schema(rows: list[dict]) -> str:
    """Render validated INFORMATION_SCHEMA rows as canonical evidence DDL."""
    tables: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
    for row in rows:
        if not isinstance(row, dict):
            continue
        schema = row.get("table_schema")
        table = row.get("table_name")
        column = row.get("column_name")
        data_type = row.get("data_type")
        if not all(
            isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value)
            for value in (schema, table, column)
        ):
            continue
        if not isinstance(data_type, str) or not _TYPE_RE.fullmatch(data_type):
            continue
        nullable = (
            "NULL" if str(row.get("is_nullable", "")).upper() == "YES" else "NOT NULL"
        )
        column_type = _type_sql(
            data_type,
            row.get("max_length"),
            row.get("numeric_precision"),
            row.get("numeric_scale"),
        )
        tables.setdefault((schema, table), []).append(
            f"  [{column}] {column_type} {nullable}"
        )
    statements = [
        f"CREATE TABLE [{schema}].[{table}] (\n" + ",\n".join(columns) + "\n);"
        for (schema, table), columns in tables.items()
        if columns
    ]
    return "\n\n".join(statements)


class LiveMSSQLSchemaClient:
    def __init__(self, gateway: GuardedSQLGatewayClient):
        self.gateway = gateway

    def fetch_schema(self, target_id: str) -> str:
        if not valid_target_id(target_id):
            return ""
        data = self.gateway.query(
            target_id=target_id,
            sql=_QUERY,
            params={"limit": _MAX_ROWS},
        )
        if not data:
            return ""
        columns = data.get("columns")
        rows = data.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return ""
        names = tuple(c.get("name") for c in columns if isinstance(c, dict))
        if names != _EXPECTED_COLUMNS:
            return ""
        if not all(isinstance(row, list) and len(row) == len(names) for row in rows):
            return ""
        return render_schema([dict(zip(names, row)) for row in rows])


def configured_client() -> LiveMSSQLSchemaClient | None:
    gateway = GuardedSQLGatewayClient(
        base_url=config.MSSQL_GATEWAY_URL,
        token=config.MSSQL_GATEWAY_TOKEN,
        target_field=config.MSSQL_GATEWAY_TARGET_FIELD,
        lock_path=config.MSSQL_GATEWAY_LOCK_PATH,
        audit_path=config.MSSQL_GATEWAY_AUDIT_PATH,
    )
    return LiveMSSQLSchemaClient(gateway) if gateway.configured else None


def live_schema_source(*, target_id: str, client: LiveMSSQLSchemaClient | None = None):
    active = client or configured_client()
    if active is None:
        return None

    def source(req) -> str:
        ddl = active.fetch_schema(target_id)
        return (
            related_schema(ddl, getattr(req, "changed_files", ()) or ()) if ddl else ""
        )

    return source
