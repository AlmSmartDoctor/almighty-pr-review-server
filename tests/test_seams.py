from server.context.base import ContextRequest
from server.seams import NoOpContextProvider


def test_context_provider_noop_returns_empty():
    assert (
        NoOpContextProvider().gather(req=ContextRequest(repo="acme/api", pr_number=7))
        == ""
    )
