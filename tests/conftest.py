"""Shared test setup.

The suite must be hermetic: a test that reaches the real network passes
or fails depending on where it runs and on what a third-party service
happens to hold that day. Any outbound request is therefore an error,
and a test that wants network behaviour stubs the specific call it needs.
"""

from urllib.parse import urlparse

import pytest

import requests

# The real thing, captured before any test replaces it, for the one
# fixture below that lets a request through.
_REAL_SESSION_REQUEST = requests.Session.request
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class NetworkUsedInTest(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly on any un-stubbed HTTP call.

    This caught a real bug: the album tests never stubbed the lyrics
    fetch, so on a runner with network they hit LRCLIB for real and wrote
    unexpected .lrc sidecars into the asserted library listing.
    """
    def blocked(*args, **kwargs):
        target = args[0] if args else kwargs.get("url", "?")
        raise NetworkUsedInTest(
            "test made a real HTTP request to %s - stub it instead" % (target,))

    for name in ("get", "post", "put", "delete", "head", "request"):
        monkeypatch.setattr(requests, name, blocked)
    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture
def loopback_only(monkeypatch):
    """Let requests reach a fake server this test started on 127.0.0.1.

    Opt-in, and only loopback: an in-process server is as hermetic as a
    stub, and is the only way to check the request a client actually
    sends - path, query, headers - rather than the one the test assumes.
    Anything else is still refused exactly as no_network refuses it.
    """
    def guarded(self, method, url, *args, **kwargs):
        if urlparse(url).hostname not in _LOOPBACK:
            raise NetworkUsedInTest(
                "test made a real HTTP request to %s - stub it instead" % (url,))
        return _REAL_SESSION_REQUEST(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", guarded)
    # The module-level helpers all go through Session.request, so putting
    # them back routes them through the guard above.
    for name in ("get", "post", "put", "delete", "head", "request"):
        monkeypatch.setattr(requests, name, getattr(requests.api, name))
