"""Auth hardening: hashing, session tokens, throttling, migration."""

import time

import pytest
from fastapi.testclient import TestClient

from trackpull.app import create_app
from trackpull.auth import (
    LoginThrottle,
    check_session_token,
    hash_password,
    is_hashed,
    load_or_create_secret,
    make_session_token,
    verify_password,
)
from trackpull.config import Config
from trackpull.db import Store


def make_config(tmp_path, **overrides):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    defaults = dict(inbox=inbox, scratch_root=tmp_path / "scratch",
                    config_dir=tmp_path / "config", min_free_mb=0)
    defaults.update(overrides)
    return Config(**defaults)


class TestHashing:
    def test_roundtrip(self):
        stored = hash_password("hunter2")
        assert is_hashed(stored)
        assert verify_password("hunter2", stored)
        assert not verify_password("wrong", stored)

    def test_salted(self):
        assert hash_password("x") != hash_password("x")

    def test_plaintext_fallback_constant_time_path(self):
        # The TRACKPULL_PASSWORD env var arrives as plaintext.
        assert verify_password("secret", "secret")
        assert not verify_password("secret", "other")
        assert not verify_password("anything", "")

    def test_garbage_hash_rejected(self):
        assert not verify_password("x", "pbkdf2:sha256:notanumber$oops")


class TestSessionTokens:
    def test_roundtrip(self, tmp_path):
        secret = load_or_create_secret(tmp_path / "cfg")
        stored = hash_password("pw")
        token = make_session_token(secret, stored)
        assert check_session_token(token, secret, stored)

    def test_expired_token_rejected(self, tmp_path):
        secret = load_or_create_secret(tmp_path / "cfg")
        stored = hash_password("pw")
        token = make_session_token(secret, stored, ttl=-10)
        assert not check_session_token(token, secret, stored)

    def test_password_change_invalidates(self, tmp_path):
        secret = load_or_create_secret(tmp_path / "cfg")
        token = make_session_token(secret, "hash-one")
        assert not check_session_token(token, secret, "hash-two")

    def test_tampered_token_rejected(self, tmp_path):
        secret = load_or_create_secret(tmp_path / "cfg")
        stored = hash_password("pw")
        token = make_session_token(secret, stored)
        assert not check_session_token(token + "0", secret, stored)
        assert not check_session_token("garbage", secret, stored)

    def test_secret_persists(self, tmp_path):
        first = load_or_create_secret(tmp_path / "cfg")
        second = load_or_create_secret(tmp_path / "cfg")
        assert first == second


class TestThrottle:
    def test_locks_after_max_failures(self):
        throttle = LoginThrottle(window=60, max_failures=3)
        for _ in range(3):
            assert throttle.retry_after("ip1") == 0
            throttle.record_failure("ip1")
        assert throttle.retry_after("ip1") > 0
        assert throttle.retry_after("ip2") == 0  # other clients unaffected

    def test_success_clears(self):
        throttle = LoginThrottle(window=60, max_failures=2)
        throttle.record_failure("ip1")
        throttle.record_failure("ip1")
        assert throttle.retry_after("ip1") > 0
        throttle.record_success("ip1")
        assert throttle.retry_after("ip1") == 0


class TestLoginThrottleIntegration:
    def test_429_after_repeated_wrong_passwords(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={"password": "hunter2"})
            for _ in range(5):
                assert client.post("/api/login", json={"password": "no"}).status_code == 401
            locked = client.post("/api/login", json={"password": "no"})
            assert locked.status_code == 429
            assert "Retry-After" in locked.headers
            # Even the CORRECT password is refused while locked out.
            assert client.post("/api/login", json={"password": "hunter2"}).status_code == 429


class TestMigrationAndEnv:
    def test_plaintext_password_hashed_on_startup(self, tmp_path):
        config = make_config(tmp_path)
        store = Store(config.db_path)
        store.set_settings({"password": "legacy-plain"})
        store.close()
        with TestClient(create_app(config)) as client:
            stored = Store(config.db_path).get_settings()["password"]
            assert stored.startswith("pbkdf2:sha256:")
            assert client.post("/api/login", json={"password": "legacy-plain"}).status_code == 200
            assert client.get("/api/jobs").status_code == 200

    def test_env_password_works(self, tmp_path):
        config = make_config(tmp_path, password="from-env")
        with TestClient(create_app(config)) as client:
            assert client.get("/api/jobs").status_code == 401
            assert client.get("/api/jobs", headers={"X-Trackpull-Password": "from-env"}).status_code == 200
            assert client.post("/api/login", json={"password": "from-env"}).status_code == 200
            assert client.get("/api/jobs").status_code == 200


class TestSecurityHeaders:
    def test_headers_on_responses(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            response = client.get("/api/health")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Referrer-Policy"] == "no-referrer"

    def test_shell_no_cache(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/").headers["Cache-Control"] == "no-cache"
            assert client.get("/sw.js").headers["Cache-Control"] == "no-cache"
            assert client.get("/static/app.js").headers["Cache-Control"] == "no-cache"
