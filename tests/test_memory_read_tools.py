import json
import urllib.error
from io import BytesIO
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
def provider():
    config = DenMemoryConfig(
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        run_id="run-42",
        role="coder",
    )
    return DenMemoryProvider(
        config=config,
        read_spaces=("assistant", "knowledge_base"),
        default_space="assistant",
    )


@pytest.fixture
def empty_provider():
    config = DenMemoryConfig(
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        run_id="run-42",
        role="coder",
    )
    return DenMemoryProvider(
        config=config,
        read_spaces=(),
        default_space=None,
    )


class TestPrefetchNoOps:
    def test_prefetch_is_noop_with_1454_reference(self, provider):
        result = provider.prefetch()
        assert result["status"] == "noop"
        assert "#1454" in result["reason"]

    def test_queue_prefetch_is_noop_with_1454_reference(self, provider):
        result = provider.queue_prefetch()
        assert result["status"] == "noop"
        assert "#1454" in result["reason"]


class TestDenSearch:
    def test_search_uses_configured_read_spaces(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "results": [
                        {
                            "space": "assistant",
                            "slug": "note-1",
                            "summary": "s1",
                            "content": "c1",
                        }
                    ]
                },
            ),
        ) as mock_urlopen:
            result = provider.den_search("test query")
            assert result["status"] == "ok"
            assert result["count"] == 1
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["spaces"] == ["assistant", "knowledge_base"]

    def test_search_with_explicit_space_override(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"results": []}),
        ) as mock_urlopen:
            result = provider.den_search("test", space="knowledge_base")
            assert result["status"] == "ok"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["spaces"] == ["knowledge_base"]

    def test_search_rejects_unconfigured_space(self, provider):
        result = provider.den_search("test", space="project")
        assert result["status"] == "permission_denied"
        assert "project" in result["error"]

    def test_search_returns_structured_error_on_500(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://test", 500, "Internal Server Error", {}, None
            ),
        ):
            result = provider.den_search("test")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result
            assert "500" in result["error"]

    def test_search_returns_structured_error_on_403(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://test", 403, "Forbidden", {}, None
            ),
        ):
            result = provider.den_search("test")
            assert result["status"] == "permission_denied"
            assert "recovery" in result

    def test_search_source_labels_results(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "results": [
                        {
                            "space": "assistant",
                            "slug": "api-pattern",
                            "summary": "Retry pattern",
                            "content": "Use backoff",
                        }
                    ]
                },
            ),
        ):
            result = provider.den_search("retry")
            entry = result["results"][0]
            assert entry["space"] == "assistant"
            assert entry["slug"] == "api-pattern"
            assert entry["summary"] == "Retry pattern"
            assert entry["content"] == "Use backoff"

    def test_search_with_empty_read_spaces(self, empty_provider):
        result = empty_provider.den_search("test")
        assert result["status"] == "configuration_error"
        assert "read_spaces" in result["error"]


class TestDenRecall:
    def test_recall_uses_search_endpoint(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"results": []}),
        ) as mock_urlopen:
            provider.den_recall("pattern")
            req = mock_urlopen.call_args[0][0]
            assert req.get_full_url().endswith("/memory/search")

    def test_recall_filters_by_tags_client_side(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "results": [
                        {
                            "space": "assistant",
                            "slug": "a",
                            "summary": "",
                            "content": "",
                            "metadata": {"tags": ["api"]},
                        },
                        {
                            "space": "assistant",
                            "slug": "b",
                            "summary": "",
                            "content": "",
                            "metadata": {"tags": ["ui"]},
                        },
                    ]
                },
            ),
        ):
            result = provider.den_recall("pattern", tags=["api"])
            assert result["count"] == 1
            assert result["results"][0]["slug"] == "a"

    def test_recall_with_list_response(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                [
                    {"space": "assistant", "slug": "x", "summary": "xs"},
                ],
            ),
        ):
            result = provider.den_recall("pattern")
            assert result["count"] == 1
            assert result["results"][0]["slug"] == "x"

    def test_recall_respects_limit(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"results": []}),
        ) as mock_urlopen:
            provider.den_recall("pattern", limit=5)
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["limit"] == 5


class TestDenRead:
    def test_read_single_doc_fetch(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "space": "assistant",
                    "slug": "api-pattern",
                    "summary": "Pattern",
                    "content": "Details",
                },
            ),
        ):
            result = provider.den_read("api-pattern")
            assert result["status"] == "ok"
            assert result["entry"]["slug"] == "api-pattern"
            assert result["entry"]["space"] == "assistant"

    def test_read_with_explicit_space(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, {"space": "knowledge_base", "slug": "kb-1"}),
        ) as mock_urlopen:
            result = provider.den_read("kb-1", space="knowledge_base")
            assert result["status"] == "ok"
            req = mock_urlopen.call_args[0][0]
            assert "space=knowledge_base" in req.get_full_url()

    def test_read_without_space_searches_configured_spaces_until_found(self, provider):
        side_effects = [
            urllib.error.HTTPError("http://test", 404, "Not Found", {}, None),
            FakeUrlopenResponse(200, {"slug": "kb-1", "summary": "KB"}),
        ]
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=side_effects,
        ) as mock_urlopen:
            result = provider.den_read("kb-1")
            assert result["status"] == "ok"
            assert result["entry"]["space"] == "knowledge_base"
            urls = [call.args[0].get_full_url() for call in mock_urlopen.call_args_list]
            assert "space=assistant" in urls[0]
            assert "space=knowledge_base" in urls[1]

    def test_read_rejects_unconfigured_space(self, provider):
        result = provider.den_read("anything", space="project")
        assert result["status"] == "permission_denied"

    def test_read_returns_not_found_on_404(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://test", 404, "Not Found", {}, None
            ),
        ):
            result = provider.den_read("missing")
            assert result["status"] == "not_found"
            assert "recovery" in result

    def test_read_returns_structured_error_on_500(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://test", 500, "Internal Server Error", {}, None
            ),
        ):
            result = provider.den_read("anything")
            assert result["status"] == "den_unavailable"


class TestDenListMyMemories:
    def test_list_queries_all_read_spaces_when_no_space_given(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, []),
        ) as mock_urlopen:
            result = provider.den_list_my_memories()
            assert result["status"] == "ok"
            assert mock_urlopen.call_count == 2
            urls = [call.args[0].get_full_url() for call in mock_urlopen.call_args_list]
            assert any("space=assistant" in u for u in urls)
            assert any("space=knowledge_base" in u for u in urls)

    def test_list_with_explicit_space_queries_once(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(200, [{"slug": "x", "space": "assistant"}]),
        ) as mock_urlopen:
            result = provider.den_list_my_memories(space="assistant")
            assert result["status"] == "ok"
            assert mock_urlopen.call_count == 1
            assert "space=assistant" in mock_urlopen.call_args[0][0].get_full_url()

    def test_list_merges_results_from_multiple_spaces(self, provider):
        side_effects = [
            FakeUrlopenResponse(200, [{"slug": "a", "space": "assistant", "summary": "sa"}]),
            FakeUrlopenResponse(200, [{"slug": "b", "space": "knowledge_base", "summary": "sb"}]),
        ]
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=side_effects,
        ) as mock_urlopen:
            result = provider.den_list_my_memories()
            assert result["status"] == "ok"
            assert result["count"] == 2
            slugs = {r["slug"] for r in result["results"]}
            assert slugs == {"a", "b"}

    def test_list_gracefully_skips_failing_space(self, provider):
        side_effects = [
            urllib.error.HTTPError("http://test", 500, "Error", {}, None),
            FakeUrlopenResponse(200, [{"slug": "b", "space": "knowledge_base"}]),
        ]
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=side_effects,
        ):
            result = provider.den_list_my_memories()
            assert result["status"] == "ok"
            assert result["count"] == 1
            assert result["results"][0]["slug"] == "b"

    def test_list_with_empty_read_spaces(self, empty_provider):
        result = empty_provider.den_list_my_memories()
        assert result["status"] == "configuration_error"


class TestDenOutageBehavior:
    def test_search_returns_structured_unavailable_on_connection_error(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = provider.den_search("query")
            assert result["status"] == "den_unavailable"
            assert "recovery" in result
            assert "Den Core REST" in result["error"]

    def test_recall_returns_structured_unavailable_on_connection_error(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = provider.den_recall("query")
            assert result["status"] == "den_unavailable"

    def test_read_returns_structured_unavailable_on_connection_error(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = provider.den_read("slug")
            assert result["status"] == "den_unavailable"

    def test_list_returns_structured_unavailable_when_all_spaces_fail(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = provider.den_list_my_memories()
            assert result["status"] == "den_unavailable"
            assert "recovery" in result
            assert "results" not in result

    def test_never_fabricates_content(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = provider.den_search("anything")
            assert result["status"] == "den_unavailable"
            assert "results" not in result or result.get("results") == []


class TestNoAutomaticRetrieval:
    def test_provider_does_not_auto_prefetch(self, provider):
        assert provider.prefetch()["status"] == "noop"
        assert provider.queue_prefetch()["status"] == "noop"

    def test_tools_return_source_labels(self, provider):
        with patch(
            "den_hermes.memory.rest_client.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(
                200,
                {
                    "results": [
                        {
                            "space": "knowledge_base",
                            "slug": "deploy-checklist",
                            "summary": "Pre-deploy steps",
                            "content": "Run tests",
                        }
                    ]
                },
            ),
        ):
            result = provider.den_search("deploy")
            entry = result["results"][0]
            assert "space" in entry
            assert "slug" in entry
            assert "summary" in entry
