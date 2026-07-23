# Safe-DB guard extraction provenance

The repository-local guard in `sql_gateway.py` was ported from the Safe-DB skill available at implementation time.
Only the resolver-backed SQL Gateway read path was retained:

- `scripts/lib/sql_gateway.sh`: SQL/parameter/TOP guards and v2 response contract
- `scripts/lib/guard.sh`: connection read lock and hash-only audit semantics

No runtime path dependency on the Pi skill remains. Generic database providers, write paths, CLI/config loading,
BigQuery approvals, and the deployable MSSQL Gateway were intentionally not copied.

The Gateway component referenced by that skill records its own upstream provenance as
`AlmSmartDoctor/mssql-query-proxy` at commit `e303846`; that Go component is not vendored here.
