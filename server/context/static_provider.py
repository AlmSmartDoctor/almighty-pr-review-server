import os
import re
import subprocess
from pathlib import PurePosixPath

from server import config
from server.context.base import ContextRequest, ContextResult


DEFAULT_REFERENCE_DOCUMENTS = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")
_MAX_CHARS_PER_DOCUMENT = 3_000
_MAX_SCOPE_PATHS = 10


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
        base_revision = self._resolve_base_revision(root, req.base_ref)
        if req.base_ref and base_revision is None:
            return ContextResult(
                provider=self.name,
                status="error",
                text="",
                error="base reference unavailable",
            )

        documents: dict[str, dict] = {}
        rejected_explicit = False
        for path, meta in candidates.items():
            display_path = self._confined_relative_path(root, path)
            if display_path is None:
                rejected_explicit = rejected_explicit or bool(meta["explicit"])
                continue
            if base_revision:
                content = self._read_git_file(root, base_revision, display_path)
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
            if len(content) > _MAX_CHARS_PER_DOCUMENT:
                content = content[:_MAX_CHARS_PER_DOCUMENT] + "\n…[document truncated]"

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
        blocks = []
        if changed_instructions and base_revision:
            blocks.append(
                "⚠ This PR changes repository instruction documents. "
                "The trusted base-branch versions are used for this review:\n"
                + "\n".join(f"- {path}" for path in changed_instructions)
            )
        blocks.extend(self._render_document(doc) for doc in selected)
        text = "\n\n".join(blocks)
        return ContextResult(
            provider=self.name,
            status="ok" if text.strip() else "empty",
            text=text,
            meta={
                "documents": [doc["path"] for doc in selected],
                "revision": base_revision or "worktree",
                "changed_instruction_documents": changed_instructions,
            },
        )

    def _candidate_paths(self, changed_files: tuple) -> dict[str, dict]:
        candidates: dict[str, dict] = {}
        scopes = tuple(path for path in changed_files if self._safe_changed_path(path))

        # 루트 문서는 변경 파일이 없거나 diff 경로 파싱이 실패해도 레포 공통 규칙으로 탐색한다.
        for name in DEFAULT_REFERENCE_DOCUMENTS:
            candidates[name] = {"scopes": set(scopes), "explicit": False, "depth": 0}

        for changed_file in scopes:
            parent_parts = PurePosixPath(changed_file).parent.parts
            current = PurePosixPath(".")
            for part in parent_parts:
                current /= part
                for name in DEFAULT_REFERENCE_DOCUMENTS:
                    rel = (current / name).as_posix()
                    candidates.setdefault(
                        rel,
                        {
                            "scopes": set(),
                            "explicit": False,
                            "depth": len(current.parts),
                        },
                    )["scopes"].add(changed_file)
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
    def _resolve_base_revision(root: str, base_ref: str) -> str | None:
        if (
            not base_ref
            or base_ref.startswith("-")
            or ".." in base_ref
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref)
        ):
            return None
        for candidate in (f"refs/remotes/origin/{base_ref}", base_ref):
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
    def _read_git_file(root: str, revision: str, path: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", root, "show", f"{revision}:{path}"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", "replace")[: _MAX_CHARS_PER_DOCUMENT + 1]

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
