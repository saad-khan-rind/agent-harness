"""Publishing ``/metrics``, without letting it take the harness down.

Prometheus scrapes; it is not scraped *to*. So the harness runs a small HTTP
server on a daemon thread and Prometheus pulls from it — the same shape as
every exporter, and the reason the agent needs no network egress to be
observable.

Two decisions live here.

**What happens when the port is taken.** A second harness on one laptop, a
stale container, a developer with something else on 9091: none of those are
reasons to refuse to run an agent. Long-term memory already settles this
argument — a missing Qdrant degrades the run rather than ending it
(ADR-069) — and observability has strictly less claim to be load-bearing
than memory does. So a closed port is a warning in the banner, not a crash.

**That the server can be stopped.** A daemon thread dies with the process,
so production would work without :func:`stop_metrics`; a test suite would
not. Thirty tests that each build a session would leave the first holding
9091 and the other twenty-nine reporting "port unavailable", which is a
false failure and, worse, a *plausible* one. Servers are tracked by port so
stopping one session's exporter cannot stop another's (ADR-081).
"""

import threading
from http.server import HTTPServer

from prometheus_client import CollectorRegistry, start_http_server

# Port -> the server on it and its thread. Holding the reference is half the
# purpose (without it the server is garbage-collected the moment
# `serve_metrics` returns); being able to stop it is the other half.
_SERVERS: dict[int, tuple[HTTPServer, threading.Thread]] = {}


def serve_metrics(port: int, registry: CollectorRegistry) -> str | None:
    """Start the exporter in the background.

    Args:
        port: TCP port to listen on.
        registry: The registry to expose. Passing it explicitly is what keeps
            the process off ``prometheus_client``'s module-level default.

    Returns:
        ``None`` when the server is listening, or a short reason to show in
        the banner when it is not.
    """
    try:
        server, thread = start_http_server(port, registry=registry)
    except OSError as exc:
        # The usual one is EADDRINUSE. Reported with the port in it, because
        # "address already in use" without a number sends people to the wrong
        # process.
        return f"port {port} unavailable ({exc.strerror or exc})"

    # The thread is already running and already a daemon — `start_http_server`
    # sets that before starting it, and Python refuses to change the flag on a
    # live thread, so this is a check rather than an assignment. It matters:
    # a non-daemon thread would keep the interpreter alive after `/exit` and
    # the container would never stop. `stop_metrics` is the belt to this
    # braces, and `Repl.close` calls it on every exit path.
    _SERVERS[port] = (server, thread)
    return None


def stop_metrics(port: int) -> None:
    """Shut down the exporter on ``port``, if this process started one.

    Safe to call when nothing was started — a session with metrics disabled,
    or one whose bind failed, closes exactly like any other.

    Args:
        port: The port passed to :func:`serve_metrics`.
    """
    entry = _SERVERS.pop(port, None)
    if entry is None:
        return
    server, thread = entry
    server.shutdown()
    server.server_close()
    # Bounded: a hung scrape must not hold the REPL's exit open. The thread
    # is a daemon, so the worst case is that it outlives this call and dies
    # with the process anyway.
    thread.join(timeout=2.0)
