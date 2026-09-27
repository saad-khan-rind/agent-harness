"""InnKube client: request shaping and response normalisation.

No network access. The SDK call itself is replaced; what is under test is
the code around it — the part that decides what goes on the wire and what
the loop gets back.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from harness.config import Settings
from harness.llm.openai_compatible import OpenAICompatibleClient
from harness.llm.protocol import LLMError
from openai import APIConnectionError, APIStatusError, APITimeoutError


def _settings() -> Settings:
    """Build settings without touching the environment or config.yaml."""
    return Settings(
        llm_api_key="test-key",
        llm_base_url="https://example.invalid",
        llm_model="test-model",
        temperature=0.3,
    )


def _client_with(
    response: Any = None, error: Exception | None = None
) -> tuple[OpenAICompatibleClient, list[dict[str, Any]]]:
    """Return a client whose SDK call is stubbed, plus a request recorder.

    Args:
        response: Object to return from ``chat.completions.create``.
        error: Exception to raise instead.

    Returns:
        The client and a list that receives each request kwargs dict.
    """
    client = OpenAICompatibleClient(_settings())
    seen: list[dict[str, Any]] = []

    def _create(**kwargs: Any) -> Any:
        seen.append(kwargs)
        if error is not None:
            raise error
        return response

    # Replace only the leaf call, so the client's own construction is real.
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    return client, seen


def _completion(content: str | None, tool_calls: Any = None, usage: Any = None) -> Any:
    """Build a stub SDK completion object.

    Args:
        content: Assistant text.
        tool_calls: Raw SDK tool-call objects, or ``None``.
        usage: Optional usage object or dict. Omitted (``None``) matches
            replies that do not report token counts.
    """
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_text_response_is_normalised() -> None:
    """A plain answer becomes content with no tool calls."""
    client, _ = _client_with(_completion("hello"))
    response = client.chat([{"role": "user", "content": "hi"}], [])
    assert response.content == "hello"
    assert response.tool_calls == []
    assert response.prompt_tokens == 0
    assert response.completion_tokens == 0
    assert response.total_tokens == 0


def test_tool_calls_are_normalised_and_arguments_decoded() -> None:
    """SDK tool calls become the loop's ToolCall, with arguments as a dict."""
    raw = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="read_file", arguments='{"path": "a.txt"}'),
    )
    client, _ = _client_with(_completion(None, [raw]))

    response = client.chat([], [{"type": "function"}])
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.txt"}


def test_malformed_tool_arguments_do_not_kill_the_run() -> None:
    """Models emit invalid JSON; the tool's own validation reports it instead.

    Raising here would end the whole run over one bad call.
    """
    raw = SimpleNamespace(
        id="call-1", function=SimpleNamespace(name="read_file", arguments="{not json")
    )
    client, _ = _client_with(_completion(None, [raw]))
    assert client.chat([], []).tool_calls[0].arguments == {}


def test_parallel_tool_calls_are_disabled_when_tools_are_sent() -> None:
    """One call per turn, because the loop runs them sequentially anyway.

    Serialising several calls into one message is where smaller models break
    first: an observed turn emitted ``memory_store`` five times, two complete
    and three with argument strings that did not parse.
    """
    client, seen = _client_with(_completion("hi"))

    client.chat([], [{"type": "function", "function": {"name": "read_file"}}])

    assert seen[0]["parallel_tool_calls"] is False


def test_an_endpoint_that_rejects_the_parallel_flag_is_retried_without_it() -> None:
    """A defensive flag must never be the reason a harness cannot start.

    Most OpenAI-compatible servers ignore a field they do not know; a strict
    one answers 400. Dropping it and going again costs one round trip and
    keeps the deployment working.
    """
    from openai import APIStatusError

    client = OpenAICompatibleClient(_settings())
    seen: list[dict[str, Any]] = []

    def _create(**kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        if "parallel_tool_calls" in kwargs:
            raise APIStatusError(
                "unknown field: parallel_tool_calls",
                response=SimpleNamespace(  # type: ignore[arg-type]
                    status_code=400, headers={}, request=None
                ),
                body=None,
            )
        return _completion("hi")

    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    response = client.chat([], [{"type": "function", "function": {"name": "f"}}])

    assert response.content == "hi"
    assert len(seen) == 2
    assert "parallel_tool_calls" in seen[0]
    assert "parallel_tool_calls" not in seen[1]


def test_model_and_temperature_come_from_settings() -> None:
    """Config drives the request, so a YAML change needs no code change."""
    client, seen = _client_with(_completion("hi"))
    client.chat([], [])
    assert seen[0]["model"] == "test-model"
    assert seen[0]["temperature"] == 0.3


def test_empty_choices_is_an_llm_error() -> None:
    """A reply with nothing in it cannot be normalised."""
    client, _ = _client_with(SimpleNamespace(choices=[]))
    with pytest.raises(LLMError, match="no choices"):
        client.chat([], [])


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (APITimeoutError(request=None), "timed out"),  # type: ignore[arg-type]
        (
            APIConnectionError(request=None),  # type: ignore[arg-type]
            "Cannot reach InnKube",
        ),
    ],
)
def test_transport_failures_become_llm_errors(error: Exception, expected: str) -> None:
    """Provider exceptions are translated at the boundary, not leaked upward."""
    client, _ = _client_with(error=error)
    with pytest.raises(LLMError, match=expected):
        client.chat([], [])


def test_http_status_failure_reports_the_code() -> None:
    """401 and 404 are the two failures users actually hit; show the code."""
    error = APIStatusError(
        "unauthorized",
        response=SimpleNamespace(status_code=401, headers={}, request=None),  # type: ignore[arg-type]
        body=None,
    )
    client, _ = _client_with(error=error)
    with pytest.raises(LLMError, match="401"):
        client.chat([], [])


def test_usage_object_is_copied_onto_the_response() -> None:
    """OpenAI-shaped usage becomes the numbers the loop accumulates."""
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=18)
    client, _ = _client_with(_completion("hello", usage=usage))
    response = client.chat([], [])
    assert response.prompt_tokens == 120
    assert response.completion_tokens == 18
    assert response.total_tokens == 138
