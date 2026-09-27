"""The MCP interface of the extended time server, as a contract.

The brief grades this slice on the *interface*: name, description, input
schema, argument validation, result transformation and error handling. So
these tests assert on what a client sees over the protocol rather than on
Python calls — the server is built in-process and a real ``Client`` is
connected to it, so every assertion below has crossed a real MCP session.

They also re-apply the harness's own bar. ``tests/unit/test_tool_contract.py``
says what a registered tool must look like — snake_case names, bounded and
distinct descriptions, every property typed and described. A tool we publish
to *other* clients should meet the standard we impose on tools we consume,
and meeting it at the source means the harness adapter has nothing to repair
(ADR-043, ADR-045).
"""

import asyncio
import json
from typing import Any

import pytest
from mcp.types import Tool as RemoteToolDefinition
from mcp_server_time_plus.meeting import InvalidArgument
from mcp_server_time_plus.server import build_server

from mcp import Client

# Mirrors tests/unit/test_tool_contract.py. Duplicated deliberately rather
# than imported: this file must keep passing if that one is refactored, and
# the numbers are part of *this* contract too.
_MIN_DESCRIPTION = 15
_MAX_DESCRIPTION = 300

EXPECTED_TOOLS = {
    "get_current_time",
    "convert_time",
    "find_meeting_slot",
    "count_working_days",
}


def _listed() -> list[RemoteToolDefinition]:
    """Return the server's tool list, fetched over a real MCP session."""

    async def run() -> list[RemoteToolDefinition]:
        async with Client(build_server("Europe/Berlin")) as client:
            return list((await client.list_tools()).tools)

    return asyncio.run(run())


def _call(name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Invoke one tool over a real session.

    Returns:
        ``(is_error, text)`` — the two things a caller actually acts on.
    """

    async def run() -> tuple[bool, str]:
        async with Client(build_server("Europe/Berlin")) as client:
            result = await client.call_tool(name, arguments)
            text = "".join(getattr(block, "text", "") for block in result.content)
            return bool(result.is_error), text

    return asyncio.run(run())


@pytest.fixture(scope="module")
def tools() -> list[RemoteToolDefinition]:
    """The published tool list, fetched once for every test in this module."""
    return _listed()


def _schemas_of(node: Any) -> list[Any]:
    """Flatten a schema into itself and every nested value, for scanning."""
    found = [node]
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_schemas_of(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_schemas_of(value))
    return found


# --- what we publish -------------------------------------------------------


def test_the_server_publishes_the_two_inherited_tools_and_the_new_ones(
    tools: list[RemoteToolDefinition],
) -> None:
    """The fork adds a capability; it does not take one away.

    Keeping the upstream two is what makes this an *extension* of an existing
    server rather than a different server that happens to tell the time.
    """
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_descriptions_are_useful_bounded_and_distinct(
    tools: list[RemoteToolDefinition],
) -> None:
    """The description is the whole basis for picking one tool over another."""
    descriptions = [tool.description or "" for tool in tools]

    for tool, description in zip(tools, descriptions, strict=True):
        assert _MIN_DESCRIPTION <= len(description) <= _MAX_DESCRIPTION, tool.name
        assert description[0].isupper(), tool.name
    assert len(set(descriptions)) == len(descriptions)


def test_every_argument_is_typed_and_described(
    tools: list[RemoteToolDefinition],
) -> None:
    """An undocumented argument is one the model fills in by guessing."""
    for tool in tools:
        for name, spec in tool.input_schema["properties"].items():
            assert "type" in spec, f"{tool.name}.{name} has no type"
            assert spec.get("description"), f"{tool.name}.{name} undocumented"


def test_bounds_are_published_not_just_enforced(
    tools: list[RemoteToolDefinition],
) -> None:
    """A limit the model is told about costs no failed call to discover."""
    schema = next(t for t in tools if t.name == "find_meeting_slot").input_schema
    properties = schema["properties"]

    assert properties["timezones"]["minItems"] == 2
    assert properties["timezones"]["maxItems"] == 8
    assert properties["duration_minutes"]["minimum"] == 15
    assert properties["duration_minutes"]["maximum"] == 480
    assert properties["max_results"]["maximum"] == 20
    assert set(schema["required"]) == {"timezones", "date"}


def test_every_tool_is_annotated_read_only(tools: list[RemoteToolDefinition]) -> None:
    """The harness reads this to file a discovered tool as observe, not mutate.

    Without the annotation the adapter assumes a mutation — the safe default,
    and the wrong answer for three tools that only read a clock.
    """
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


# --- results ---------------------------------------------------------------


def test_get_current_time_returns_the_upstream_result_shape() -> None:
    """Same four fields as the reference server, so parsers keep working."""
    is_error, text = _call("get_current_time", {"timezone": "Asia/Karachi"})

    assert not is_error
    payload = json.loads(text)
    assert set(payload) == {"timezone", "datetime", "day_of_week", "is_dst"}
    assert payload["timezone"] == "Asia/Karachi"
    assert payload["datetime"].endswith("+05:00")


def test_convert_time_handles_a_quarter_hour_offset() -> None:
    """Kathmandu is UTC+5:45, which is where naive hour arithmetic breaks.

    Karachi is the source rather than Berlin on purpose. ``convert_time``
    reads *today's* date to decide the offset, as upstream does, so a pair
    with a summer-time side answers differently after the clocks change —
    Berlin to Kathmandu is +3.75h until 25 October 2026 and +4.75h after.
    Neither Karachi nor Kathmandu observes summer time, so this pair gives
    the same answer on any day and the test is about the arithmetic only.
    """
    is_error, text = _call(
        "convert_time",
        {
            "source_timezone": "Asia/Karachi",
            "time": "14:30",
            "target_timezone": "Asia/Kathmandu",
        },
    )

    assert not is_error
    payload = json.loads(text)
    assert payload["source"]["datetime"].endswith("+05:00")
    assert payload["target"]["datetime"].endswith("+05:45")
    assert payload["target"]["datetime"][11:16] == "15:15"
    assert payload["time_difference"] == "+0.75h"


def test_find_meeting_slot_returns_json_the_model_can_use() -> None:
    """The result carries the query, the count and every local reading."""
    is_error, text = _call(
        "find_meeting_slot",
        {
            "timezones": ["Europe/Berlin", "Asia/Karachi"],
            "date": "2026-09-09",
            "max_results": 2,
        },
    )

    assert not is_error
    payload = json.loads(text)
    assert payload["organiser_timezone"] == "Europe/Berlin"
    assert payload["slots_found"] == 21
    assert len(payload["slots"]) == 2
    assert [view["timezone"] for view in payload["slots"][0]["local"]] == [
        "Europe/Berlin",
        "Asia/Karachi",
    ]


# --- errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"timezones": ["UTC", "Mars/Olympus"]}, "not an IANA timezone"),
        ({"timezones": ["UTC", "UTC"]}, "listed more than once"),
        ({"date": "yesterday"}, "YYYY-MM-DD"),
        ({"earliest_local": "18:00", "latest_local": "09:00"}, "must be before"),
    ],
)
def test_a_bad_argument_comes_back_as_a_readable_message(
    arguments: dict[str, Any], expected: str
) -> None:
    """The message must survive the protocol, or the model cannot self-correct.

    This is the mapping in ``server._reject``. Without it the SDK treats our
    ``InvalidArgument`` as a crash and the client is told only "Error
    executing tool find_meeting_slot" — technically an error, practically a
    dead end.
    """
    call: dict[str, Any] = {
        "timezones": ["Europe/Berlin", "Asia/Karachi"],
        "date": "2026-09-09",
    }
    call.update(arguments)

    is_error, text = _call("find_meeting_slot", call)

    assert is_error
    assert expected in text


def test_an_unknown_tool_name_is_refused() -> None:
    """The model does hallucinate names; the server must not pretend."""
    is_error, text = _call("book_the_meeting", {})

    assert is_error
    assert "book_the_meeting" in text


def test_a_broken_local_timezone_stops_the_server_starting() -> None:
    """A launch argument is validated at startup, not handed to the model.

    ``--local-timezone`` is quoted in three tool descriptions. A server that
    started with a bad one would advertise it to the model on every request.
    """
    with pytest.raises(InvalidArgument, match="not an IANA timezone"):
        build_server("Europe/Berlinn")
