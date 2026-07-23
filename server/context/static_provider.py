import os
import re
import subprocess
import threading
from collections import OrderedDict
from pathlib import PurePosixPath

from server import config
from server.context.base import ContextBlock, ContextRequest, ContextResult


DEFAULT_REFERENCE_DOCUMENTS = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")
_MAX_CHARS_PER_DOCUMENT = 3_000
# 루트 기본 문서는 넓은 일반 규칙이라 여러 파일이 동시에 발견되면 프롬프트를 쉽게
# 압도한다. 명시 문서·변경 경로 가까운 문서는 기존 상한을 보존하고 루트 기본만 줄인다.
_MAX_ROOT_CHARS_PER_DOCUMENT = 2_000
# 적용 범위 전체는 diff에 이미 있으므로 대표 경로만 보여 준다. 긴 Kotlin 경로를 문서마다
# 반복해 실제 규칙보다 헤더가 커지는 것을 막는다.
_MAX_SCOPE_PATHS = 3
_MAX_CHANGED_FILES_FOR_DISCOVERY = 500
_MAX_DISCOVERY_DIRECTORIES = 40
_MAX_GIT_DOCUMENT_CACHE = 512
_GIT_DOCUMENT_CACHE: OrderedDict[tuple[str, str, str], str | None] = OrderedDict()
_GIT_DOCUMENT_CACHE_LOCK = threading.Lock()


def _clear_git_document_cache() -> None:
    with _GIT_DOCUMENT_CACHE_LOCK:
        _GIT_DOCUMENT_CACHE.clear()


class StaticContextProvider:
    """변경 경로에 적용되는 레포 참조 문서와 명시적 고정 문서를 읽는다.

    각 변경 파일의 디렉터리부터 레포 루트까지 올라가며 표준 문서를 찾고, 동일 문서는
    한 번만 렌더링한다. path가 있으면 기존 static_context_path 동작처럼 변경 경로와
    무관한 고정 문서도 함께 포함한다. 모든 파일은 root 하위로 realpath 봉쇄한다.
    """

    name = "static"

    def __init__(self, *, path: str | None, root: str | None):
        self._path = path
        self._root = root

    def fetch(self, req: ContextRequest) -> ContextResult:
        root_src = req.workdir or self._root
        if not root_src:
            return ContextResult(provider=self.name, status="empty", text="")

        try:
            root = os.path.realpath(root_src)
        except (OSError, ValueError, TypeError):
            return ContextResult(provider=self.name, status="empty", text="")

        candidates = self._candidate_paths(req.changed_files)
        if self._path:
            candidates.setdefault(
                self._path, {"scopes": set(), "explicit": True, "depth": 0}
            )
            candidates[self._path]["explicit"] = True

        # PR이 문서 자체를 바꿔 리뷰 지침을 조작하지 못하도록 base 버전만 읽는다.
        # base_ref가 없는 직접 provider 사용(유닛 테스트/비-PR 호출)만 현재 root를 읽는다.
        base_revision = self._resolve_base_revision(root, req.base_ref, req.base_sha)
        if req.base_ref and base_revision is None:
            return ContextResult(
                provider=self.name,
                status="error",
                text="",
                error="base reference unavailable",
            )

        base_contents: dict[str, str | None] = {}
        cache_hits = 0
        requested_paths = []
        if base_revision:
            requested_paths = [
                display
                for path in candidates
                if (display := self._confined_relative_path(root, path)) is not None
            ]
            base_contents, cache_hits = self._read_git_files(
                root,
                base_revision,
                requested_paths,
                cache_scope=req.repo.casefold(),
            )

        documents: dict[str, dict] = {}
        content_chars_read = 0
        rejected_explicit = False
        for path, meta in candidates.items():
            display_path = self._confined_relative_path(root, path)
            if display_path is None:
                rejected_explicit = rejected_explicit or bool(meta["explicit"])
                continue
            if base_revision:
                content = base_contents.get(display_path)
                identity = display_path
            else:
                real = self._confined_realpath(root, path)
                if real is None:
                    rejected_explicit = rejected_explicit or bool(meta["explicit"])
                    continue
                try:
                    with open(real, encoding="utf-8") as f:
                        content = f.read(_MAX_CHARS_PER_DOCUMENT + 1)
                except (OSError, ValueError, TypeError, IsADirectoryError):
                    continue
                display_path = os.path.relpath(real, root).replace(os.sep, "/")
                identity = real
            if not content or not content.strip():
                continue
            content_chars_read += len(content)
            content_limit = (
                _MAX_ROOT_CHARS_PER_DOCUMENT
                if not meta["explicit"] and meta["depth"] == 0
                else _MAX_CHARS_PER_DOCUMENT
            )
            if len(content) > content_limit:
                content = content[:content_limit] + "\n…[document truncated]"

            existing = documents.get(identity)
            if existing is None:
                documents[identity] = {
                    "path": display_path,
                    "content": content,
                    "scopes": set(meta["scopes"]),
                    "explicit": bool(meta["explicit"]),
                    # 문서 자체(.claude 하위 등)가 아니라 적용 디렉터리의 깊이다.
                    "depth": meta["depth"],
                }
            else:
                existing["scopes"].update(meta["scopes"])
                existing["explicit"] = existing["explicit"] or bool(meta["explicit"])
                existing["depth"] = max(existing["depth"], meta["depth"])

        changed_instructions = self._changed_instruction_documents(req.changed_files)
        if not documents and not (changed_instructions and base_revision):
            return ContextResult(
                provider=self.name,
                status="error" if rejected_explicit else "empty",
                text="",
                error="path outside allowed root" if rejected_explicit else None,
            )

        selected = self._select_within_budget(list(documents.values()))
        selected.sort(key=lambda d: (d["depth"], d["path"]))
        rendered_blocks = []
        semantic_blocks = []
        if changed_instructions and base_revision:
            warning = (
                "⚠ This PR changes repository instruction documents. "
                "The trusted base-branch versions are used for this review:\n"
                + "\n".join(f"- {path}" for path in changed_instructions)
            )
            rendered_blocks.append(warning)
            semantic_blocks.append(
                ContextBlock(
                    source=self.name,
                    block_id="instruction-change-warning",
                    text=warning,
                    priority=5,
                    recoverable_from_repo=False,
                    trust_class="trusted_base_repo",
                    sensitivity="internal",
                    retention="review_history",
                    relevant_files=tuple(changed_instructions),
                )
            )
        for doc in selected:
            rendered = self._render_document(doc)
            rendered_blocks.append(rendered)
            semantic_blocks.append(
                ContextBlock(
                    source=self.name,
                    block_id=doc["path"],
                    text=rendered,
                    priority=50,
                    recoverable_from_repo=True,
                    trust_class="trusted_base_repo",
                    sensitivity="internal",
                    retention="manifest_only",
                    relevant_files=tuple(sorted(doc["scopes"])),
                )
            )
        text = "\n\n".join(rendered_blocks)
        return ContextResult(
            provider=self.name,
            status="ok" if text.strip() else "empty",
            text=text,
            meta={
                "documents": [doc["path"] for doc in selected],
                "revision": base_revision or "worktree",
                "changed_instruction_documents": changed_instructions,
                "cache_hit": bool(requested_paths) and cache_hits == len(requested_paths),
                "cache_hits": cache_hits,
                "items_read": len(requested_paths),
                "items_selected": len(selected),
                "content_chars_read": content_chars_read,
                "content_chars_selected": sum(
                    len(doc["content"]) for doc in selected
                ),
            },
            blocks=tuple(semantic_blocks),
        )

    def _candidate_paths(self, changed_files: tuple) -> dict[str, dict]:
        candidates: dict[str, dict] = {}
        scopes = tuple(
            path
            for path in changed_files
            if self._safe_changed_path(path)
        )[:_MAX_CHANGED_FILES_FOR_DISCOVERY]

        # 루트 문서는 변경 파일이 없거나 diff 경로 파싱이 실패해도 레포 공통 규칙으로 탐색한다.
        for name in DEFAULT_REFERENCE_DOCUMENTS:
            candidates[name] = {"scopes": set(scopes), "explicit": False, "depth": 0}

        # 파일마다 모든 조상을 git cat-file에 질의하면 대형 PR에서 대부분 missing인 spec이
        # 수천 개 생긴다. 여러 변경에 공통 적용되는 디렉터리, 그다음 가까운(깊은) 경로를
        # 우선해 fan-out을 고정 상한으로 둔다.
        directory_scopes: dict[PurePosixPath, set[str]] = {}
        for changed_file in scopes:
            current = PurePosixPath(".")
            for part in PurePosixPath(changed_file).parent.parts:
                current /= part
                directory_scopes.setdefault(current, set()).add(changed_file)
        selected_directories = sorted(
            directory_scopes,
            key=lambda path: (
                -len(directory_scopes[path]),
                -len(path.parts),
                path.as_posix(),
            ),
        )[:_MAX_DISCOVERY_DIRECTORIES]
        for current in selected_directories:
            for name in DEFAULT_REFERENCE_DOCUMENTS:
                rel = (current / name).as_posix()
                candidates[rel] = {
                    "scopes": directory_scopes[current],
                    "explicit": False,
                    "depth": len(current.parts),
                }
        return candidates

    @staticmethod
    def _safe_changed_path(path: str) -> bool:
        if not path or not isinstance(path, str):
            return False
        parsed = PurePosixPath(path)
        return not parsed.is_absolute() and ".." not in parsed.parts

    @staticmethod
    def _confined_relative_path(root: str, path: str) -> str | None:
        if not isinstance(path, str) or not path or "\x00" in path or ":" in path:
            return None
        try:
            candidate = path if os.path.isabs(path) else os.path.join(root, path)
            relative = os.path.relpath(candidate, root).replace(os.sep, "/")
        except (OSError, ValueError, TypeError):
            return None
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or ".." in parsed.parts:
            return None
        return parsed.as_posix()

    @staticmethod
    def _resolve_base_revision(
        root: str, base_ref: str, base_sha: str = ""
    ) -> str | None:
        if (
            not base_ref
            or base_ref.startswith("-")
            or ".." in base_ref
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref)
        ):
            return None
        # SHA가 제공됐으면 그 정확한 snapshot만 허용한다. 미도달 시 branch tip으로
        # 폴백하면 폴링 이후 base 이동에 따라 리뷰 컨텍스트가 비결정적으로 바뀐다.
        if base_sha:
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_sha):
                return None
            candidates = (base_sha,)
        else:
            candidates = (f"refs/remotes/origin/{base_ref}", base_ref)
        for candidate in candidates:
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        root,
                        "rev-parse",
                        "--verify",
                        f"{candidate}^{{commit}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return None
            if result.returncode == 0:
                return result.stdout.strip()
        return None

    @staticmethod
    def _read_git_files(
        root: str,
        revision: str,
        paths: list[str],
        *,
        cache_scope: str,
    ) -> tuple[dict[str, str | None], int]:
        """base snapshot의 여러 문서를 git cat-file 한 번으로 읽고 bounded LRU에 보관한다.
        렌더/scope는 캐시하지 않아 변경 파일별 적용 범위가 섞이지 않는다."""
        out: dict[str, str | None] = {}
        missing = []
        hits = 0
        with _GIT_DOCUMENT_CACHE_LOCK:
            for path in paths:
                key = (cache_scope, revision, path)
                if key in _GIT_DOCUMENT_CACHE:
                    out[path] = _GIT_DOCUMENT_CACHE[key]
                    _GIT_DOCUMENT_CACHE.move_to_end(key)
                    hits += 1
                else:
                    missing.append(path)
        if missing:
            specs = [f"{revision}:{path}" for path in missing]
            try:
                result = subprocess.run(
                    ["git", "-C", root, "cat-file", "--batch"],
                    input=("\n".join(specs) + "\n").encode(),
                    capture_output=True,
                    timeout=10,
                )
                parsed = StaticContextProvider._parse_cat_file_batch(
                    result.stdout if result.returncode == 0 else b"", missing
                )
            except (OSError, subprocess.TimeoutExpired, ValueError):
                parsed = {path: None for path in missing}
            with _GIT_DOCUMENT_CACHE_LOCK:
                for path in missing:
                    value = parsed.get(path)
                    out[path] = value
                    key = (cache_scope, revision, path)
                    _GIT_DOCUMENT_CACHE[key] = value
                    _GIT_DOCUMENT_CACHE.move_to_end(key)
                while len(_GIT_DOCUMENT_CACHE) > _MAX_GIT_DOCUMENT_CACHE:
                    _GIT_DOCUMENT_CACHE.popitem(last=False)
        return out, hits

    @staticmethod
    def _parse_cat_file_batch(data: bytes, paths: list[str]) -> dict[str, str | None]:
        parsed: dict[str, str | None] = {}
        offset = 0
        for path in paths:
            end = data.find(b"\n", offset)
            if end < 0:
                parsed[path] = None
                continue
            header = data[offset:end]
            offset = end + 1
            if header.endswith(b" missing"):
                parsed[path] = None
                continue
            try:
                size = int(header.rsplit(b" ", 1)[1])
            except (IndexError, ValueError):
                parsed[path] = None
                continue
            content = data[offset : offset + size]
            if len(content) != size:
                parsed[path] = None
                offset = len(data)
                continue
            offset += size
            if offset < len(data) and data[offset : offset + 1] == b"\n":
                offset += 1
            parsed[path] = content.decode("utf-8", "replace")[
                : _MAX_CHARS_PER_DOCUMENT + 1
            ]
        return parsed

    def _changed_instruction_documents(self, changed_files: tuple) -> list[str]:
        configured = None
        if self._path:
            configured = PurePosixPath(self._path.replace(os.sep, "/")).as_posix()
        changed = []
        for path in changed_files:
            if not self._safe_changed_path(path):
                continue
            parsed = PurePosixPath(path)
            if parsed.name in ("AGENTS.md", "CLAUDE.md") or path == configured:
                changed.append(path)
        return sorted(set(changed))

    @staticmethod
    def _confined_realpath(root: str, path: str) -> str | None:
        try:
            real = os.path.realpath(os.path.join(root, path))
        except (OSError, ValueError, TypeError):
            return None
        if real != root and not real.startswith(root + os.sep):
            return None
        return real

    def _select_within_budget(self, documents: list[dict]) -> list[dict]:
        # 기존 명시 문서를 보존한 뒤, 변경 파일에 가까운 문서, 루트 공통 문서 순으로 선택한다.
        ranked = sorted(
            documents,
            key=lambda d: (
                not d["explicit"],
                -d["depth"],
                d["path"] != "AGENTS.md",
                d["path"],
            ),
        )
        selected = []
        used = 0
        for doc in ranked:
            block = self._render_document(doc)
            separator = 2 if selected else 0
            if used + separator + len(block) > config.MAX_CONTEXT_CHARS_PER_SOURCE:
                continue
            selected.append(doc)
            used += separator + len(block)
        return selected

    @staticmethod
    def _render_document(doc: dict) -> str:
        scopes = sorted(doc["scopes"])
        if scopes:
            shown = scopes[:_MAX_SCOPE_PATHS]
            scope_lines = "\n".join(f"- {path}" for path in shown)
            if len(scopes) > len(shown):
                scope_lines += f"\n- … and {len(scopes) - len(shown)} more"
        else:
            scope_lines = "- repository-wide (configured document)"
        return f"### Reference: {doc['path']}\nApplies to:\n{scope_lines}\n\n{doc['content']}"
