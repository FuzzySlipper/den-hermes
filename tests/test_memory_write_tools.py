import json
import urllib.error
from unittest.mock import patch

import pytest

from den_hermes.memory.config import DenMemoryConfig
from den_hermes.memory.provider import DenMemoryProvider


class FakeUrlopenResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def write_provider():
    config = DenMemoryConfig(
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        run_id="run-42",
        role="coder",
        task_id=1460,
    )
    return DenMemoryProvider(
        config=config,
        read_spaces=("assistant", "knowledge_base"),
        default_space="assistant",
        write_spaces=("assistant", "knowledge_base"),
        default_write_space="assistant",
    )


@pytest.fixture
def write_provider_no_default():
    config = DenMemoryConfig(
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        run_id="run-42",
        role="coder",
        task_id=1460,
    )
    return DenMemoryProvider(
        config=config,
        read_spaces=("assistant",),
        default_space="assistant",
        write_spaces=("assistant",),
        default_write_space=None,
    )


@pytest.fixture
def write_provider_single_space():
    config = DenMemoryConfig(
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        run_id="run-42",
        role="coder",
        task_id=1460,
    )
    return DenMemoryProvider(
        config=config,
        read_spaces=("assistant",),
        default_space="assistant",
        write_spaces=("assistant",),
        default_write_space="assistant",
    )


class TestNoOpHooks:
    def test_sync_turn_is_noop_with_1454_reference(self, write_provider):
        result = write_provider.sync_turn()
        assert result["status"] == "noop"
        assert "#1454" in result["reason"]

    def test_on_session_end_is_noop_with_1454_reference(self, write_provider):
        result = write_provider.on_session_end()
        assert result["status"] == "noop"
        assert "#1454" in result["reason"]

    def test_on_pre_compress_is_noop_with_1454_reference(self, write_provider):
        result = write_provider.on_pre_compress()
        assert result["status"] == "noop"
        assert "#1454" in result["reason"]

    def test_on_memory_write_is_noop_with_1454_reference(self, write_provider):
        result = write_provider.on_memory_write()
        assert result["status"] == "noop"
        assert "#1454" in result["reason"]


class TestDenStore:
    def test_store_creates_entry_with_default_space(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "slug": "test-note",
                    "space": "assistant",
                    "title": "Test Note",
                    "content": "Hello world",
                },
            ),
        ) as mock_urlopen:
            result = write_provider.den_store("Test Note", "Hello world")
            assert result["status"] == "ok"
            assert result["slug"] == "test-note"
            assert result["space"] == "assistant"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["space"] == "assistant"
            assert body["title"] == "Test Note"
            assert body["content"] == "Hello world"
            assert body["doc_type"] == "memory"

    def test_store_uses_explicit_space_override(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "slug": "kb-pattern",
                    "space": "knowledge_base",
                    "title": "Pattern",
                    "content": "Details",
                },
            ),
        ) as mock_urlopen:
            result = write_provider.den_store(
                "Pattern", "Details", space="knowledge_base"
            )
            assert result["status"] == "ok"
            assert result["space"] == "knowledge_base"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["space"] == "knowledge_base"

    def test_store_rejects_unconfigured_write_space(self, write_provider):
        result = write_provider.den_store("X", "Y", space="project")
        assert result["status"] == "permission_denied"
        assert "project" in result["error"]

    def test_store_derives_slug_from_title(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "my-test-note"}),
        ) as mock_urlopen:
            result = write_provider.den_store("My Test Note!", "content")
            assert result["slug"] == "my-test-note"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["slug"] == "my-test-note"

    def test_store_uses_explicit_slug(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "custom-slug"}),
        ) as mock_urlopen:
            result = write_provider.den_store(
                "Title", "content", slug="custom-slug"
            )
            assert result["slug"] == "custom-slug"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["slug"] == "custom-slug"

    def test_store_includes_tags_and_summary(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            write_provider.den_store(
                "T", "C", tags=["api", "v1"], summary="A summary"
            )
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["tags"] == ["api", "v1"]
            assert body["summary"] == "A summary"

    def test_store_returns_structured_error_on_500(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://test", 500, "Internal Server Error", {}, None
            ),
        ):
            result = write_provider.den_store("T", "C")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result

    def test_store_returns_structured_error_on_connection_error(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = write_provider.den_store("T", "C")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result

    def test_store_requires_meaningful_title_or_slug(self, write_provider):
        result = write_provider.den_store("!!!", "content")
        assert result["status"] == "validation_error"
        assert "slug" in result["error"].lower()

    def test_store_with_empty_write_spaces(self):
        config = DenMemoryConfig(
            project_id="den-hermes-bridge",
            requested_by="den-hermes-runner",
            run_id="run-42",
            role="coder",
        )
        provider = DenMemoryProvider(
            config=config,
            read_spaces=(),
            write_spaces=(),
            default_write_space=None,
        )
        result = provider.den_store("T", "C")
        assert result["status"] == "configuration_error"
        assert "default_write_space" in result["error"]


class TestDenUpdate:
    def test_update_replaces_existing_entry(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "slug": "api-pattern",
                    "space": "assistant",
                    "content": "Updated content",
                },
            ),
        ) as mock_urlopen:
            result = write_provider.den_update(
                "api-pattern", "Updated content", title="Updated Title"
            )
            assert result["status"] == "ok"
            assert result["slug"] == "api-pattern"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["content"] == "Updated content"
            assert body["title"] == "Updated Title"
            assert body["doc_type"] == "memory"

    def test_update_uses_explicit_space(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "x"}),
        ) as mock_urlopen:
            result = write_provider.den_update("x", "c", space="knowledge_base")
            assert result["status"] == "ok"
            assert result["space"] == "knowledge_base"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["space"] == "knowledge_base"

    def test_update_rejects_unconfigured_write_space(self, write_provider):
        result = write_provider.den_update("x", "c", space="project")
        assert result["status"] == "permission_denied"
        assert "project" in result["error"]

    def test_update_returns_structured_error_on_500(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://test", 500, "Internal Server Error", {}, None
            ),
        ):
            result = write_provider.den_update("x", "c")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result

    def test_update_returns_structured_error_on_connection_error(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = write_provider.den_update("x", "c")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result

    def test_update_includes_optional_summary_and_tags(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            write_provider.den_update(
                "s", "c", summary="new summary", tags=["updated"]
            )
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["summary"] == "new summary"
            assert body["tags"] == ["updated"]

    def test_update_single_call_no_retries(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            write_provider.den_update("s", "c")
            assert mock_urlopen.call_count == 1


class TestDefaultSpaceBehavior:
    def test_store_uses_default_write_space_when_none_specified(
        self, write_provider_single_space
    ):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            result = write_provider_single_space.den_store("T", "C")
            assert result["status"] == "ok"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["space"] == "assistant"

    def test_update_uses_default_write_space_when_none_specified(
        self, write_provider_single_space
    ):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            result = write_provider_single_space.den_update("s", "c")
            assert result["status"] == "ok"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["space"] == "assistant"

    def test_store_fails_when_no_default_write_space_and_none_given(
        self, write_provider_no_default
    ):
        result = write_provider_no_default.den_store("T", "C")
        assert result["status"] == "configuration_error"
        assert "default_write_space" in result["error"]

    def test_update_fails_when_no_default_write_space_and_none_given(
        self, write_provider_no_default
    ):
        result = write_provider_no_default.den_update("s", "c")
        assert result["status"] == "configuration_error"
        assert "default_write_space" in result["error"]


class TestExplicitSpaceOverride:
    def test_explicit_space_override_allowed_within_write_spaces(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            result = write_provider.den_store("T", "C", space="knowledge_base")
            assert result["status"] == "ok"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["space"] == "knowledge_base"

    def test_explicit_space_override_rejected_outside_write_spaces(self, write_provider):
        result = write_provider.den_store("T", "C", space="project")
        assert result["status"] == "permission_denied"


class TestProvenanceMetadata:
    def test_store_includes_provenance(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            write_provider.den_store("T", "C")
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            prov = body["provenance"]
            assert prov["profile"] == "den-hermes-runner"
            assert prov["session_id"] == "run-42"
            assert prov["task_id"] == 1460
            assert prov["project_id"] == "den-hermes-bridge"
            assert prov["run_id"] == "run-42"
            assert prov["role"] == "coder"
            assert prov["source"] == "agent-decided"
            assert "timestamp" in prov
            assert "T" in body["title"]

    def test_update_includes_fresh_provenance(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            write_provider.den_update("s", "new content")
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            prov = body["provenance"]
            assert prov["source"] == "agent-decided"
            assert prov["role"] == "coder"
            assert prov["task_id"] == 1460

    def test_provenance_timestamp_is_iso8601_utc(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"slug": "s"}),
        ) as mock_urlopen:
            write_provider.den_store("T", "C")
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            ts = body["provenance"]["timestamp"]
            assert ts.endswith("+00:00")


class TestDenOutageBehavior:
    def test_store_returns_structured_unavailable_on_connection_error(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = write_provider.den_store("T", "C")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result
            assert "results" not in result or result.get("results") == []

    def test_update_returns_structured_unavailable_on_connection_error(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = write_provider.den_update("s", "c")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result

    def test_store_does_not_retry_silently(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ) as mock_urlopen:
            write_provider.den_store("T", "C")
            assert mock_urlopen.call_count == 1

    def test_update_does_not_retry_silently(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ) as mock_urlopen:
            write_provider.den_update("s", "c")
            assert mock_urlopen.call_count == 1

    def test_store_no_destructive_fallback(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = write_provider.den_store("T", "C")
            assert result["status"] == "den_unavailable"
            assert "entry" not in result

    def test_update_no_destructive_fallback(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = write_provider.den_update("s", "c")
            assert result["status"] == "den_unavailable"
            assert "entry" not in result


class TestNoAutomaticHooksInvokeWrites:
    def test_sync_turn_does_not_call_den(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen"
        ) as mock_urlopen:
            result = write_provider.sync_turn()
            assert result["status"] == "noop"
            mock_urlopen.assert_not_called()

    def test_on_session_end_does_not_call_den(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen"
        ) as mock_urlopen:
            result = write_provider.on_session_end()
            assert result["status"] == "noop"
            mock_urlopen.assert_not_called()

    def test_on_pre_compress_does_not_call_den(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen"
        ) as mock_urlopen:
            result = write_provider.on_pre_compress()
            assert result["status"] == "noop"
            mock_urlopen.assert_not_called()

    def test_on_memory_write_does_not_call_den(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen"
        ) as mock_urlopen:
            result = write_provider.on_memory_write()
            assert result["status"] == "noop"
            mock_urlopen.assert_not_called()

    def test_prefetch_does_not_call_den(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen"
        ) as mock_urlopen:
            result = write_provider.prefetch()
            assert result["status"] == "noop"
            mock_urlopen.assert_not_called()

    def test_queue_prefetch_does_not_call_den(self, write_provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen"
        ) as mock_urlopen:
            result = write_provider.queue_prefetch()
            assert result["status"] == "noop"
            mock_urlopen.assert_not_called()
