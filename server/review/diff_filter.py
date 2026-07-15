"""PR diff에서 리뷰 가치가 낮은 노이즈 파일(lock/generated/vendored/minified/snapshot)을
제외하고, 남은 diff를 파일 경계 기준 예산 이하 청크로 쪼갠다. 순수 함수(테스트 용이).

파이프라인이 diff를 프롬프트에 인라인하므로 크기가 곧 비용/상한이다. 노이즈를 걷어
크기를 낮추고(filter), 그래도 크면 파일 단위로 나눠(chunk) 통째 취소 대신 스케일한다."""

import re

# `diff --git a/<path> b/<path>` 파일 블록 헤더.
_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.MULTILINE)

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


def split_file_blocks(diff: str) -> list[tuple[str, str]]:
    """diff를 `diff --git` 헤더 경계로 나눠 [(path, block_text), ...] 반환.
    첫 헤더 앞 프리앰블(정상 gh pr diff엔 없음)은 ("", text)로 보존(무엇도 유실 안 함).
    헤더가 아예 없으면 [("", diff)] — degenerate-safe."""
    if not diff:
        return []
    matches = list(_FILE_HEADER.finditer(diff))
    if not matches:
        return [("", diff)]
    blocks: list[tuple[str, str]] = []
    if matches[0].start() > 0:  # 첫 헤더 앞 프리앰블 보존
        blocks.append(("", diff[: matches[0].start()]))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        # 리네임 등으로 a/b 경로가 다를 수 있으니 b(대상) 경로를 파일 식별자로 사용.
        blocks.append((m.group("b"), diff[m.start() : end]))
    return blocks


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


def chunk_by_budget(diff: str, budget: int) -> list[str]:
    """diff를 예산 이하 청크 리스트로 분할. 파일 블록은 쪼개지 않는다(모델이 파일 전체를 보게).
    len(diff) <= budget면 [diff] — 기존 단일 프롬프트 동작 그대로(빠른 경로).
    단일 블록이 예산 초과면 그 블록 자체가 한 청크(v1은 그대로 전송)."""
    if len(diff) <= budget:
        return [diff]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for _, text in split_file_blocks(diff):
        if cur and cur_len + len(text) > budget:
            chunks.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(text)
        cur_len += len(text)
    if cur:
        chunks.append("".join(cur))
    return chunks
