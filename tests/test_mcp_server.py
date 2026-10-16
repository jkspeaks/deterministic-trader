"""The MCP wrapper returns exactly what the CLI returns, and exposes only read-only commands."""

import io
import json

from mcp_server import server
from src import cli
from tests.factories import cli_payload


def cli_output(command: str, payload: dict) -> dict:
    out = io.StringIO()
    cli.main([command], io.StringIO(json.dumps(payload)), out)
    return json.loads(out.getvalue())


def test_wrapper_matches_cli_output_for_plan_fetch():
    payload = cli_payload(now="2026-08-03T14:40:00Z")

    assert server.run_command("plan-fetch", payload) == cli_output("plan-fetch", payload)


def test_a_cycle_missing_required_keys_comes_back_as_a_refusal_not_an_exception():
    result = server.run_command("decide", {})

    assert result["command"] == "decide"
    assert result["error"]["code"] == cli.BAD_INPUT
    assert "now" in result["error"]["detail"]


def test_every_exposed_tool_is_a_real_cli_command():
    assert set(server.TOOLS) <= set(cli.COMMANDS)


def test_glue_and_state_shaping_commands_are_not_exposed():
    assert not {c for c in server.TOOLS if c.startswith(("day-context", "shape-", "merge-"))}
