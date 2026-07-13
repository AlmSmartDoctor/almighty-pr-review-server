from server.context.base import ContextRequest
from server.seams import NoOpContextProvider, LocalIdentity, NullMemoryStore


def test_context_provider_noop_returns_empty():
    assert (
        NoOpContextProvider().gather(req=ContextRequest(repo="acme/api", pr_number=7))
        == ""
    )


def test_identity_is_local_me():
    assert LocalIdentity().actor == "me"


def test_memory_store_record_is_noop():
    assert NullMemoryStore().record(event="reviewed", payload={}) is None
