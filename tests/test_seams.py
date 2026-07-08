from server.seams import NoOpContextProvider, LocalIdentity


def test_context_provider_noop_returns_empty():
    assert NoOpContextProvider().gather(repo="acme/api", pr_number=7) == ""


def test_identity_is_local_me():
    assert LocalIdentity().actor == "me"
