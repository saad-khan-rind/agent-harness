"""Contract every registered tool must satisfy.

These tests check the *shape* of the tool interface rather than any tool's
behaviour. They exist because the registry is the seam MCP servers will
register through in Week 2: at that point tool definitions stop being
written by us and start arriving from a remote server, and a malformed or
colliding definition would surface as the model quietly misusing a tool
rather than as an exception.

Pinning the contract now means the Week 2 integration has something concrete
to satisfy, and any built-in tool added later is held to the same rules.
Nothing here pre-solves MCP itself — it validates what we already ship.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from harness.tools import ToolRegistry, build_registry
from harness.tools.base import Tool
from jsonschema import Draft202012Validator, SchemaError

# The model sees these names as function names. snake_case is what the
# OpenAI function-calling format expects, and mixing conventions across a
# tool set measurably degrades selection accuracy.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# A description is the entire basis on which the model picks one tool over
# another. Too short is uninformative; too long crowds the context on every
# single request, since schemas are re-sent each turn.
_MIN_DESCRIPTION = 15
_MAX_DESCRIPTION = 300


@pytest.fixture
def registry(agent_registry: ToolRegistry) -> ToolRegistry:
    """Override the shared fixture with the *complete* production tool set.

    Every test in this file must hold for every tool the agent is given, not
    only for the workspace file tools. Overriding the fixture here rather
    than naming ``agent_registry`` in twelve signatures is what keeps
    ``AGENTS.md``'s promise literal: a new tool needs nothing added to this
    file, because these tests iterate whatever the production assembler
    built.
    """
    return agent_registry


def _tools(registry: ToolRegistry) -> list[Tool]:
    """Return every registered tool instance."""
    return [registry.get(name) for name in registry.names()]


# --- schema validity ------------------------------------------------------


def test_every_schema_is_valid_json_schema(registry: ToolRegistry) -> None:
    """Each tool's parameters must satisfy the JSON Schema meta-schema.

    An invalid schema is not rejected by the API — it is accepted and then
    interpreted loosely, so the failure shows up as the model sending
    arguments the tool cannot use. Validating locally turns that into a test
    failure instead.
    """
    for tool in _tools(registry):
        try:
            Draft202012Validator.check_schema(tool.parameters)
        except SchemaError as exc:  # pragma: no cover - only on a real defect
            pytest.fail(f"{tool.name} has an invalid JSON Schema: {exc.message}")


def test_schemas_are_objects_with_properties(registry: ToolRegistry) -> None:
    """Function-calling arguments are always a JSON object."""
    for tool in _tools(registry):
        assert tool.parameters["type"] == "object", tool.name
        assert isinstance(tool.parameters.get("properties"), dict), tool.name


def test_required_fields_actually_exist(registry: ToolRegistry) -> None:
    """A required name with no matching property is unsatisfiable.

    The model would be told to send an argument the schema never describes.
    """
    for tool in _tools(registry):
        properties = set(tool.parameters.get("properties", {}))
        required = set(tool.parameters.get("required", []))
        assert required <= properties, f"{tool.name}: {required - properties}"


def test_every_property_is_typed_and_described(registry: ToolRegistry) -> None:
    """An untyped or undocumented argument is guesswork for the model."""
    for tool in _tools(registry):
        for prop_name, spec in tool.parameters["properties"].items():
            assert "type" in spec, f"{tool.name}.{prop_name} has no type"
            assert spec.get("description"), f"{tool.name}.{prop_name} undocumented"


def test_schemas_accept_a_valid_call_and_reject_a_bad_one(
    registry: ToolRegistry,
) -> None:
    """The schemas must actually discriminate, not just parse.

    A schema that validates everything is as useless as none at all.
    """
    validator = Draft202012Validator(registry.get("write_file").parameters)
    assert validator.is_valid({"path": "a.txt", "content": "hello"})
    assert not validator.is_valid({"path": "a.txt"})  # missing content
    assert not validator.is_valid({"path": 1, "content": "hello"})  # wrong type


# --- naming and metadata --------------------------------------------------


def test_names_are_snake_case(registry: ToolRegistry) -> None:
    """Consistent naming; also what the function-calling format expects."""
    for name in registry.names():
        assert _NAME_PATTERN.match(name), f"{name!r} is not snake_case"


def test_names_are_unique(registry: ToolRegistry) -> None:
    """Guaranteed by the registry, asserted here as the contract itself.

    Week 2 makes this load-bearing: a remote MCP tool must never silently
    shadow a built-in one.
    """
    names = registry.names()
    assert len(names) == len(set(names))


def test_class_name_matches_registered_name(registry: ToolRegistry) -> None:
    """The name the model sees is the class attribute, not the class name.

    ADR-005 deliberately lets these differ (``ListDirectory`` is exposed as
    ``list_files``). This test just pins that the registry key and the
    instance agree, so a rename cannot leave the two out of step.
    """
    for name in registry.names():
        assert registry.get(name).name == name


def test_descriptions_are_useful_and_bounded(registry: ToolRegistry) -> None:
    """Long enough to disambiguate, short enough not to eat the context."""
    for tool in _tools(registry):
        assert tool.description, f"{tool.name} has no description"
        assert _MIN_DESCRIPTION <= len(tool.description) <= _MAX_DESCRIPTION, (
            f"{tool.name} description is {len(tool.description)} chars"
        )
        assert tool.description[0].isupper(), f"{tool.name} description not a sentence"


def test_descriptions_are_distinct(registry: ToolRegistry) -> None:
    """Two tools described identically cannot be told apart by the model."""
    descriptions = [tool.description for tool in _tools(registry)]
    assert len(descriptions) == len(set(descriptions))


# --- risk and safety ------------------------------------------------------


def test_every_tool_declares_a_valid_risk(registry: ToolRegistry) -> None:
    """``RiskBasedPolicy`` keys off this; an unset value would default-allow."""
    for tool in _tools(registry):
        assert tool.risk in {"observe", "mutate"}, f"{tool.name}: {tool.risk!r}"


def test_no_tool_name_suggests_deletion(registry: ToolRegistry) -> None:
    """The brief's hard constraint, asserted against the live registry."""
    forbidden = ("delete", "remove", "unlink", "rmdir", "destroy", "purge", "truncate")
    for name in registry.names():
        assert not any(word in name for word in forbidden), name


def test_registry_matches_the_documented_tool_set(registry: ToolRegistry) -> None:
    """Guards against a tool being added without updating the docs.

    If this fails, update README.md and docs/architecture.md in the same
    change — that is the point of the test.
    """
    assert registry.names() == [
        # Workspace file tools: observe first, then mutate.
        "list_files",
        "read_file",
        "search_text",
        "find_files",
        "create_folder",
        "write_file",
        "append_file",
        "edit_file",
        # Long-term memory, vector-backed (ADR-070). Four file verbs
        # became three record verbs: there is no path to read and no
        # substring to edit, and ``memory_search`` is the capability the
        # file surface could not offer.
        "memory_search",
        "memory_store",
        "memory_upsert_slot",
        # Skills, level two and three of progressive disclosure (ADR-038).
        "skill_read",
    ]


# --- the contract holds for the schema payload too ------------------------


def test_exported_schemas_match_the_tools(registry: ToolRegistry) -> None:
    """What goes on the wire must equal what the tools declare."""
    exported: list[dict[str, Any]] = registry.schemas()
    assert len(exported) == len(registry.names())
    for schema, name in zip(exported, registry.names(), strict=True):
        function = schema["function"]
        tool = registry.get(name)
        assert function["name"] == tool.name
        assert function["description"] == tool.description
        assert function["parameters"] == tool.parameters


def test_contract_holds_for_a_freshly_built_registry(tmp_path: Path) -> None:
    """The contract is a property of the tools, not of one fixture instance."""
    fresh = build_registry(tmp_path)
    for tool in _tools(fresh):
        Draft202012Validator.check_schema(tool.parameters)
        assert _NAME_PATTERN.match(tool.name)


# --- the contract applied to tools we did not write ------------------------


def test_the_contract_also_holds_for_discovered_remote_tools(
    workspace: Path,
    store: Any,
    skills: Any,
) -> None:
    """Tools from an MCP server meet the same bar as the built-ins.

    This is the moment this file's docstring was written for: tool
    definitions stop being authored here and start arriving at runtime.
    Remote tools register through ``build_agent_registry`` exactly like a
    built-in, so they are held to exactly the same contract — the adapter
    normalises the metadata on the way in rather than the contract being
    relaxed on their behalf (ADR-043).

    The definitions below are deliberately bad in every way a real server
    might be careless: no description, a name that is not snake_case, a
    schema property with a ``title`` and no description, and two tools
    sharing a description. Each check is the *same function* the built-in
    tools are held to, called against the combined registry, so the two can
    never drift apart.
    """
    from harness.mcp import wrap_remote_tools
    from harness.toolset import build_agent_registry
    from tests.fakes import remote_tool_definition

    remote = wrap_remote_tools(
        server_name="toy",
        remote_tools=[
            remote_tool_definition("Get-Current-Time", description=None),
            remote_tool_definition(
                "lookup",
                description="does a thing",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"title": "Q", "type": "string"}},
                    "required": ["q", "missing"],
                },
            ),
            remote_tool_definition("other_lookup", description="does a thing"),
        ],
        call=lambda name, arguments: "ok",
    )
    registry = build_agent_registry(
        workspace, memory_store=store, skill_library=skills, mcp_tools=remote
    )

    # Every remote tool actually made it in, beside the local ones.
    assert "toy__get_current_time" in registry.names()
    assert "read_file" in registry.names()

    for check in (
        test_every_schema_is_valid_json_schema,
        test_schemas_are_objects_with_properties,
        test_required_fields_actually_exist,
        test_every_property_is_typed_and_described,
        test_names_are_snake_case,
        test_names_are_unique,
        test_class_name_matches_registered_name,
        test_descriptions_are_useful_and_bounded,
        test_descriptions_are_distinct,
        test_every_tool_declares_a_valid_risk,
        test_no_tool_name_suggests_deletion,
        test_exported_schemas_match_the_tools,
    ):
        check(registry)
