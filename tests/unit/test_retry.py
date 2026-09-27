"""Retry policy for transient InnKube failures (ADR-025).

No network and no real waiting: the SDK call is stubbed and the sleeper is
injected, so these tests assert the *schedule* the client would follow
rather than living through it.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from harness.config import Settings
from harness.llm.openai_compatible import RETRYABLE_STATUS_CODES, OpenAICompatibleClient
from harness.llm.protocol import LLMError
from openai import APIStatusError


def _settings(**overrides: Any) -> Settings:
    """Build settings without touching the environment or config.yaml."""
    values: dict[str, Any] = {
        "llm_api_key": "test-key",
        "llm_base_url": "https://example.invalid",
        "llm_model": "test-model",
        "max_retries": 3,
        "retry_base_delay": 1.0,
        "retry_max_delay": 30.0,
    }
    values.update(overrides)
    return Settings(**values)


def _status_error(code: int, headers: dict[str, str] | None = None) -> APIStatusError:
    """Build an SDK HTTP error with a given status and optional headers."""
    return APIStatusError(
        f"http {code}",
        response=SimpleNamespace(  # type: ignore[arg-type]
            status_code=code, headers=headers or {}, request=None
        ),
        body=None,
    )


def _completion(content: str = "ok") -> Any:
    """Build a stub SDK completion object."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))
        ]
    )


def _client(
    outcomes: list[Any], **overrides: Any
) -> tuple[OpenAICompatibleClient, list[float], list[int]]:
    """Return a client whose SDK call replays ``outcomes`` in order.

    Args:
        outcomes: Per-attempt results. An ``Exception`` is raised, anything
            else is returned.
        **overrides: Settings overrides.

    Returns:
        The client, the list of sleep durations it requested, and a
        single-element list holding the call count.
    """
    slept: list[float] = []
    calls = [0]

    client = OpenAICompatibleClient(_settings(**overrides), sleep=slept.append)

    def _create(**_: Any) -> Any:
        index = calls[0]
        calls[0] += 1
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    return client, slept, calls


# --- the headline case ----------------------------------------------------


def test_fails_twice_then_succeeds() -> None:
    """Two 429s then a success: the caller never sees the failures."""
    client, slept, calls = _client(
        [_status_error(429), _status_error(429), _completion("recovered")]
    )

    response = client.chat([{"role": "user", "content": "hi"}], [])

    assert response.content == "recovered"
    assert calls[0] == 3
    assert len(slept) == 2  # one wait before each retry


# --- what is and is not retried -------------------------------------------


@pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS_CODES))
def test_transient_statuses_are_retried(code: int) -> None:
    """429/500/502/503/504 all mean 'not now', so try again."""
    client, _, calls = _client([_status_error(code), _completion()])
    assert client.chat([], []).content == "ok"
    assert calls[0] == 2


@pytest.mark.parametrize("code", [400, 401])
def test_permanent_statuses_are_not_retried(code: int) -> None:
    """A bad token or unknown model fails the same way however often it is sent.

    Retrying would only delay an error the user must act on.
    """
    client, slept, calls = _client([_status_error(code)])
    with pytest.raises(LLMError, match=str(code)):
        client.chat([], [])
    assert calls[0] == 1
    assert slept == []


# --- budget ---------------------------------------------------------------


def test_budget_is_max_retries_plus_one() -> None:
    """Three retries means four attempts in total, then a clear error."""
    client, slept, calls = _client([_status_error(503)], max_retries=3)

    with pytest.raises(LLMError, match="after 4 attempts"):
        client.chat([], [])

    assert calls[0] == 4
    assert len(slept) == 3


def test_zero_retries_disables_retrying() -> None:
    """max_retries=0 is valid and means 'try once'."""
    client, slept, calls = _client([_status_error(503)], max_retries=0)
    with pytest.raises(LLMError):
        client.chat([], [])
    assert calls[0] == 1
    assert slept == []


# --- backoff schedule -----------------------------------------------------


def test_backoff_grows_and_is_bounded() -> None:
    """Each ceiling doubles, and no wait exceeds retry_max_delay.

    Full jitter makes the exact value random, so the assertion is on the
    envelope: wait *i* must fall in [0, base * 2**i], capped.
    """
    client, slept, _ = _client(
        [_status_error(429)] * 6,
        max_retries=5,
        retry_base_delay=1.0,
        retry_max_delay=8.0,
    )
    with pytest.raises(LLMError):
        client.chat([], [])

    assert len(slept) == 5
    for index, delay in enumerate(slept):
        ceiling = min(1.0 * (2**index), 8.0)
        assert 0.0 <= delay <= ceiling


# --- Retry-After ----------------------------------------------------------


def test_retry_after_header_is_honoured() -> None:
    """The server knows its own capacity better than our formula does."""
    client, slept, _ = _client(
        [_status_error(429, {"retry-after": "5"}), _completion()],
        retry_base_delay=1.0,
        retry_max_delay=30.0,
    )
    assert client.chat([], []).content == "ok"
    assert slept == [5.0]


# --- interaction with the rest of the harness -----------------------------


def test_sdk_retries_are_disabled() -> None:
    """Two retry layers would multiply and make the delay unpredictable."""
    client = OpenAICompatibleClient(_settings())
    assert client._client.max_retries == 0
