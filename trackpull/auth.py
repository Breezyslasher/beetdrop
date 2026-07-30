"""Password hashing, session tokens, and login throttling.

Built for the internet-facing case (a Cloudflare tunnel in front): the
password is stored as a salted PBKDF2 hash, verification is
constant-time, browsers hold a signed HttpOnly session cookie instead of
the password itself, and failed logins are throttled per client.

The password never travels in a query string: query strings end up in
access logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from pathlib import Path

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 30 * 86400
THROTTLE_WINDOW_SECONDS = 900
THROTTLE_MAX_FAILURES = 5

_HASH_PREFIX = "pbkdf2:sha256:"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "%s%d$%s$%s" % (_HASH_PREFIX, PBKDF2_ITERATIONS, _b64(salt), _b64(digest))


def is_hashed(stored: str) -> bool:
    return stored.startswith(_HASH_PREFIX)


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification against either a PBKDF2 hash or a
    plaintext value (the TRACKPULL_PASSWORD environment variable arrives
    as plaintext and cannot be pre-hashed)."""
    if not stored:
        return False
    if is_hashed(stored):
        try:
            params, salt_text, digest_text = stored[len(_HASH_PREFIX):].split("$")
            iterations = int(params)
            salt = _unb64(salt_text)
            expected = _unb64(digest_text)
        except (ValueError, TypeError):
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(candidate, expected)
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))


def load_or_create_secret(config_dir: Path) -> bytes:
    """Server secret for signing session tokens, persisted so sessions
    survive restarts."""
    config_dir.mkdir(parents=True, exist_ok=True)
    secret_file = config_dir / "secret.key"
    if secret_file.is_file():
        data = secret_file.read_bytes()
        if len(data) >= 32:
            return data
    data = secrets.token_bytes(32)
    secret_file.touch(mode=0o600, exist_ok=True)
    secret_file.write_bytes(data)
    return data


def _signing_key(secret: bytes, password_stored: str) -> bytes:
    # The stored password (hash) is part of the key material, so changing
    # the password invalidates every outstanding session.
    return hashlib.sha256(secret + password_stored.encode("utf-8")).digest()


def make_session_token(secret: bytes, password_stored: str,
                       ttl: int = SESSION_TTL_SECONDS) -> str:
    expiry = str(int(time.time()) + ttl)
    signature = hmac.new(_signing_key(secret, password_stored),
                         expiry.encode("ascii"), hashlib.sha256).hexdigest()
    return "%s.%s" % (expiry, signature)


def check_session_token(token: str, secret: bytes, password_stored: str) -> bool:
    try:
        expiry_text, signature = token.split(".", 1)
        expiry = int(expiry_text)
    except (ValueError, AttributeError):
        return False
    if expiry < time.time():
        return False
    expected = hmac.new(_signing_key(secret, password_stored),
                        expiry_text.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


class LoginThrottle:
    """Sliding-window lockout for failed logins, keyed per client."""

    def __init__(self, window: float = THROTTLE_WINDOW_SECONDS,
                 max_failures: int = THROTTLE_MAX_FAILURES):
        self._window = window
        self._max = max_failures
        self._failures: dict[str, deque] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        """Seconds until this client may try again; 0 when allowed."""
        now = time.monotonic()
        with self._lock:
            entries = self._failures.get(key)
            if not entries:
                return 0
            while entries and now - entries[0] > self._window:
                entries.popleft()
            if len(entries) < self._max:
                return 0
            return max(1, int(self._window - (now - entries[0])))

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, deque()).append(time.monotonic())

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
