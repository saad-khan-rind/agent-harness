"""Chat client for any OpenAI-compatible provider.

InnKube, Gemini, OpenAI, Groq, OpenRouter, Mistral and Ollama all expose the
OpenAI chat-completions API, so the official ``openai`` SDK is used rather
than hand-rolled HTTP: it already handles connection pooling and the
tool-call payload shape. Only the base URL, key and model change between
providers, and those come from :class:`~harness.config.Settings` (ADR-004,
ADR-121).

Everything this module does beyond one SDK call is **normalisation, retry,
and failure containment** — turning provider-shaped objects and
provider-shaped exceptions into the small vocabulary defined in
:mod:`harness.llm.protocol`, and absorbing the transient failures that a
shared inference cluster produces (ADR-025).
"""

import json
import random
import time
from collections.abc import Callable
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from harness.config import Settings
from harness.llm.protocol import LLMError, LLMResponse, ToolCall

# HTTP statuses worth trying again. All of them mean "the server could not
# serve this request *now*", not "this request is wrong":
#
#   429 Too Many Requests  — rate limited, e.g. a shared cluster or free tier
#   500 Internal Error     — often a transient worker fault
#   502 Bad Gateway        — proxy in front of the model workers
#   503 Service Unavailable— model still loading, or capacity exhausted
#   504 Gateway Timeout    — upstream took too long
#
# Deliberately absent: 400 (malformed request), 401/403 (bad token) and 404
# (unknown model). Those fail identically no matter how often they are sent,
# so retrying them only delays a clear error the user has to act on anyway.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Longest raw argument payload quoted back to the model when its JSON does
# not parse. Long enough to show where a truncated string stopped, which is
# the usual shape of the failure and the part that tells the model what went
# wrong; short enough that a runaway generation cannot spend the context
# window describing its own mistake.
MAX_RAW_ARGUMENTS_ECHO = 200

# How each provider is named in an error message. A user who switched
# provider should read "Cannot reach Gemini", not a name they never set.
PROVIDER_LABELS = {
    "innkube": "InnKube",
    "gemini": "Gemini",
    "openai": "OpenAI",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "mistral": "Mistral",
    "ollama": "Ollama",
    "custom": "the LLM endpoint",
}


def _echo(raw: str) -> str:
    """Quote a raw argument payload back to the model, bounded and readable.

    Args:
        raw: The argument string exactly as it arrived.

    Returns:
        The payload in quotes, truncated with an ellipsis past
        :data:`MAX_RAW_ARGUMENTS_ECHO` characters.
    """
    clipped = raw[:MAX_RAW_ARGUMENTS_ECHO]
    suffix = "..." if len(raw) > MAX_RAW_ARGUMENTS_ECHO else ""
    return f"{clipped!r}{suffix}"


def _rejects_parallel_flag(exc: Exception, request: dict[str, Any]) -> bool:
    """Return whether ``exc`` is this endpoint refusing ``parallel_tool_calls``.

    Narrow on purpose, and all three conditions are load-bearing: the field
    has to still be in the request (or a later pass would strip it twice),
    the status has to be 400 (a 429 naming the field in a rate-limit message
    is still a rate limit), and the body has to name the field (a 400 about
    the model or the messages is a real error the user must see, and
    silently retrying it without the flag would report the wrong cause).

    Args:
        exc: The exception the SDK raised.
        request: The request kwargs, inspected for the flag.

    Returns:
        ``True`` when dropping the flag and retrying is the right response.
    """
    if "parallel_tool_calls" not in request:
        return False
    if not isinstance(exc, APIStatusError) or exc.status_code != 400:
        return False
    return "parallel_tool_calls" in str(getattr(exc, "message", "") or str(exc))


def _as_token_count(value: Any) -> int:
    """Coerce a usage field to a non-negative int.

    Args:
        value: Whatever the SDK or a dict payload put in the field.

    Returns:
        ``value`` as an int, or ``0`` when it is missing, unreadable, or
        negative. Inventing a count would be worse than reporting zero.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def _usage_tokens(usage: Any) -> tuple[int, int]:
    """Read prompt and completion counts off an SDK usage object.

    Providers are OpenAI-compatible but usage is optional: some replies omit
    it, and test stubs often have no ``usage`` attribute. Missing counts
    become 0 so the loop can always accumulate without a None check.

    Accepts the SDK object (``prompt_tokens`` / ``completion_tokens``), the
    newer ``input_tokens`` / ``output_tokens`` names, and a plain dict, so a
    payload-shape change does not silently drop the measurement.

    Args:
        usage: ``completion.usage``, or ``None`` when the field is absent.

    Returns:
        ``(prompt_tokens, completion_tokens)``.
    """
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        return _as_token_count(prompt), _as_token_count(completion)
    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "input_tokens", 0)
    completion = getattr(usage, "completion_tokens", None)
    if completion is None:
        completion = getattr(usage, "output_tokens", 0)
    return _as_token_count(prompt), _as_token_count(completion)


class OpenAICompatibleClient:
    """Chat client for the configured OpenAI-compatible inference endpoint."""

    def __init__(
        self,
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[int, Exception], None] | None = None,
    ) -> None:
        """Build the SDK client from validated process settings.

        The client is constructed once and reused. Creating one per request
        would discard the underlying connection pool and add a TCP and TLS
        handshake to every turn of the loop — noticeable, because a ReAct
        run makes one request per iteration.

        Args:
            settings: Validated harness settings, including the API key.
            sleep: Injected so tests can assert on the backoff schedule
                without actually waiting. Production uses ``time.sleep``.
            on_retry: Optional observer called once per retried attempt.

                Retries happen *here*, below the loop, so nothing above this
                module has ever been able to see them: three rate-limited
                attempts and one slow response are the same forty seconds on
                the same span, and "excessive retries" is invisible by
                construction. A callable rather than a hook, because this
                module knows nothing about the lifecycle seam and should not
                start to; the CLI passes the telemetry hook's recorder
                method (ADR-094).

                A plain default of ``None`` keeps every existing caller —
                and every test that builds a client — working unchanged.
        """
        self._settings = settings
        self._label = PROVIDER_LABELS.get(settings.llm_provider, "the LLM endpoint")
        self._sleep = sleep
        self._on_retry = on_retry
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            # Timeout comes from config rather than the SDK default: the lab
            # models are medium-sized and slow, and a run that dies at 10
            # minutes with a clear message beats one that hangs forever.
            timeout=settings.llm_timeout,
            # The SDK retries on its own by default. Disabled so that this
            # module owns the policy: two independent retry layers multiply
            # (2 SDK attempts x 3 of ours = 6 requests) and make the observed
            # delay impossible to reason about or test.
            max_retries=0,
        )

    @property
    def sdk(self) -> OpenAI:
        """Return the underlying SDK client.

        Exposed for one caller: the memory subsystem needs an embeddings
        client against the same endpoint and the same credential. Handing
        it this object rather than the settings keeps the key in exactly
        one place and reuses the connection pool, so probing for embeddings
        costs no second TLS handshake (ADR-066).
        """
        return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Send one chat completion, retrying transient failures.

        Args:
            messages: OpenAI-style chat messages.
            tools: Function-calling schemas, or an empty list for none.

        Returns:
            The normalised response.

        Raises:
            LLMError: On a non-retryable failure, on a reply with no choices,
                or once the retry budget is exhausted.
        """
        # Build kwargs incrementally: passing ``tools=[]`` makes some
        # OpenAI-compatible servers reject the request outright, so the key
        # is omitted entirely when there are no tools.
        request: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.temperature,
        }
        if tools:
            request["tools"] = tools
            # "auto" leaves the decision to act or answer with the model.
            # Forcing a tool call would break the loop's exit condition,
            # since an empty tool_calls list is how a run terminates.
            request["tool_choice"] = "auto"
            # One call per turn. The loop executes calls sequentially anyway
            # (``loop.py``, "they run sequentially"), so parallel calls buy
            # no concurrency here — they only add a way for the turn to be
            # malformed. Serialising several calls into one message is where
            # smaller and quantised models break first: an observed turn
            # emitted ``memory_store`` five times, two with complete
            # arguments and three with argument strings that did not parse
            # as JSON, which stored the same fact twice and produced three
            # errors. Asking for one call at a time removes that failure
            # mode rather than detecting it after the fact.
            #
            # Most OpenAI-compatible servers ignore a field they do not
            # know, but a strict one answers 400 instead, and a harness that
            # cannot start because of a defensive flag is worse than the
            # duplicate calls the flag prevents. ``_request_with_retries``
            # drops the key and retries once when that happens, so this is
            # an optimisation the deployment can decline rather than a
            # requirement it has to meet.
            request["parallel_tool_calls"] = False

        completion = self._request_with_retries(request)

        if not completion.choices:
            raise LLMError(f"{self._label} returned a response with no choices.")

        return self._normalise(completion)

    # -- Retry ---------------------------------------------------------------

    def _request_with_retries(self, request: dict[str, Any]) -> Any:
        """Call the endpoint, retrying transient failures with backoff.

        Attempt budget is ``max_retries + 1``: the first try plus that many
        retries. A budget of 0 disables retrying entirely, which is what the
        tests for the non-retryable paths use.

        Args:
            request: Fully built kwargs for the SDK call.

        Returns:
            The SDK completion object.

        Raises:
            LLMError: On the final failure, whether it was retryable or not.
        """
        attempts = self._settings.max_retries + 1
        attempt = 0
        while attempt < attempts:
            try:
                return self._client.chat.completions.create(**request)
            except (
                APITimeoutError,
                APIConnectionError,
                APIStatusError,
            ) as exc:
                # An endpoint that rejects ``parallel_tool_calls`` outright
                # is told once, not argued with: the key is dropped and the
                # same request goes again. Deliberately *not* counted as an
                # attempt — the request that failed is not the request being
                # retried, and charging the user's retry budget for a field
                # the harness chose to send would spend their retries on the
                # harness's own optimism. It cannot loop: the key is gone,
                # so the guard is false on every later pass.
                if _rejects_parallel_flag(exc, request):
                    del request["parallel_tool_calls"]
                    continue

                retryable = self._is_retryable(exc)
                last_attempt = attempt == attempts - 1

                # Give up immediately on a permanent failure, and on the last
                # attempt of a transient one. Either way the user gets the
                # same explanatory message.
                if not retryable or last_attempt:
                    raise self._as_llm_error(
                        exc, attempt + 1 if retryable else 0
                    ) from exc

                self._notify_retry(attempt + 1, exc)
                self._sleep(self._backoff_delay(attempt, exc))
                attempt += 1

        # Unreachable: the loop either returns or raises. Present so the
        # function has no implicit ``None`` return path for mypy.
        raise LLMError(f"{self._label} retry loop exited without a result.")

    def _notify_retry(self, attempt: int, exc: Exception) -> None:
        """Tell the observer about one retried attempt, if there is one.

        Swallows anything the observer raises. An observer that can fail a
        model request is an observer that has made the thing it watches less
        reliable, which is the rule the whole telemetry layer is built on
        (ADR-069, ADR-081) applied to the one seam that reaches into the
        client.

        Args:
            attempt: 1-based number of the attempt that just failed.
            exc: Why it failed.
        """
        if self._on_retry is None:
            return
        try:
            self._on_retry(attempt, exc)
        except Exception:  # noqa: BLE001 - see docstring
            pass

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Decide whether ``exc`` is worth another attempt.

        Timeouts and connection errors are always transient — nothing about
        the request itself caused them. HTTP errors are retryable only for
        the statuses in :data:`RETRYABLE_STATUS_CODES`.

        Args:
            exc: The provider exception just caught.

        Returns:
            ``True`` if another attempt could plausibly succeed.
        """
        if isinstance(exc, APITimeoutError | APIConnectionError):
            return True
        if isinstance(exc, APIStatusError):
            return exc.status_code in RETRYABLE_STATUS_CODES
        return False

    def _backoff_delay(self, attempt: int, exc: Exception) -> float:
        """Return how long to wait before retry number ``attempt``.

        Exponential backoff with **full jitter**: the delay is a random point
        in ``[0, base * 2**attempt]`` rather than the ceiling itself. The
        randomness matters here specifically — the whole class hits one
        InnKube cluster, and fixed backoff would synchronise everyone's
        retries into repeating thundering herds that keep causing the 429s
        they are backing off from.

        A ``Retry-After`` header, when the server sends one, overrides the
        computed value: the server knows better than we do.

        Args:
            attempt: Zero-based index of the attempt that just failed.
            exc: The exception, inspected for a ``Retry-After`` header.

        Returns:
            Seconds to sleep, never above ``retry_max_delay``.
        """
        server_hint = self._retry_after_seconds(exc)
        if server_hint is not None:
            return min(server_hint, self._settings.retry_max_delay)

        ceiling = min(
            self._settings.retry_base_delay * (2**attempt),
            self._settings.retry_max_delay,
        )
        return random.uniform(0.0, ceiling)

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        """Read a ``Retry-After`` header off an HTTP error, if present.

        Only the numeric-seconds form is honoured. The HTTP-date form is
        legal but rare from inference servers, and parsing it wrongly would
        produce a wildly wrong sleep, so it is ignored in favour of our own
        backoff.

        Args:
            exc: The exception to inspect.

        Returns:
            Seconds to wait, or ``None`` when there is no usable header.
        """
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return None
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return None
        # A negative or absurd value is a server bug; fall back to our own.
        return seconds if seconds >= 0 else None

    def _as_llm_error(self, exc: Exception, attempts: int) -> LLMError:
        """Translate a provider exception into our own error type.

        Args:
            exc: The provider exception.
            attempts: How many attempts were made, or 0 when the failure was
                not retryable. Included in the message so a user seeing a
                failure knows whether the harness already tried again.

        Returns:
            The :class:`LLMError` to raise.
        """
        suffix = f" after {attempts} attempts" if attempts > 1 else ""

        if isinstance(exc, APITimeoutError):
            return LLMError(
                f"{self._label} request timed out after "
                f"{self._settings.llm_timeout}s{suffix}. "
                f"Increase LLM_TIMEOUT or try a smaller task."
            )
        if isinstance(exc, APIConnectionError):
            return LLMError(
                f"Cannot reach {self._label} at "
                f"{self._settings.llm_base_url}{suffix}: {exc}"
            )
        if isinstance(exc, APIStatusError):
            # The status code is the single most useful thing for the user to
            # see, so it leads the message. 401 means a bad token, 404 an
            # unknown model, 429 a rate limit we could not wait out.
            return LLMError(
                f"{self._label} returned HTTP {exc.status_code} for model "
                f"{self._settings.llm_model!r}{suffix}: {exc.message}"
            )
        return LLMError(f"{self._label} request failed{suffix}: {exc}")

    # -- Normalisation -------------------------------------------------------

    def _normalise(self, completion: Any) -> LLMResponse:
        """Convert an SDK completion into an :class:`LLMResponse`.

        Kept private and separate from :meth:`chat` so the parsing rules can
        be unit-tested against a stub completion without any network access.
        Usage is read from the completion, not the message: that is where
        the OpenAI-compatible payload puts it.

        Args:
            completion: The SDK chat-completion object.

        Returns:
            Normalised content, tool calls, token counts, and the finish
            reason when the endpoint sent one.
        """
        choice = completion.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        # ``tool_calls`` is None (not []) when the model answers in text, so
        # the guard is required before iterating.
        for raw in message.tool_calls or []:
            arguments, decode_error = self._decode_arguments(raw.function.arguments)
            calls.append(
                ToolCall(
                    id=raw.id,
                    name=raw.function.name,
                    arguments=arguments,
                    decode_error=decode_error,
                )
            )
        prompt_tokens, completion_tokens = _usage_tokens(
            getattr(completion, "usage", None)
        )
        # ``getattr`` because it is optional on the wire and absent from
        # some OpenAI-compatible servers and from most test stubs. Absent
        # means "not reported", which is different from "stop" and must not
        # be rounded to it: a dashboard that showed every unreported finish
        # as a clean stop would hide exactly the truncations this field is
        # here to surface (ADR-094).
        finish_reason = getattr(choice, "finish_reason", None)
        return LLMResponse(
            content=message.content,
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=str(finish_reason) if finish_reason else None,
        )

    @staticmethod
    def _decode_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
        """Decode a tool-call argument string, reporting why it failed.

        Models emit invalid JSON often enough that this cannot raise: doing
        so would kill the whole run over one malformed call. What it also
        must not do is *hide* the failure. Returning a bare ``{}`` made a
        parse error indistinguishable from a model that sent no arguments,
        and the two need opposite corrections — one is "your JSON is
        broken", the other is "you left out an argument". Told the wrong
        one, the model cannot fix the right thing.

        The raw payload is quoted back because the fault is usually visible
        in it: a string that stops mid-token says "truncated", where a
        description of the error does not.

        Args:
            raw: JSON string from the SDK, or ``None``.

        Returns:
            ``(arguments, decode_error)``. On success, the decoded object
            and ``None``. On failure, ``{}`` and a sentence naming what
            arrived, suitable for handing straight to the model.
        """
        if not raw:
            # Genuinely absent, not malformed. A no-argument tool call is
            # legal, so this is not an error — the tool's own validation
            # decides whether the call needed arguments.
            return {}, None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, (
                f"the arguments were not valid JSON ({exc.msg} at "
                f"character {exc.pos}). Received: {_echo(raw)}. Send the "
                f"arguments again as one complete JSON object."
            )
        # A model can legally emit a JSON scalar or list here; only an object
        # can be splatted into tool arguments.
        if not isinstance(decoded, dict):
            return {}, (
                f"the arguments must be a JSON object, but a "
                f"{type(decoded).__name__} arrived. Received: {_echo(raw)}. "
                f"Send the arguments as an object of named parameters."
            )
        return decoded, None
