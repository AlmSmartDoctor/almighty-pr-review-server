import re

from server.context.base import read_confined

_MAX_TABLES = 20  # 한 PR이 끌어올 수 있는 테이블 DDL 수 상한(fan-out 캡)
_MAX_SCHEMA_BYTES = 2_000_000  # 스키마 덤프 읽기 상한(부모 프로세스 메모리 보호)

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# 헤더는 라인 시작(공백 허용)의 CREATE TABLE만. '-- CREATE TABLE …' 주석 라인은
# '-'로 시작해 자연 제외된다. 인용 스키마-한정명을 캡처해 마지막 세그먼트를 테이블명으로 쓴다.
_HEADER_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"((?:[`\"\[]?\w+[`\"\]]?\.)?[`\"\[]?\w+[`\"\]]?)",
    re.IGNORECASE,
)
_INLINE_COMMENT_RE = re.compile(r"--.*")


def _terminates(line: str) -> bool:
    """CREATE TABLE 문의 종결 라인 판정(dialect·문자열 파싱 없이 라인 형태만 본다).
    (a) 컬럼 목록을 닫는 라인 = lstrip이 ')'로 시작(')' 단독/') ENGINE=…;' tail 포함), 또는
    (b) 문장 종결 = 인라인 '-- …' 주석 제거 후 ';'로 끝남('id INT, -- t#1;' 오종결 방지).
    (a)는 컬럼/한정키 라인의 라인-끝 ')'·';'에 오작동하지 않는다(그 라인들은 ')'로 시작하지 않음)."""
    if line.lstrip().startswith(")"):
        return True
    return _INLINE_COMMENT_RE.sub("", line).rstrip().endswith(";")


def _singular(word: str) -> str:
    """단순 trailing-s 단수화. 양측(테이블명·경로 토큰)에 대칭 적용해 users↔user 매칭."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _tokens(text: str) -> set:
    """camelCase 분해 → 비영숫자 split → 소문자 → 단수화. 테이블명·파일경로에 동일 적용."""
    spaced = _CAMEL_RE.sub(" ", text)
    return {_singular(t) for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t}


def _parse_tables(ddl: str):
    """(테이블명, 전체 CREATE TABLE 문)을 순서대로 추출. 라인 지향: 헤더 라인부터
    _terminates()가 참인 첫 라인까지를 한 문장으로 본다. 문자열/주석 내부의
    괄호·세미콜론·백슬래시·$$ 를 파싱하지 않아 dialect에 무관하게 견고.
    (드묾: 미종결 문자열이 ';'로 끝나는 라인에 걸치면 조기 종료될 수 있음.)"""
    lines = ddl.splitlines(keepends=True)
    tables = []
    i, n = 0, len(lines)
    while i < n:
        m = _HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1).split(".")[-1].strip('`"[] ')
        start = i
        while i < n and not _terminates(lines[i]):
            i += 1
        end = i if i < n else n - 1  # 종결 라인 없으면 EOF까지
        if name:
            tables.append((name, "".join(lines[start : end + 1])))
        i = end + 1
    return tables


def file_schema_source(*, path: str, root: str):
    """레포에 체크인된 DDL 덤프에서 변경 파일 관련 테이블의 DDL만 골라주는
    schema_source(req)->str 콜백을 만든다. 실패/무매칭/무신호는 ""로 degrade."""

    def source(req) -> str:
        root_eff = (
            getattr(req, "workdir", "") or root
        )  # PR-head worktree 우선, local_path 폴백
        ddl = read_confined(path, root_eff, _MAX_SCHEMA_BYTES)
        if not ddl:
            return ""
        file_tokens = [_tokens(str(f)) for f in getattr(req, "changed_files", ()) or ()]
        if not file_tokens:
            return ""
        picked = []
        for name, stmt in _parse_tables(ddl):
            tname = _tokens(name)
            # 테이블 토큰이 어떤 변경 파일 한 곳의 토큰집합에 모두 포함되면 관련
            # (order_items ⊆ {order,item,rb} 매칭; 파일 간 교차 매칭은 배제).
            if tname and any(tname <= ft for ft in file_tokens):
                picked.append(stmt)
        return "\n\n".join(picked[:_MAX_TABLES])

    return source
