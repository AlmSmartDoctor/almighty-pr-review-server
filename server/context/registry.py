from server.context.base import redact_secrets
from server.context.composite import CompositeContextProvider
from server.context.static_provider import StaticContextProvider


def _effective(repo, settings, key):
    """per-repo 값 우선(NULL=global 상속), 양측 다 없으면 0(off). (D3)"""
    v = repo[key] if key in repo.keys() else None
    if v is not None:
        return v
    return settings[key] if key in settings.keys() else 0


def _ref(repo, key):
    return repo[key] if key in repo.keys() else None


def build_context_provider(repo, settings):
    """활성 프로바이더 조립. 생성은 절대 예외를 밖으로 던지지 않는다(B-INV-4/D6)."""
    providers = []
    if _effective(repo, settings, "context_static_on") and _ref(
        repo, "static_context_path"
    ):
        try:
            providers.append(
                StaticContextProvider(
                    path=_ref(repo, "static_context_path"), root=repo["local_path"]
                )
            )
        except Exception as e:  # 생성 실패 = 드롭+로그, never raise
            print(f"[context] static provider skipped: {redact_secrets(str(e))}")
    return CompositeContextProvider(providers)
