"""MCP wrapper around the deterministic core's read-only commands.

Every tool is a pure function of the cycle document it is given: the wrapper
calls ``src.cli.main`` in-process and returns its JSON. It holds no broker
credentials, places no orders and fetches nothing. The caller supplies the broker
data and executes whatever plan comes back.

Not financial advice. See README.md and LICENSE.

Run (stdio):  uv run --group mcp python -m mcp_server.server
Config:       set DT_CONFIG_DIR to use your own config/ directory instead of
              the repo's checked-in one.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

from src import cli

NOTICE = (
    "Not financial advice. Returns a plan only; this tool never places orders."
)

# Commands that compute from the cycle document and return JSON. Deliberately
# not exposed: the glue commands the scheduled agent uses to assemble its own
# loop (merge-fetch, shape-*, day-context-*, dip-recovery-pool/pick, mc-prefilter).
TOOLS: dict[str, str] = {
    "plan-fetch": "Classify the cycle and list which broker data must be fetched next.",
    "decide": "Run the full pipeline and return the plan (orders, protection, cancels).",
    "reconcile": "Compare broker positions and orders with the system's intended state.",
    "report": "Build the per-cycle status report.",
    "candidates": "List entry candidates for the cycle.",
    "settled-cash": "Compute settled cash for the account.",
    "dip-recovery-decide": "Run the dip-recovery strategy decision.",
    "manual-check-decide": "Run the manual-check decision.",
}


def run_command(command: str, cycle: dict[str, Any]) -> dict[str, Any]:
    """Run one core command in-process; the result is exactly what the CLI prints.

    The CLI answers malformed JSON with a refusal but lets a cycle that is
    missing a required key raise. Over MCP that would surface as a transport
    error, so it is returned as the same refusal shape instead.
    """
    out = io.StringIO()
    try:
        cli.main(
            [command],
            io.StringIO(json.dumps(cycle)),
            out,
            config_dir=os.environ.get("DT_CONFIG_DIR") or None,
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes a refusal
        return {
            "command": command,
            "error": {
                "code": cli.BAD_INPUT,
                "detail": f"{type(exc).__name__}: {exc}",
            },
        }
    return json.loads(out.getvalue())


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("deterministic-trader")

    def register(command: str, summary: str) -> None:
        def tool(cycle: dict[str, Any]) -> dict[str, Any]:
            return run_command(command, cycle)

        tool.__name__ = command.replace("-", "_")
        tool.__doc__ = f"{summary} {NOTICE}"
        server.tool()(tool)

    for command, summary in TOOLS.items():
        register(command, summary)
    return server


if __name__ == "__main__":
    build_server().run()
