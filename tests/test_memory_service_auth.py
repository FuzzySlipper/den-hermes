import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from den_hermes.memory.config import DenMemoryConfig
from den_hermes.memory.rest_client import DenMemoryRestClient


class TestDenMemoryConfigTokenResolution:
    def test_config_resolves_token_from_env(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "env-token-abc123")
        config = DenMemoryConfig()
        assert config.resolve_token() == "env-token-abc123"

    def test_config_resolves_token_from_file(self, tmp_path):
        cred_file = tmp_path / "token.txt"
        cred_file.write_text("file-token-xyz789\n")
        config = DenMemoryConfig(credential_path=str(cred_file))
        assert config.resolve_token() == "file-token-xyz789"

    def test_config_prefers_env_over_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "env-token-abc123")
        cred_file = tmp_path / "token.txt"
        cred_file.write_text("file-token-xyz789\n")
        config = DenMemoryConfig(credential_path=str(cred_file))
        assert config.resolve_token() == "env-token-abc123"

    def test_config_returns_none_when_no_token(self):
        # Ensure env is not set and no credential path
        with patch.dict(os.environ, {}, clear=True):
            config = DenMemoryConfig(credential_path=None)
            assert config.resolve_token() is None

    def test_config_returns_none_for_missing_credential_file(self, tmp_path):
        missing = tmp_path / "missing.txt"
        config = DenMemoryConfig(credential_path=str(missing))
        assert config.resolve_token() is None

    def test_config_repr_redacts_env_token(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "super-secret-bearer-token")
        config = DenMemoryConfig()
        rep = repr(config)
        assert "super-secret" not in rep
        assert "[REDACTED]" in rep
        assert "http://192.168.1.10:5000" in rep  # base_url visible

    def test_config_repr_redacts_file_token(self, tmp_path, monkeypatch):
        # Clear env so file token is used
        monkeypatch.delenv("DEN_CORE_API_TOKEN", raising=False)
        cred_file = tmp_path / "token.txt"
        cred_file.write_text("file-secret-token\n")
        config = DenMemoryConfig(credential_path=str(cred_file))
        rep = repr(config)
        assert "file-secret" not in rep
        assert "[REDACTED]" in rep

    def test_config_str_is_redaction_safe(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "sk-den-memory-12345")
        config = DenMemoryConfig()
        text = str(config)
        assert "sk-den-memory" not in text
        assert "[REDACTED]" in text


class TestDenMemoryRestClientAuthHeaders:
    def test_client_injects_auth_headers_when_token_present(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "service-token-42")
        config = DenMemoryConfig(
            project_id="den-hermes-bridge",
            requested_by="den-hermes-runner",
            run_id="run-42",
            role="coder",
        )
        client = DenMemoryRestClient(config)
        headers = client._build_headers()
        assert headers["Authorization"] == "Bearer service-token-42"
        assert headers["X-Den-Service-Token"] == "service-token-42"

    def test_client_omits_auth_headers_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("DEN_CORE_API_TOKEN", raising=False)
        config = DenMemoryConfig(
            project_id="den-hermes-bridge",
            requested_by="den-hermes-runner",
            run_id="run-42",
            role="coder",
        )
        client = DenMemoryRestClient(config)
        headers = client._build_headers()
        assert "Authorization" not in headers
        assert "X-Den-Service-Token" not in headers

    def test_client_includes_required_den_headers(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "tok")
        config = DenMemoryConfig(
            project_id="den-hermes-bridge",
            requested_by="den-hermes-runner",
            run_id="run-42",
            role="coder",
        )
        client = DenMemoryRestClient(config)
        headers = client._build_headers()
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"
        assert headers["X-Den-Project-Id"] == "den-hermes-bridge"
        assert headers["X-Den-Requested-By"] == "den-hermes-runner"
        assert headers["X-Den-Run-Id"] == "run-42"
        assert headers["X-Den-Role"] == "coder"

    def test_client_allows_loopback_without_token(self, monkeypatch):
        monkeypatch.delenv("DEN_CORE_API_TOKEN", raising=False)
        config = DenMemoryConfig(
            base_url="http://127.0.0.1:5000",
            project_id="den-hermes-bridge",
            requested_by="den-hermes-runner",
            role="coder",
        )
        client = DenMemoryRestClient(config)
        headers = client._build_headers()
        assert "Authorization" not in headers
        assert "X-Den-Service-Token" not in headers
        # No exception; client is usable in unauth mode

    def test_client_repr_redacts_token(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "hidden-token-123")
        config = DenMemoryConfig(project_id="p", requested_by="r", role="coder")
        client = DenMemoryRestClient(config)
        rep = repr(client)
        assert "hidden-token" not in rep
        assert "[REDACTED]" in rep

    def test_client_no_fake_token_on_missing(self, monkeypatch):
        monkeypatch.delenv("DEN_CORE_API_TOKEN", raising=False)
        config = DenMemoryConfig(project_id="p", requested_by="r", role="coder")
        client = DenMemoryRestClient(config)
        assert client._token is None
        headers = client._build_headers()
        # No placeholder/fake token injected
        for key in headers:
            assert "fake" not in headers[key].lower()
            assert "placeholder" not in headers[key].lower()


class TestDenMemoryRestClientStubRequests:
    def test_get_stub_builds_correct_url_and_headers(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "tok")
        config = DenMemoryConfig(
            base_url="http://192.168.1.10:5000",
            project_id="den-hermes-bridge",
            requested_by="runner",
            role="coder",
        )
        client = DenMemoryRestClient(config)
        # _build_request is a small helper we can test
        method, url, headers, body = client._build_request(
            "GET", "/api/v1/projects/den-hermes-bridge/memory/spaces"
        )
        assert method == "GET"
        assert url == "http://192.168.1.10:5000/api/v1/projects/den-hermes-bridge/memory/spaces"
        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-Den-Service-Token"] == "tok"

    def test_post_stub_builds_json_body(self, monkeypatch):
        monkeypatch.setenv("DEN_CORE_API_TOKEN", "tok")
        config = DenMemoryConfig(
            base_url="http://192.168.1.10:5000",
            project_id="den-hermes-bridge",
            requested_by="runner",
            role="coder",
        )
        client = DenMemoryRestClient(config)
        method, url, headers, body = client._build_request(
            "POST",
            "/api/v1/projects/den-hermes-bridge/memory/entries",
            json_payload={"key": "test", "space": "task"},
        )
        assert method == "POST"
        assert body is not None
        assert "test" in body
        assert headers["Content-Type"] == "application/json"
