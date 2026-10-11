"""pead_reaction.py — §6.1's report-day move, as arithmetic instead of prose.

That routine is retired; the pre-market data job
(`services/core/core/premarket.py`) computes the same number server-side from
the Twelve Data bars it already fetches, and this module is the rule it
applies.

The rule is the retired prompt's Step 4, unchanged:

* **`timing == "am"`** (reported before the open) — the reaction session is
  the report date's own bar; `prior_close` is the trading day immediately
  before it in the series.
* **`timing == "pm"`, or unknown** — the reaction session is the next trading
  day's bar after the report date; `prior_close` is the report date's own bar
  (or, if the report landed on a non-trading day, the closest trading day
  before it). Defaulting an unknown timing to the `pm` rule is the
  conservative side: claiming the move a session early would attribute it to
  earnings before the market could have reacted.
* If the fetched window cannot place **both** the reaction session and the
  prior session, the symbol is skipped — never guessed, never widened. It is
  picked up on a later run, still inside its 1-3 session window on any day the
  gap was not the whole window.

Absolute 2, the same one the retired prompt held itself to: the two prices are
copied verbatim from the bar series and the single division is the only
arithmetic. **Nothing here rounds.**

**Pure, and given no network** (Invariant 2). Bars in, one dict out.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

AM = "am"
PM = "pm"


def _bars_by_date(bars: Sequence[Mapping[str, Any]]) -> list[tuple[str, float]]:
    """`[(iso_date, close), ...]` ascending, dropping anything unparseable.

    A bar with no date, a non-numeric close, or a non-positive close is not a
    session this rule can measure against — the same "missing data, not a
    crash" posture `regime._number` takes with vendor strings.
    """
    cleaned: list[tuple[str, float]] = []
    for bar in bars or []:
        if not isinstance(bar, Mapping):
            continue
        day = bar.get("date") or bar.get("datetime")
        raw = bar.get("close")
        if not day or raw is None:
            continue
        try:
            close = float(raw)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        cleaned.append((str(day)[:10], close))
    cleaned.sort(key=lambda pair: pair[0])
    # A vendor page can repeat the latest bar; keep the last value seen for a
    # date so a duplicate does not create a zero-length session gap.
    deduped: dict[str, float] = {}
    for day, close in cleaned:
        deduped[day] = close
    return sorted(deduped.items(), key=lambda pair: pair[0])


def reaction(
    report_date: str,
    timing: str | None,
    bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """One symbol's report-day move, or ``None`` when the bars cannot place it.

    ``report_date`` is the ISO date of the earnings report. ``timing`` is
    Robinhood's own ``"am"``/``"pm"`` string, copied through — anything that is
    not exactly ``"am"`` takes the ``pm`` (conservative) branch. ``bars`` is a
    daily series as ``[{"date": "YYYY-MM-DD", "close": <number>}, ...]`` in any
    order; ``"datetime"`` is accepted as an alias for ``"date"`` so a Twelve
    Data ``values`` array can be passed straight through.

    The return dict is exactly the shape `edge_earnings_reactions_write`
    upserts: ``symbol`` is added by the caller, everything else is here.
    """
    series = _bars_by_date(bars)
    if len(series) < 2:
        return None

    report = str(report_date)[:10]
    dates = [day for day, _ in series]
    on = dates.index(report) if report in dates else None

    before = [i for i, day in enumerate(dates) if day < report]
    after = [i for i, day in enumerate(dates) if day > report]

    if timing == AM:
        reaction_idx = on if on is not None else (after[0] if after else None)
        prior_idx = (reaction_idx - 1) if reaction_idx is not None else None
    else:
        reaction_idx = after[0] if after else None
        prior_idx = on if on is not None else (before[-1] if before else None)

    if (
        reaction_idx is None
        or prior_idx is None
        or prior_idx < 0
        or reaction_idx >= len(series)
        or prior_idx >= reaction_idx
    ):
        return None

    prior_close = series[prior_idx][1]
    reaction_close = series[reaction_idx][1]
    if prior_close == 0:
        return None

    return {
        "report_date": report,
        "timing": timing,
        "reaction_session": series[reaction_idx][0],
        "prior_close": prior_close,
        "reaction_close": reaction_close,
        "reaction_pct": (reaction_close - prior_close) / prior_close,
    }
