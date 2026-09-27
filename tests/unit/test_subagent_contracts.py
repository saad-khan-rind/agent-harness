"""The typed contracts: what crosses the boundary, and what is refused.

``test_subagents.py`` tests the runtime. This file tests the vocabulary it
speaks — the envelope, the result models, the lifecycle, the budget
arithmetic and the JSON extraction that decides whether a child's answer is
a result or a failed attempt.

These are unglamorous units and they carry a disproportionate amount of the
design's weight. The gate that stops an unvalidated model answer entering a
coordinating agent's context as a finding is a parser and a pydantic call;
if either is loose, every other guarantee in the slice is decoration.
"""

import pytest
from harness.subagents.contracts import (
    DelegationResult,
    DelegationStatus,
    DelegationTask,
    Evidence,
    ExecutionBudget,
    ResearchResult,
    extract_json_object,
    render_payload,
    validate_payload,
)
from harness.subagents.prompts import child_system_prompt
from harness.subagents.roles import builtin_specs
from pydantic import ValidationError

# -- the envelope -----------------------------------------------------------


def test_the_envelope_carries_only_what_the_parent_chose() -> None:
    """There is no field for "the conversation so far", and that is the point.

    A parent that needs the child to know something has to restate it, and
    the act of restating *is* the isolation boundary. Pinned as a test
    because a well-meaning future change — "just pass the history too" —
    would undo the whole design without breaking anything else.
    """
    assert set(DelegationTask.model_fields) == {
        "role",
        "task",
        "context",
        "constraints",
        "success_criteria",
    }


# -- budgets ----------------------------------------------------------------


def test_a_budget_is_clamped_to_its_ceiling() -> None:
    """The rule everything else about budgets rests on: never wider."""
    asked = ExecutionBudget(max_iterations=8, max_tool_calls=20, timeout_seconds=120)
    ceiling = ExecutionBudget(max_iterations=3, max_tool_calls=50, timeout_seconds=10)

    narrowed = asked.narrowed_to(ceiling)

    assert narrowed.max_iterations == 3
    assert narrowed.max_tool_calls == 20
    assert narrowed.timeout_seconds == 10


# -- the lifecycle ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (DelegationStatus.CREATED, False),
        (DelegationStatus.RUNNING, False),
        (DelegationStatus.COMPLETED, True),
        (DelegationStatus.INVALID_OUTPUT, True),
        (DelegationStatus.FAILED, True),
        (DelegationStatus.REJECTED, True),
        (DelegationStatus.TIMED_OUT, True),
        (DelegationStatus.CANCELLED, True),
    ],
)
def test_the_terminal_states_are_the_ones_that_stop(
    status: DelegationStatus, terminal: bool
) -> None:
    """A record that still says ``running`` is a run that never came back."""
    assert status.is_terminal is terminal


def test_only_a_completed_delegation_is_a_usable_result() -> None:
    """Every other status means the payload must not be treated as a finding."""
    assert DelegationStatus.COMPLETED.is_success
    for status in DelegationStatus:
        if status is not DelegationStatus.COMPLETED:
            assert not status.is_success


# -- validating a child's answer -------------------------------------------


def test_a_fenced_object_is_found() -> None:
    """The shape the contract asks for, and the one models produce."""
    found = extract_json_object('Some prose.\n```json\n{"answer": "hi"}\n```')

    assert found == {"answer": "hi"}


def test_a_valid_answer_validates() -> None:
    """The happy path, through the real model rather than a shape check."""
    payload, reason = validate_payload(
        ResearchResult, '```json\n{"answer": "two lines", "confidence": "high"}\n```'
    )

    assert reason is None
    assert payload == {
        "answer": "two lines",
        "evidence": [],
        "uncertainties": [],
        "confidence": "high",
    }


def test_a_missing_required_field_is_reported_in_terms_the_child_can_fix() -> None:
    """The reason is fed back to the child for its one repair attempt.

    So it has to name the field, not merely say "invalid" — a model told
    only that its output was wrong rewrites the content and breaks what was
    already right.
    """
    payload, reason = validate_payload(ResearchResult, '{"confidence": "high"}')

    assert payload is None
    assert reason is not None
    assert "answer" in reason


def test_an_answer_with_no_object_says_so() -> None:
    """Distinct from a shape failure, because the fix is different."""
    payload, reason = validate_payload(ResearchResult, "no json at all")

    assert payload is None
    assert reason is not None
    assert "No JSON object" in reason


# -- the result models say something --------------------------------------


def test_evidence_separates_fact_from_assumption() -> None:
    """The field that makes a research result worth more than a paragraph.

    A coordinator that cannot tell a sourced claim from an inference will
    state the inference as a finding, which is the failure this field
    exists to prevent.
    """
    assert Evidence(claim="x").kind == "fact"
    assert Evidence(claim="x", kind="assumption").kind == "assumption"
    with pytest.raises(ValidationError):
        Evidence(claim="x", kind="hunch")  # type: ignore[arg-type]


# -- how the contract is explained to the child ----------------------------


def test_every_specialists_example_satisfies_its_own_contract() -> None:
    """The example a child is shown must be one the runtime would accept.

    An example that fails validation teaches the model to produce output
    the runtime rejects, which is the most expensive possible documentation
    bug: it fails every delegation and looks like a model problem.
    """
    for spec in builtin_specs():
        payload, reason = validate_payload(spec.result_model, spec.output_example)
        assert payload is not None, f"{spec.agent_id}: {reason}"


def test_a_childs_prompt_states_the_terms_it_runs_under() -> None:
    """A child that knows its limits stops spending turns discovering them.

    Not how the limits are enforced — the loop, the registry and the policy
    do that — but stating them turns an enforced refusal into a call that
    never happens.
    """
    from harness.subagents.contracts import RunMetadata

    spec = builtin_specs()[0]
    runtime = RunMetadata(
        agent_id=spec.agent_id,
        role=spec.role,
        child_run_id="child-1",
        parent_run_id="parent-1",
        depth=1,
        permission_mode="deny_mutations",
        capabilities=("file_read",),
        tools=("read_file",),
        budget=ExecutionBudget(max_iterations=4, max_tool_calls=6),
    )

    prompt = child_system_prompt(spec, runtime)

    assert "child-1" in prompt
    assert "4 steps and 6 tool calls" in prompt
    assert "cannot delegate" in prompt
    assert "deny_mutations" in prompt
    assert "Your tools: read_file." in prompt


# -- what the parent reads -------------------------------------------------


def test_a_payload_is_rendered_as_prose_not_json() -> None:
    """The parent is a language model reading its own conversation.

    A wall of JSON is both harder for it to use and larger than the prose
    it encodes. The structured payload stays on the result object and in
    the artifact; what enters the window is this.
    """
    rendered = render_payload(
        {
            "answer": "two lines",
            "evidence": [{"claim": "two lines", "source": "notes.md", "kind": "fact"}],
            "uncertainties": [],
            "confidence": "high",
        }
    )

    assert "answer: two lines" in rendered
    assert "- two lines — notes.md — fact" in rendered
    # Dropped rather than rendered empty: "uncertainties: []" tells the
    # parent nothing it could act on and costs it tokens to read.
    assert "uncertainties" not in rendered


def test_the_observation_names_the_status_the_role_and_the_record() -> None:
    """What the parent sees is traceable to the full evidence.

    The child run id in the line is the id of the JSONL trace entries and
    of the stored artifact, so a summary in a transcript can always be
    followed back to what actually happened.
    """
    result = DelegationResult(
        status=DelegationStatus.COMPLETED,
        agent_id="researcher",
        role="researcher",
        child_run_id="child-1",
        parent_run_id="parent-1",
        summary="answer: two lines",
        artifact_path="/memory/traces/delegations/child-1.json",
    )

    observation = result.as_observation()

    assert observation.startswith("[delegation completed] researcher (child-1)")
    assert "answer: two lines" in observation
    # The record's path is not offered to the model: it lives under the
    # memory root, which the parent's file tools cannot reach (ADR-113).
    assert "delegations/child-1.json" not in observation


# -- the edges that only show up under stress ------------------------------
