# Fixture: violates Invariant 2 on purpose, from inside a function body — the
# shape a deferred import would take if someone tried to hide one.


def fetch():
    import httpx

    return httpx
