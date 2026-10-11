"""The command surface: `uv run python -m src.cli <command> < cycle.json`.

§8's diagram is the contract — JSON on stdin, JSON on stdout, and the routine
writes that stdout verbatim to `runlog` **before** anything is placed
(Invariant 3).

The four commands share one property worth pinning on its own: **each derives
its own cycle from `cadence.decide`**, and none accepts one on stdin. A cycle
dict arriving from the routine is prompt text, and Invariant 6 exists because
the previous system kept exactly this kind of decision in prompt text and lost
it silently.
"""

import io
import json

import pytest

from src import cadence, cli
from tests.factories import cli_payload


def run(command: str, payload: dict) -> tuple[int, dict]:
    """One invocation, as the routine makes it."""
    out = io.StringIO()
    code = cli.main([command], io.StringIO(json.dumps(payload)), out)
    return code, json.loads(out.getvalue())


# --- the envelope ----------------------------------------------------------


def test_a_command_reads_stdin_and_writes_json_to_stdout():
    code, result = run("plan-fetch", cli_payload())

    assert code == 0
    assert result["cycle"]["cycle_type"] == cadence.FULL


def test_the_output_survives_a_json_round_trip():
    """"Pure function from JSON to JSON" is only true if it does. This is what
    lands in `runlog`, and a value that serialises to something Python can no
    longer read is an audit trail that cannot be replayed."""
    _, result = run("decide", cli_payload())

    assert json.loads(json.dumps(result)) == result


def test_the_cycle_id_survives_onto_the_output():
    """§13.1's lock is claimed before any of this runs, so the id belongs to
    the routine. Every command echoes it, because a runlog row that cannot be
    tied to the invocation that wrote it is not an audit trail."""
    _, result = run("plan-fetch", cli_payload(cycle_id="2026-08-03T13:40Z-full"))

    assert result["cycle_id"] == "2026-08-03T13:40Z-full"


# --- refusals, which are still JSON ----------------------------------------


def test_an_unknown_command_is_refused_rather_than_guessed():
    out = io.StringIO()
    code = cli.main(["decyde"], io.StringIO("{}"), out)

    assert code == cli.EXIT_USAGE
    assert json.loads(out.getvalue())["error"]["code"] == cli.UNKNOWN_COMMAND


def test_no_command_at_all_is_refused():
    out = io.StringIO()
    code = cli.main([], io.StringIO("{}"), out)

    assert code == cli.EXIT_USAGE
    assert json.loads(out.getvalue())["error"]["code"] == cli.UNKNOWN_COMMAND


def test_malformed_stdin_is_refused_with_the_parser_message_intact():
    """§14's rule about broker text applies to every diagnostic that crosses a
    boundary: the sentence naming the byte offset is what identifies a
    truncated payload, and `BAD_INPUT` alone would identify nothing."""
    out = io.StringIO()
    code = cli.main(["decide"], io.StringIO("{not json"), out)
    error = json.loads(out.getvalue())["error"]

    assert code == cli.EXIT_BAD_INPUT
    assert error["code"] == cli.BAD_INPUT
    assert "line 1" in error["detail"]


def test_a_refusal_still_names_the_command_it_refused():
    out = io.StringIO()
    cli.main(["decide"], io.StringIO("["), out)

    assert json.loads(out.getvalue())["command"] == "decide"


# --- Invariant 6: the cycle is never taken on trust ------------------------


@pytest.mark.parametrize("command", sorted(cli.COMMANDS))
def test_no_command_accepts_a_cycle_from_stdin(command):
    """A routine that could hand `decide` its own cycle could claim
    `entries_allowed` on a post-close, and Invariant 3's fence would be an
    honour system. Cadence is pure and costs nothing, so every command derives
    its own from the clock and `edge_control`."""
    forged = cli_payload(
        cycle={"cycle_type": cadence.FULL, "entries_allowed": True},
        now="2026-08-03T20:30:00Z",
    )

    _, result = run(command, forged)

    assert result["cycle"]["cycle_type"] == cadence.POSTCLOSE
    assert result["cycle"]["entries_allowed"] is False


def test_the_clock_stamp_is_this_invocations_not_the_previous_ones():
    """`clock_updates` carries `last_full_cycle`, and the routine writes it at
    the *end* of the cycle. Stamping it after `plan-fetch` would make `decide`
    three minutes later classify the same firing as `monitor` — one cycle
    reading as two."""
    _, result = run("decide", cli_payload(now="2026-08-03T13:41:00Z"))

    assert result["cycle"]["clock_updates"]["last_full_cycle"] == "2026-08-03T13:41:00Z"


# --- Invariant 2 -----------------------------------------------------------


def test_the_config_comes_from_the_checkout_not_from_stdin():
    """A payload carrying its own config must not be able to
    widen the drawdown floor or empty the holiday table."""
    _, result = run(
        "plan-fetch",
        cli_payload(config={"drawdown_halt_pct": 0.99}, calendar={"closed": []}),
    )

    assert result["config_source"] == "checkout"
