"""UI shell serving: routes, content types, and auth exemption."""

import json

import pytest
from fastapi.testclient import TestClient

from beetdrop.app import create_app
from beetdrop.config import Config


@pytest.fixture
def client(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = Config(
        music_root=inbox,
        scratch_root=tmp_path / "scratch",
        config_dir=tmp_path / "config",
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


class TestShell:
    def test_index_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Beetdrop" in response.text
        assert "app.js" in response.text

    def test_manifest(self, client):
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        manifest = json.loads(response.text)
        assert manifest["display"] == "standalone"
        assert manifest["theme_color"]
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes
        assert all("maskable" in icon["purpose"] for icon in manifest["icons"])

    def test_service_worker_at_root_scope(self, client):
        response = client.get("/sw.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        # Shell only - the worker must never cache API responses.
        assert "/api/" in response.text

    def test_static_assets(self, client):
        assert client.get("/static/style.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/icons/icon-192.png").status_code == 200
        assert client.get("/static/icons/icon-512.png").status_code == 200

    def test_shell_open_when_password_set(self, client):
        client.put("/api/settings", json={"password": "hunter2"})
        # The page is what asks for the password; the API stays locked.
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/manifest.webmanifest").status_code == 200
        assert client.get("/api/jobs").status_code == 401
