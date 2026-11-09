"""Sign in to Plex with a PIN, and pick the server to scan.

The token a scan needs used to be dug out by hand - open a track in
Plex, Get Info, View XML, copy the end of the address - and the server's
address typed in. Plex's own link flow does both:

1. Beetdrop asks plex.tv for a PIN and shows its four-character code.
2. The person enters the code at plex.tv/link, signed in as themselves.
3. Beetdrop polls the PIN until plex.tv attaches an account token to it.
4. With that token it lists the account's servers and every address each
   can be reached at, and tries them in turn from where Beetdrop runs.

The requests are the ones Vita Plex sends, which already work against
plex.tv: a form-encoded POST of strong=false for a short code, polling
by PIN id, and /api/v2/resources for servers.

Two rules:

- Only servers the account owns can be chosen. A library refresh needs
  owner rights, so a server merely shared with the account would refuse
  every scan. Shared ones are listed, with that reason, rather than
  hidden - a missing server is more confusing than a greyed-out one.
- The account token stays in memory, held against the PIN only until a
  server is chosen, and is never sent to the browser. What is saved is
  that server's own access token, which is all a scan uses.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import __version__
from .plex import PlexError, sections

PLEX_TV = "https://plex.tv"
LINK_URL = "https://plex.tv/link"
PRODUCT = "Beetdrop"
TIMEOUT = 10
# Long enough to try a LAN address that is not answering, short enough
# that four dead ones do not leave the page waiting half a minute.
PROBE_TIMEOUT = 4
# A PIN plex.tv has given out lives about this long; one that was never
# linked is forgotten after it, so the table cannot grow without bound.
PENDING_TTL = 30 * 60


def headers(client_id: str, token: str = "") -> dict:
    """What plex.tv wants to know about the device asking.

    The client identifier is what makes "Beetdrop" one entry in the
    account's Authorized Devices rather than a new one per sign-in, so it
    is stored and reused, never generated per request.
    """
    found = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Device": PRODUCT,
        "X-Plex-Device-Name": PRODUCT,
        "X-Plex-Platform": "Web",
    }
    if token:
        found["X-Plex-Token"] = token
    return found


def _plex_tv(method: str, path: str, client_id: str, token: str = "",
             data: Optional[dict] = None):
    try:
        response = requests.request(
            method, PLEX_TV + path, data=data, timeout=TIMEOUT,
            headers=headers(client_id, token))
    except requests.RequestException as exc:
        raise PlexError("could not reach plex.tv (%s)" % exc.__class__.__name__) from exc
    if response.status_code == 401:
        raise PlexError("plex.tv refused the sign-in")
    if response.status_code == 404:
        return None
    if not response.ok:
        raise PlexError("plex.tv answered HTTP %d" % response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise PlexError("plex.tv sent something that is not JSON") from exc


@dataclass
class Connection:
    uri: str
    local: bool = False
    relay: bool = False


@dataclass
class Server:
    id: str
    name: str
    owned: bool
    access_token: str = field(repr=False)
    version: str = ""
    shared_by: str = ""
    connections: list = field(default_factory=list)

    def public(self) -> dict:
        """What the page may see: everything but the token."""
        return {"id": self.id, "name": self.name, "owned": self.owned,
                "version": self.version, "shared_by": self.shared_by,
                "addresses": [c.uri for c in self.connections]}


def _order(connections: list) -> list:
    """Local first, then direct remote, relay last - the order a Plex
    client tries them in. Beetdrop usually sits on the same network as
    the server, and the relay is Plex's bandwidth-capped fallback."""
    return sorted(connections, key=lambda c: (not c.local, c.relay))


def servers(token: str, client_id: str) -> list:
    """The account's Plex Media Servers, owned ones first."""
    body = _plex_tv("GET", "/api/v2/resources?includeHttps=1&includeRelay=1"
                    "&includeIPv6=0", client_id, token) or []
    found = []
    for resource in body if isinstance(body, list) else []:
        if "server" not in (resource.get("provides") or "").split(","):
            continue
        found.append(Server(
            id=resource.get("clientIdentifier", ""),
            name=resource.get("name", "") or "Plex Media Server",
            owned=bool(resource.get("owned")),
            access_token=resource.get("accessToken") or token,
            version=resource.get("productVersion", ""),
            shared_by=resource.get("sourceTitle") or "",
            connections=_order([
                Connection(c.get("uri", ""), bool(c.get("local")), bool(c.get("relay")))
                for c in resource.get("connections") or [] if c.get("uri")]),
        ))
    return sorted(found, key=lambda s: (not s.owned, s.name.casefold()))


def reachable_address(server: Server) -> tuple:
    """The first of the server's addresses Beetdrop can actually use.

    Tried from here, not from the browser: what matters is whether the
    machine Beetdrop runs on can reach Plex, and a phone on the same
    Wi-Fi is no evidence of that. Returns (address, reasons tried).
    """
    tried = []
    for connection in server.connections:
        try:
            sections(connection.uri, server.access_token, fresh=True,
                     timeout=PROBE_TIMEOUT)
            return connection.uri, tried
        except PlexError as exc:
            tried.append("%s: %s" % (connection.uri, exc))
    return "", tried


@dataclass
class _Pending:
    code: str
    created: float
    token: str = field(default="", repr=False)


class SignIn:
    """The PINs this Beetdrop handed out, and the account token each one
    earned, until a server is chosen.

    In memory on purpose. An account token is the whole Plex account, and
    there is no reason for it to outlive the minute between linking and
    picking a server. A restart in that minute costs a fresh code.

    Only PINs created here are polled, so the endpoint cannot be used to
    ask plex.tv about anybody else's.
    """

    def __init__(self):
        self._pending: dict = {}
        self._lock = threading.Lock()

    def _sweep(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL
        for pin_id in [k for k, v in self._pending.items() if v.created < cutoff]:
            del self._pending[pin_id]

    def start(self, client_id: str) -> dict:
        body = _plex_tv("POST", "/api/v2/pins", client_id, data={"strong": "false"})
        if not body or not body.get("code"):
            raise PlexError("plex.tv did not give out a code")
        pin_id = str(body["id"])
        with self._lock:
            self._sweep()
            self._pending[pin_id] = _Pending(code=body["code"], created=time.monotonic())
        return {"id": pin_id, "code": body["code"], "link": LINK_URL,
                "expires_in": int(body.get("expiresIn") or 0)}

    def poll(self, pin_id: str, client_id: str) -> dict:
        """Has the code been entered yet? Once it has, the servers."""
        with self._lock:
            self._sweep()
            pending = self._pending.get(pin_id)
        if pending is None:
            return {"state": "unknown"}
        if not pending.token:
            body = _plex_tv("GET", "/api/v2/pins/%s" % pin_id, client_id)
            if body is None or body.get("expired"):
                with self._lock:
                    self._pending.pop(pin_id, None)
                return {"state": "expired"}
            if not body.get("authToken"):
                return {"state": "waiting"}
            pending.token = body["authToken"]
        return {"state": "linked",
                "servers": [s.public() for s in servers(pending.token, client_id)]}

    def choose(self, pin_id: str, server_id: str, client_id: str) -> dict:
        """Settle on one server: find an address that answers, and hand
        back what to save. The account token is forgotten here either
        way it goes."""
        with self._lock:
            pending = self._pending.get(pin_id)
        if pending is None or not pending.token:
            raise PlexError("that sign-in has expired - start again")
        chosen = next((s for s in servers(pending.token, client_id)
                       if s.id == server_id), None)
        if chosen is None:
            raise PlexError("that server is no longer on the account")
        if not chosen.owned:
            raise PlexError("%s is shared with this account, and only its owner "
                            "can ask it to scan" % chosen.name)
        address, tried = reachable_address(chosen)
        if not address:
            raise PlexError("none of %s's addresses answered from here (%s)"
                            % (chosen.name, "; ".join(tried) or "it lists none"))
        with self._lock:
            self._pending.pop(pin_id, None)
        return {"url": address, "token": chosen.access_token, "name": chosen.name}


def new_client_id() -> str:
    return "beetdrop-" + uuid.uuid4().hex
