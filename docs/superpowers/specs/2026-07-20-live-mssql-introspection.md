# Live MSSQL Read-only Introspection Spec

**작성일:** 2026-07-20
**상태:** metadata-only MVP, repository-local Safe-DB guard

## 목표

로컬 병원 DB 조회 스킬에서 검증된 Safe-DB SQL Gateway read 로직 중 필요한 부분만 레포에 이식해,
레포별 데이터베이스의 **테이블·컬럼 메타데이터만** 읽고 기존 `db_schema` 컨텍스트와 Ground Truth
Wiki의 database evidence에 공급한다. 실행 환경의 `~/.pi` 또는 외부 Safe-DB 설치 경로에는 의존하지 않는다.

## 비목표

- 업무 데이터 row 조회, 샘플링, 통계 집계
- 임의 SQL 입력 또는 대시보드 SQL 실행기
- 쓰기/DDL/procedure/동적 SQL
- 자동 페이지네이션이나 전체 대형 카탈로그 수집
- Gateway를 우회한 DB raw 접속
- Safe-DB의 PostgreSQL/MySQL/BigQuery/write 기능 전체 복사

## 이식 범위와 출처

`server/safe_db/sql_gateway.py`는 로컬 Safe-DB의 다음 동작을 Python으로 동일하게 이식한다.

- `scripts/lib/sql_gateway.sh`: SELECT/WITH-only 분류, comment/literal masking, forbidden keyword,
  outer `TOP`과 parameter 한도, Gateway request/response v2 계약
- `scripts/lib/guard.sh`: connection별 non-blocking read lock, raw SQL 대신 SHA-256 audit

Almighty에 필요 없는 write-plan, generic DB provider, BigQuery approval, CLI/config loader는 가져오지 않는다.
Gateway 자체는 별도 배포 경계이며 resolver routing, 강제 read-only credential, T-SQL 재검증,
SHOWPLAN_XML, streaming row/byte cap, execution timeout과 TDS cancellation을 계속 담당한다.

## 설정과 비밀 경계

- `ALMIGHTY_MSSQL_GATEWAY_URL`: 신뢰된 HTTPS origin. 명시적 loopback 테스트만 HTTP 허용.
- `ALMIGHTY_MSSQL_GATEWAY_TOKEN`: query/cancel bearer token, 최소 32자.
- `ALMIGHTY_MSSQL_GATEWAY_TARGET_FIELD`: `hospitalId`(기본) 또는 `targetId`.

레포 DB에는 비밀이 아닌 `live_db_target_id`만 저장한다. Gateway token은 env-only이며 기존 secret
redactor에 포함한다. resolver URL, DB 주소·계정·비밀번호는 Gateway 밖으로 나오지 않는다. audit에는 SQL
원문·parameter·token을 기록하지 않고 SQL hash, target ID, request ID, row/cost/duration metadata만 기록한다.

## 고정 조회

사용자 입력은 SQL에 삽입하지 않는다. target ID는 request field, `limit`은 JSON parameter로 전달한다.

```sql
SELECT TOP (@limit)
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
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
```

요청 상한은 rows 1,000, plan rows 100,000, cost 2, execution 5초, response 5 MiB로 고정한다. response는
Content-Length를 먼저 검사하고 streaming 누적이 5 MiB를 넘기 전에 중단한다. 개별 socket timeout과 별도로 전체
wall-clock deadline을 watchdog으로 강제한다. connection별 파일 lock으로 동시 read는 하나만 허용한다. HTTP
timeout/오류 시 같은 opaque request ID로 cancel을 시도한다. 자동 다음 페이지 요청은 하지 않는다.

## fail-closed 검증

- URL에 credential/query/fragment 금지, HTTPS 강제(loopback 테스트 예외), redirect 금지, ambient proxy 무시
- target/parameter identifier와 scalar parameter type 검증
- comment/literal을 가린 뒤 단일 `SELECT`/`WITH ... SELECT`만 허용
- outer `TOP (@limit)` 필수, LIMIT/OFFSET/SELECT INTO/temp table/forbidden keyword 차단
- 응답 `requestId`가 보낸 32자 lowercase hex와 정확히 일치
- `limitsApplied`가 요청한 모든 상한과 정확히 일치
- `columns`가 고정 query alias와 정확히 일치, `rowCount`와 row 수 일치
- 응답 byte/row 상한 및 schema/table/column/type 문자 집합 재검증

실패·timeout·계약 불일치는 `""`로 degrade하며 provider 오류 body나 token을 로그에 출력하지 않는다.
리뷰와 Wiki 생성은 라이브 DB 접근 실패 때문에 차단하지 않는다.

## 기존 seam 연결

- `server/safe_db/sql_gateway.py`: 레포 내 guard, lock, HTTP v2 client, audit/cancel.
- `server/context/live_mssql_source.py`: 고정 metadata query와 canonical `CREATE TABLE` 렌더.
- 리뷰 컨텍스트: 정적 `db_schema_path`와 live source를 독립적으로 수집해 합치고 변경 파일 관련 테이블만
  최대 20개 주입한다.
- Wiki: 정적/live DDL을 합쳐 prompt 및 `build_database_catalog` 검증에 사용한다.

## 테스트

- 고정 SQL 허용 및 multi-statement/write/SELECT INTO/OPENROWSET/OFFSET 차단
- request field, parameter, 모든 안전 상한과 bearer header
- HTTPS/token/target 검증, non-blocking connection lock
- `requestId`/`limitsApplied`/column shape/rowCount fail-closed
- audit에 hash만 있고 SQL/token이 없음
- identifier/type sanitization, canonical DDL, 변경 파일 필터
- registry 정적+live 독립 degrade 및 Wiki catalog 연결
