from server.context.composite import CompositeContextProvider


def _effective(repo, settings, key):
    """per-repo 값 우선(NULL=global 상속), 양측 다 없으면 0(off). (D3)"""
    v = repo[key] if key in repo.keys() else None
    if v is not None:
        return v
    return settings[key] if key in settings.keys() else 0


def build_context_provider(repo, settings):
    """활성 프로바이더를 조립. 생성은 절대 예외를 밖으로 던지지 않는다(B-INV-4/D6).
    B1 시점엔 등록된 프로바이더가 없어 빈 Composite를 반환(gather→'')."""
    providers = []
    # B4부터: 활성 프로바이더를 try/except로 생성해 providers.append (실패=드롭)
    return CompositeContextProvider(providers)
