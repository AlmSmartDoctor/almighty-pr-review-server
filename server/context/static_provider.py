import os

from server import config
from server.context.base import ContextRequest, ContextResult


class StaticContextProvider:
    """로컬 참조 파일(.md 등)을 읽어 주입하는 첫 concrete provider(외부의존 0).
    B-INV-9: path는 root 하위로 제한(realpath containment) — 임의 절대경로 exfil 차단."""

    name = "static"

    def __init__(self, *, path: str, root: str):
        self._path = path
        self._root = root

    def fetch(self, req: ContextRequest) -> ContextResult:
        # 봉쇄 root: 요청의 PR-head worktree 우선, 없으면 생성자 root(local_path) 폴백.
        # 상대경로 path도 root 기준으로 해석(UI가 상대경로를 저장; read_confined와 동일 계약).
        root_src = req.workdir or self._root
        try:
            root = os.path.realpath(root_src)
            real = os.path.realpath(os.path.join(root_src, self._path))
            if real != root and not real.startswith(root + os.sep):
                return ContextResult(
                    provider=self.name,
                    status="error",
                    text="",
                    error="path outside allowed root",
                )
            with open(real, encoding="utf-8") as f:
                content = f.read(config.MAX_CONTEXT_CHARS_PER_SOURCE + 1)
        except (OSError, ValueError, TypeError):
            # 파일 없음/권한/None root 등 → best-effort degrade
            return ContextResult(provider=self.name, status="empty", text="")
        status = "ok" if content.strip() else "empty"
        return ContextResult(provider=self.name, status=status, text=content)
