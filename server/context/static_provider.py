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
        try:
            real = os.path.realpath(self._path)
            root = os.path.realpath(self._root)
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
