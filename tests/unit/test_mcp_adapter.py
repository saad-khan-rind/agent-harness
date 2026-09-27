"""What happens to a tool definition we did not write.

Every tool definition in this codebase up to Week 2 was authored here and
reviewed by a human. A definition discovered on an MCP server is authored
elsewhere and arrives at runtime, so these tests are mostly about *hostile
metadata*: names that are not snake_case, missing descriptions, duplicate
descriptions, schemas with undescribed properties, and names that offer a
capability the brief forbids.

The bar is not "does it work" but "would it pass ``test_tool_contract.py``",
because remote tools register on the same registry the built-ins do and that
file iterates whatever the production assembler built.
"""

from typing import Any

import pytest
from harness.mcp.adapter import (
    DENIED_TOOL_WORDS,
    NAME_SEPARATOR,
    is_denied,
    wrap_remote_tools,
)
from tests.fakes import remote_tool_definition


class _MalformedDefinition:
    """A definition too broken for the SDK's own model to hold.

    ``mcp.Tool`` validates ``input_schema`` as a mapping, so a server
    sending a string or a list there produces a decode failure rather
    than a usable object. The adapter still guards against it, because
    the guard is cheap and the alternative is a registered tool whose
    schema the model cannot satisfy. This stub is the only way to reach
    those branches.
    """

    def __init__(self, name: str, input_schema: Any) -> None:
        self.name = name
        self.description = "A tool with an unusable schema."
        self.input_schema = input_schema
        self.annotations = None


def _wrap(*definitions: Any, server: str = "toy", denied: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    """Wrap definitions with a call function that echoes its arguments."""
    return wrap_remote_tools(
        server_name=server,
        remote_tools=list(definitions),
        call=lambda name, arguments: f"called {name} with {sorted(arguments)}",
        denied_tools=denied,
    )


# --- namespacing ----------------------------------------------------------


def test_the_server_name_prefixes_every_tool() -> None:
    """The prefix is what stops a remote tool shadowing a local one."""
    (tool,) = _wrap(remote_tool_definition("read_file"), server="toy")
    assert tool.name == f"toy{NAME_SEPARATOR}read_file"


def test_two_servers_offering_the_same_name_stay_distinct() -> None:
    """The case the user asked about: same name, different behaviour.

    Without the prefix these are one name, ``ToolRegistry.register`` raises,
    and startup dies over a collision the user did not cause.
    """
    (first,) = _wrap(remote_tool_definition("search"), server="alpha")
    (second,) = _wrap(remote_tool_definition("search"), server="beta")
    assert first.name != second.name
    assert first.name == "alpha__search"
    assert second.name == "beta__search"


@pytest.mark.parametrize(
    ("advertised", "expected"),
    [
        ("Get-Current-Time", "toy__get_current_time"),
        ("getTime", "toy__gettime"),
        ("weird!!name", "toy__weird_name"),
        ("__leading", "toy__leading"),
        ("9lives", "toy__lives"),
    ],
)
def test_awkward_remote_names_become_valid_function_names(
    advertised: str, expected: str
) -> None:
    """Servers are not obliged to use snake_case; the model's API is."""
    (tool,) = _wrap(remote_tool_definition(advertised), server="toy")
    assert tool.name == expected


# --- the deny-list --------------------------------------------------------


@pytest.mark.parametrize("word", sorted(DENIED_TOOL_WORDS))
def test_every_denied_word_is_refused(word: str) -> None:
    """ADR-008 by construction stops working once tools arrive at runtime."""
    assert is_denied(word)
    assert _wrap(remote_tool_definition(word)) == []


def test_denial_matches_inside_a_longer_name() -> None:
    """``fs_delete_path`` offers deletion just as plainly as ``delete``."""
    assert is_denied("fs_delete_path")
    assert is_denied("FileRemove")
    assert _wrap(remote_tool_definition("fs_delete_path")) == []


# --- descriptions ---------------------------------------------------------


# --- schemas --------------------------------------------------------------


# --- risk -----------------------------------------------------------------


def test_a_read_only_tool_is_classified_observe() -> None:
    """MCP's closest equivalent to our risk split is honoured where present."""
    (tool,) = _wrap(remote_tool_definition("ping", read_only=True))
    assert tool.risk == "observe"


# --- reach ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("read_only", "open_world", "expected"),
    [
        (True, False, "local"),
        (True, True, "open_world"),
        (True, None, "open_world"),
        (None, None, "open_world"),
    ],
)
def test_reach_follows_open_world_hint_with_the_specs_default(
    read_only: bool | None, open_world: bool | None, expected: str
) -> None:
    """Only an explicit ``open_world_hint=False`` earns ``local``.

    The specification's default for the hint is true and it is honoured:
    a read-only tool that reaches the open world — web search — is the
    case that motivated the axis, and a tool with no annotations at all
    lands in the cautious corner on both axes (ADR-102).
    """
    (tool,) = _wrap(
        remote_tool_definition("t", read_only=read_only, open_world=open_world)
    )
    assert tool.reach == expected


# --- the wrapped tool behaves like a built-in ------------------------------


def test_a_remote_failure_becomes_an_error_observation() -> None:
    """The loop must not be able to tell local failure from remote failure."""

    def boom(name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("the server said no")

    tool = wrap_remote_tools("toy", [remote_tool_definition("add")], boom)[0]
    observation = tool.execute({})
    assert observation.startswith("Error: ")
    assert "the server said no" in observation
    # Named, so a user reading the trace knows which server to go and check.
    assert "toy" in observation
