from server.review.diff_filter import (
    chunk_by_budget,
    commentable_lines,
    filter_reviewable,
    split_file_blocks,
)


def _block(path: str, body: str = "@@ -1 +1 @@\n-a\n+b\n") -> str:
    return f"diff --git a/{path} b/{path}\n{body}"


def test_split_file_blocks_by_header():
    diff = _block("src/a.py") + _block("src/b.py")
    blocks = split_file_blocks(diff)
    assert [p for p, _ in blocks] == ["src/a.py", "src/b.py"]
    assert "".join(t for _, t in blocks) == diff  # 원문 재구성(무손실)


def test_split_preserves_preamble_before_first_header():
    diff = "preamble line\n" + _block("a.py")
    blocks = split_file_blocks(diff)
    assert blocks[0] == ("", "preamble line\n")
    assert blocks[1][0] == "a.py"


def test_split_no_header_returns_single_pathless_block():
    assert split_file_blocks("just some text") == [("", "just some text")]
    assert split_file_blocks("") == []


def test_filter_drops_lockfiles_and_minified_and_vendored():
    diff = (
        _block("src/real.py")
        + _block("package-lock.json")
        + _block("web/app.min.js")
        + _block("node_modules/pkg/index.js")
        + _block("go.sum")
        + _block("dist/bundle.js")
    )
    out = filter_reviewable(diff)
    assert "src/real.py" in out
    for noise in ("package-lock.json", "app.min.js", "node_modules", "go.sum", "dist/"):
        assert noise not in out


def test_filter_keeps_pathless_and_all_real():
    diff = _block("a.py") + _block("b.py")
    assert filter_reviewable(diff) == diff  # 노이즈 없으면 원문 그대로


def test_filter_all_noise_returns_empty():
    diff = _block("yarn.lock") + _block("__snapshots__/x.snap")
    assert filter_reviewable(diff) == ""


def test_filter_ignore_dir_matches_path_segment_not_substring():
    # 정상 소스가 build/·dist/·vendor/ 부분 문자열로 오탐돼 유실되면 안 된다.
    for path in ("src/rebuild/helper.py", "redist/x.py", "app/myvendor/thing.py"):
        assert filter_reviewable(_block(path)) == _block(path), path
    # 진짜 노이즈 디렉터리(세그먼트 일치)는 계속 제외된다.
    for path in (
        "node_modules/pkg/i.js",
        "web/dist/bundle.js",
        "build/out.o",
        "src/vendor/lib.py",
    ):
        assert filter_reviewable(_block(path)) == "", path


def test_commentable_lines_tracks_added_and_context_right_side():
    # @@ -1,2 +10,4 @@ → 신규측 10부터: 문맥(10)·추가(11)·삭제(신규측 불변)·추가(12)·문맥(13)
    diff = _block(
        "a.py",
        body="@@ -1,2 +10,4 @@\n ctx10\n+add11\n-removed\n+add12\n cvtx13\n",
    )
    lines = commentable_lines(diff)
    assert lines["a.py"] == {10, 11, 12, 13}


def test_commentable_lines_multi_file_and_multi_hunk():
    diff = _block(
        "a.py", body="@@ -1 +1 @@\n+one\n@@ -5,0 +5,2 @@\n+five\n+six\n"
    ) + _block("b.py", body="@@ -1 +3 @@\n+three\n")
    lines = commentable_lines(diff)
    assert lines["a.py"] == {1, 5, 6}
    assert lines["b.py"] == {3}


def test_commentable_lines_ignores_file_header_plusminus_and_empty():
    # +++/--- 파일 헤더(첫 @@ 이전)는 +/-로 시작해도 신규측 라인으로 계수되면 안 된다.
    diff = _block("a.py", body="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+only\n")
    assert commentable_lines(diff)["a.py"] == {1}
    assert commentable_lines("") == {}


def test_chunk_small_diff_is_single_fast_path():
    diff = _block("a.py")
    assert chunk_by_budget(diff, budget=100_000) == [diff]


def test_chunk_splits_on_file_boundary_within_budget():
    a, b, c = _block("a.py"), _block("b.py"), _block("c.py")
    diff = a + b + c
    # 예산을 블록 2개 크기로 잡으면 파일 경계에서 나뉜다(블록 미분할).
    budget = len(a) + len(b) - 1
    chunks = chunk_by_budget(diff, budget=budget)
    assert len(chunks) >= 2
    assert "".join(chunks) == diff  # 순서·내용 보존
    for ch in chunks:
        assert ch.count("diff --git") >= 1  # 각 청크는 온전한 파일 블록(들)


def test_chunk_oversized_single_block_becomes_own_chunk():
    big = _block("huge.py", body="@@ -1 +1 @@\n" + ("+x\n" * 1000))
    small = _block("small.py")
    diff = big + small
    chunks = chunk_by_budget(diff, budget=len(small) + 10)
    assert big in chunks  # 예산 초과 단일 블록도 통째로 한 청크
    assert "".join(chunks) == diff
