"""deterministic-trader: the deterministic core.

Every module here is a pure function from JSON to JSON. Invariant 2: nothing
under ``src/`` may touch the network. The agent is the only thing that reaches
the outside world; this package only transforms the JSON it is handed.
"""
