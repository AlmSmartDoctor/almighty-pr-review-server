"""PR diff에서 리뷰 가치가 낮은 노이즈 파일(lock/generated/vendored/minified/snapshot)을
제외하고, 남은 diff를 파일 경계 기준 예산 이하 청크로 쪼갠다. 순수 함수(테스트 용이).

파이프라인이 diff를 프롬프트에 인라인하므로 크기가 곧 비용/상한이다. 노이즈를 걷어
크기를 낮추고(filter), 그래도 크면 파일 단위로 나눠(chunk) 통째 취소 대신 스케일한다."""

import hashlib
import re
import shlex
from dataclasses import dataclass

# `diff --git ...` file block header. Paths may be C-quoted by git.
_FILE_HEADER = re.compile(r"^diff --git .+$", re.MULTILINE)

# hunk 헤더 `@@ -l,s +l,s @@` — 신규(RIGHT)측 시작 라인번호를 캡처.
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_FULL_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class DiffChunk:
    index: int
    text: str
    owned_changed_lines: dict[str, frozenset[int]]
    diff_hash: str

# basename 완전일치로 거르는 lockfile류.
DEFAULT_IGNORE = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "poetry.lock",
        "Pipfile.lock",
        "composer.lock",
        "Gemfile.lock",
        "Cargo.lock",
        "go.sum",
    }
)
_IGNORE_SUFFIX = (".min.js", ".min.css", ".map", ".snap", ".lock")
_IGNORE_DIR = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    ".next/",
    "__snapshots__/",
)


def _decode_git_path(value: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 3 < len(value) and all(
            character in "01234567" for character in value[i + 1 : i + 4]
        ):
            raw.append(int(value[i + 1 : i + 4], 8))
            i += 4
            continue
        raw.extend(value[i].encode("utf-8", "replace"))
        i += 1
    return raw.decode("utf-8", "replace")


def _header_target(header: str) -> str:
    try:
        parts = shlex.split(header)
    except ValueError:
        return ""
    if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
        return ""
    target = _decode_git_path(parts[3])
    return target[2:] if target.startswith("b/") else target


def split_file_blocks(diff: str) -> list[tuple[str, str]]:
    """diff를 `diff --git` 헤더 경계로 나눠 [(target_path, block), ...]."""
    if not diff:
        return []
    matches = list(_FILE_HEADER.finditer(diff))
    if not matches:
        return [("", diff)]
    blocks: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        blocks.append(("", diff[: matches[0].start()]))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        blocks.append(
            (_header_target(match.group(0)), diff[match.start() : end])
        )
    return blocks


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """PR review 인라인 코멘트를 달 수 있는 파일별 RIGHT-side(신규 파일) 라인 집합.
    GitHub은 diff에 등장한 라인에만 review 코멘트를 허용하고, createReview는 유효하지
    않은 라인이 하나라도 있으면 요청 전체를 422로 거부한다. 그래서 게시 전에 이 집합으로
    finding 라인을 걸러 유효한 것만 인라인 부착한다.

    hunk 헤더의 +시작에서 신규 라인번호를 시작해 추가(+)·문맥( ) 라인마다 1씩 증가시키고,
    삭제(-) 라인은 신규측 번호를 증가시키지 않는다. 파일 헤더(+++/---)는 첫 @@ 이전이라
    카운터가 None → 자연히 무시된다."""
    out: dict[str, set[int]] = {}
    for path, block in split_file_blocks(diff):
        if not path:
            continue
        lines = out.setdefault(path, set())
        new_no: int | None = None
        for ln in block.splitlines():
            if ln.startswith("@@"):
                m = _HUNK_HEADER.match(ln)
                new_no = int(m.group("start")) if m else None
            elif new_no is None:
                continue  # 첫 hunk 이전(파일 헤더 등) → 신규측 라인 아님
            elif ln.startswith("+") or ln.startswith(" "):  # 추가·문맥 → 신규측 라인
                lines.add(new_no)
                new_no += 1
            # 삭제(-)·"\ No newline"·빈 줄 → 신규측 번호 불변
    return out


def changed_lines(diff: str) -> dict[str, set[int]]:
    """Return only added RIGHT-side lines; unchanged context is excluded."""
    out: dict[str, set[int]] = {}
    for path, block in split_file_blocks(diff):
        if not path:
            continue
        lines = out.setdefault(path, set())
        new_no: int | None = None
        for line in block.splitlines():
            if line.startswith("@@"):
                match = _HUNK_HEADER.match(line)
                new_no = int(match.group("start")) if match else None
            elif new_no is None:
                continue
            elif line.startswith("+") and not line.startswith("+++"):
                lines.add(new_no)
                new_no += 1
            elif line.startswith(" "):
                new_no += 1
            # deletion and metadata do not advance the right-side line number
    return out


def _is_noise(path: str) -> bool:
    if not path:
        return False  # 경로 없는 블록(프리앰블/degenerate)은 보존
    base = path.rsplit("/", 1)[-1]
    if base in DEFAULT_IGNORE:
        return True
    if path.endswith(_IGNORE_SUFFIX):
        return True
    # 디렉터리 토큰은 경로 세그먼트로만 매칭(부분 문자열 아님). 안 그러면 rebuild/·
    # redist/·myvendor/ 같은 정상 소스가 build/·dist/·vendor/에 오탐돼 리뷰에서 유실된다.
    return any(path.startswith(seg) or ("/" + seg) in path for seg in _IGNORE_DIR)


def filter_reviewable(diff: str) -> str:
    """노이즈 아닌 파일 블록만 원래 순서로 이어붙여 반환. 전부 노이즈면 "" (리뷰할 게 없음 신호)."""
    kept = [text for path, text in split_file_blocks(diff) if not _is_noise(path)]
    return "".join(kept)


@dataclass(frozen=True)
class _HunkLine:
    text: str
    old_no: int
    new_no: int
    old_count: int
    new_count: int
    owned_line: int | None


def _hunk_line_units(text: str) -> list[_HunkLine]:
    units = []
    old_no: int | None = None
    new_no: int | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("@@"):
            match = _FULL_HUNK_HEADER.match(line)
            old_no = int(match.group("old")) if match else None
            new_no = int(match.group("new")) if match else None
            continue
        if old_no is None or new_no is None or not line:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            units.append(_HunkLine(line, old_no, new_no, 0, 1, new_no))
            new_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            units.append(_HunkLine(line, old_no, new_no, 1, 0, None))
            old_no += 1
        elif line.startswith(" "):
            units.append(_HunkLine(line, old_no, new_no, 1, 1, None))
            old_no += 1
            new_no += 1
        # "\\ No newline" and other metadata do not change line coordinates.
    return units


def _synthetic_diff_prefix(path: str, budget: int) -> str:
    display = path or "unknown"
    prefix = f"diff --git a/{display} b/{display}\n"
    # Leave room for a hunk header and at least a one-character diff line.
    if len(prefix) + 32 > budget:
        display = f"oversized-{hashlib.sha256(display.encode()).hexdigest()[:8]}"
        prefix = f"diff --git a/{display} b/{display}\n"
    return prefix


def _render_hunk_piece(prefix: str, units: list[_HunkLine]) -> str:
    old_count = sum(unit.old_count for unit in units)
    new_count = sum(unit.new_count for unit in units)
    header = (
        f"@@ -{units[0].old_no},{old_count} "
        f"+{units[0].new_no},{new_count} @@ continued\n"
    )
    return prefix + header + "".join(unit.text for unit in units)


def _truncate_unit_to_budget(prefix: str, unit: _HunkLine, budget: int) -> _HunkLine:
    header = _render_hunk_piece(prefix, [unit])[:-len(unit.text)]
    available = max(0, budget - len(header))
    marker = " … [truncated; inspect snapshot]\n"
    sigil = unit.text[:1] if unit.text[:1] in {"+", "-", " "} else " "
    if available <= len(sigil):
        shortened = sigil[:available]
    elif available <= len(sigil) + len(marker):
        shortened = (sigil + "…\n")[:available]
    else:
        body_room = available - len(sigil) - len(marker)
        shortened = sigil + unit.text[1 : 1 + body_room].rstrip("\n") + marker
    return _HunkLine(
        shortened,
        unit.old_no,
        unit.new_no,
        unit.old_count,
        unit.new_count,
        unit.owned_line,
    )


def _split_oversized_block(
    path: str, text: str, budget: int
) -> list[tuple[str, dict[str, set[int]]]]:
    """Create self-contained parseable continuation hunks with explicit ownership."""
    units = _hunk_line_units(text)
    prefix = _synthetic_diff_prefix(path, budget)
    if not units or len(prefix) + 28 >= budget:
        marker = f"# oversized diff metadata: {path or 'unknown'}\n"
        return [((prefix + marker)[:budget], {})]

    pieces: list[tuple[str, dict[str, set[int]]]] = []
    current: list[_HunkLine] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        rendered = _render_hunk_piece(prefix, current)
        owned = {unit.owned_line for unit in current if unit.owned_line is not None}
        pieces.append((rendered, {path: owned} if path and owned else {}))
        current = []

    for unit in units:
        if current:
            previous = current[-1]
            contiguous = (
                unit.old_no == previous.old_no + previous.old_count
                and unit.new_no == previous.new_no + previous.new_count
            )
            if not contiguous:
                flush()
        candidate = current + [unit]
        if len(_render_hunk_piece(prefix, candidate)) <= budget:
            current = candidate
            continue
        flush()
        fitted = unit
        if len(_render_hunk_piece(prefix, [fitted])) > budget:
            fitted = _truncate_unit_to_budget(prefix, fitted, budget)
        current = [fitted]
    flush()
    return pieces


def _freeze_ownership(value: dict[str, set[int]]) -> dict[str, frozenset[int]]:
    return {path: frozenset(lines) for path, lines in value.items() if lines}


def chunk_records_by_budget(diff: str, budget: int) -> list[DiffChunk]:
    """Return hard-capped chunks plus canonical added-line ownership metadata."""
    if budget <= 0:
        raise ValueError("diff chunk budget must be positive")
    if not diff:
        return []
    pieces: list[tuple[str, dict[str, set[int]]]] = []
    for path, text in split_file_blocks(diff):
        if len(text) > budget:
            pieces.extend(_split_oversized_block(path, text, budget))
        else:
            pieces.append((text, changed_lines(text)))

    packed: list[tuple[str, dict[str, set[int]]]] = []
    current_text = ""
    current_owned: dict[str, set[int]] = {}

    def flush():
        nonlocal current_text, current_owned
        if current_text:
            packed.append((current_text, current_owned))
            current_text, current_owned = "", {}

    for text, ownership in pieces:
        if current_text and len(current_text) + len(text) > budget:
            flush()
        current_text += text
        for path, lines in ownership.items():
            current_owned.setdefault(path, set()).update(lines)
        if len(current_text) >= budget:
            flush()
    flush()

    records = []
    seen: set[tuple[str, int]] = set()
    for index, (text, ownership) in enumerate(packed):
        frozen = _freeze_ownership(ownership)
        for path, lines in frozen.items():
            for line in lines:
                key = (path, line)
                if key in seen:
                    raise ValueError("changed line belongs to multiple diff chunks")
                seen.add(key)
        records.append(
            DiffChunk(
                index=index,
                text=text,
                owned_changed_lines=frozen,
                diff_hash=hashlib.sha256(
                    text.encode("utf-8", "replace")
                ).hexdigest(),
            )
        )
    expected = {
        (path, line) for path, lines in changed_lines(diff).items() for line in lines
    }
    if seen != expected:
        raise ValueError("diff chunk ownership lost changed lines")
    return records


def chunk_by_budget(diff: str, budget: int) -> list[str]:
    """Compatibility wrapper returning chunk text only."""
    return [record.text for record in chunk_records_by_budget(diff, budget)]
