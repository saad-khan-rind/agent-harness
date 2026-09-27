# Attribution

`get_current_time` and `convert_time` — their names, arguments, result
fields and formatting — are taken from the **time** reference server in
[`modelcontextprotocol/servers`](https://github.com/modelcontextprotocol/servers/tree/main/src/time)
(package `mcp-server-time`, version 2026.8.18), which is published under the
MIT licence, Copyright (c) 2024 Anthropic, PBC.

What we changed, and why, is recorded in ADR-044:

- ported from the `mcp<2` low-level decorator API to `MCPServer` in `mcp>=2`,
  because the harness pins `mcp==2.2.0` and the two cannot coexist;
- dropped the `tzlocal` dependency: `--local-timezone` defaults to UTC, which
  is the container's clock anyway;
- split the capability away from the interface (`meeting.py` / `server.py`).

What we added is `find_meeting_slot` and `count_working_days`, which are
our own work.

MIT licence text: <https://github.com/modelcontextprotocol/servers/blob/main/LICENSE>
