"""**Real model.** The shipped image serves real numbers about a real run.

`tests/unit/test_span_metrics.py` proves the spans are counted correctly and
`tests/integration/test_metrics_endpoint.py` proves `cli.main()` wires it up.
Neither proves the thing a TA will do, which is start the container, point
something at port 9091, and see numbers from an agent that just did some
work. Between those tests and that moment sit the Dockerfile, the container
network, the non-root user, and a model nobody scripted — and every one of
them has broken something in this project before (ADR-076, ADR-081).

So these run the image, hold the REPL open, give it a task in English, and
scrape the endpoint **while the agent is working**. One-shot mode cannot be
used here: the exporter lives and dies with the process, so a run that has
already exited has nothing left to scrape.

The scrape comes from a sibling container on the same Docker network, not
from a published port — see :class:`~tests.e2e.conftest.Session`. That is
the only form that works when the gate is itself running inside a container,
and it is also the path Prometheus takes in ``compose.yaml``.

Every assertion is on the exposition text, which is the only thing a scraper
ever sees. Nothing here asserts on how the model chose to do the task —
which tool it reached for is its business, and a test that demanded a
particular route would be measuring the model rather than the harness
(ADR-078).
"""

import warnings

import pytest
from tests.e2e.conftest import Stack

pytestmark = pytest.mark.e2e


def test_the_container_serves_metrics_about_a_real_run(stack: Stack) -> None:
    """A task is given, and the numbers for it can be scraped from the host.

    The single most important assertion in this file, because it is the demo:
    `harness_runs_total{status="success"}` reaching 1.0 means a real model
    finished a real task and the harness counted it, read over the network
    from outside the container.
    """
    with stack.session() as live:
        live.wait_for("Metrics:")
        live.ask("Create a file called notes.md containing the word ready.")

        body = live.scrape_until('harness_runs_total{role="main",status="success"}')

        assert 'harness_runs_total{role="main",status="success"} 1.0' in body
        # The gauge comes back down. A metric that only ever climbs would
        # report this agent as permanently busy from its first task onward.
        assert 'harness_active_runs{role="main"} 0.0' in body


def test_the_banner_says_where_metrics_are(stack: Stack) -> None:
    """The URL is printed, because an endpoint nobody can find is not one.

    This is also the only assertion that the port the image serves on is the
    port `config.yaml` configures — they are two different files, and a
    scraper pointed at the wrong number sees nothing and says nothing.
    """
    with stack.session() as live:
        banner = live.wait_for("Metrics:")

        assert "9091" in banner, banner
        assert "/metrics" in banner, banner


def test_a_real_tool_call_is_counted_with_its_kind(stack: Stack) -> None:
    """Tool usage is one of §8.4's required metrics, and it is per-tool.

    The assertion is on the *shape* — some builtin tool was called and
    recorded — rather than on `write_file` specifically. Naming the tool
    would punish a model that solved the task a different legitimate way,
    which is the mistake ADR-078 was written about.
    """
    with stack.session() as live:
        live.wait_for("Metrics:")
        live.ask("Create a file called report.md containing one sentence.")

        body = live.scrape_until("harness_tool_calls_total{")

        recorded = [
            line
            for line in body.splitlines()
            if line.startswith("harness_tool_calls_total{")
        ]
        assert recorded, body[-2000:]
        assert any('kind="builtin"' in line for line in recorded), recorded
        # Every tool call passes the gate, so a counted call with no counted
        # decision means the two are being recorded from different places —
        # exactly the drift `after_permission` exists to prevent.
        assert "harness_permission_decisions_total{" in body


def test_token_usage_is_reported_for_a_real_endpoint(stack: Stack) -> None:
    """Token consumption is §8.4's other required LLM metric.

    Only a real run can produce this: `usage` comes back from InnKube, and
    the harness records what it was told rather than estimating (ADR-031).
    A scripted model has no usage to report, so this assertion cannot exist
    in any other tier.

    Warned rather than failed when the endpoint omits `usage` — that is a
    property of the endpoint, not of this code, and the harness is explicitly
    built to stay quiet about numbers it was not given.
    """
    with stack.session() as live:
        live.wait_for("Metrics:")
        live.ask("In one short sentence, what is a ReAct agent?")

        body = live.scrape_until("harness_llm_requests_total{")

        assert 'harness_llm_requests_total{role="main"}' in body
        if 'harness_llm_tokens_total{direction="prompt"}' not in body:
            warnings.warn(
                "the endpoint reported no token usage for this run; the "
                "harness recorded nothing rather than estimating (ADR-031)",
                stacklevel=1,
            )


def test_no_series_is_labelled_by_run(stack: Stack) -> None:
    """Cardinality, checked on the wire that a scraper actually reads.

    A `run_id` label would arrive as a *value* inside a series, invisible to
    any check that only looked at declared label names. This reads what
    Prometheus reads: if a per-run label ever appears, the database grows
    without bound and is useless within a day (ADR-081).
    """
    with stack.session() as live:
        live.wait_for("Metrics:")
        live.ask("Create a file called one.md containing the word one.")

        body = live.scrape_until('harness_runs_total{role="main",status="success"}')

        assert "run_id" not in body


def test_a_denied_call_is_visible_to_a_scraper(stack: Stack) -> None:
    """The demo: `curl :9091/metrics` while a deny happens.

    Run read-only, then asked to write. What the model does about the
    refusal is its own business — it may explain, retry, or give up — but
    the *harness* must record the decision, and nothing must land on disk.
    Both halves are asserted, because a permission panel that shows a deny
    while the file appears anyway would be worse than no panel.
    """
    with stack.session(permissions="deny_mutations") as live:
        live.wait_for("Metrics:")
        live.ask("Create a file called forbidden.md containing the word hello.")

        body = live.scrape_until('harness_permission_decisions_total{outcome="deny"}')

        assert 'harness_permission_decisions_total{outcome="deny"}' in body

    assert not stack.exists("/app/workspace/forbidden.md"), stack.tree()
