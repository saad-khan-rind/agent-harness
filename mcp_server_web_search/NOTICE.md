# Third-party notices

## ddgs

`web_search` is backed by [`ddgs`](https://pypi.org/project/ddgs/), a
metasearch library that aggregates results from several public search
engines. It is used through the `SearchBackend` protocol in `search.py`, so
replacing it with a keyed provider (Tavily, Serper, Exa) is a new class and
a configuration change rather than a rewrite.

`ddgs` queries public search endpoints. It requires no API key and no
account, which is why it was chosen: the test suite must pass with no
credentials (`.github/workflows/ci.yml`), and every member of the team must be able to
run the agent without signing up for anything. It rotates across engines, so
one engine throttling does not fail the call.

## httpx

`web_fetch` uses [`httpx`](https://www.python-httpx.org/), which already
ships in the image as a dependency of `openai` and `mcp`. It is declared
explicitly in `requirements.txt` now that this package imports it directly:
an undeclared transitive dependency is one that can disappear under a
version bump of something unrelated.
