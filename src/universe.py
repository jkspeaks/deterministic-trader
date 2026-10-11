"""universe.py — constituent snapshot to candidate set.

Design v3 §5 and §11. Two jobs that look like one: decide who is *in* the
universe, and decide which of them this cycle actually reasons about.

The split matters because they have opposite economics. §5.3: universe size
costs nothing — it is a weekly CI fetch into `edge_universe` and a set
membership test — while candidate set size costs a Robinhood call per symbol,
every cycle. A 530-name universe is affordable precisely because the candidate
set that comes out of it is 5–30 names.

Membership is also the fence Invariant 3 rests on. `guard.py` rejects any
symbol outside the candidate set, so anything that can *widen* this set is a
way around that invariant. That is why the issuer's own holdings file is the
only thing here that can add a name, and why a play trigger can only ever
select from what the file already listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

# Merge notes. None is an error — §5.1 asks for the disagreement to survive
# into the record, not to stop the cycle.
CROSS_CHECK_MISSING = "CROSS_CHECK_MISSING"
NOT_IN_ISSUER_FILE = "NOT_IN_ISSUER_FILE"
SECTOR_DISAGREEMENT = "SECTOR_DISAGREEMENT"

# Candidate-set notes. The first two are names the fence turned away; the
# third is a name it let through on Invariant 7's authority.
TRIGGER_OUTSIDE_UNIVERSE = "TRIGGER_OUTSIDE_UNIVERSE"
MACRO_TARGET_NOT_IN_ROTATION = "MACRO_TARGET_NOT_IN_ROTATION"
ADOPTED_NON_MEMBER = "ADOPTED_NON_MEMBER"


@dataclass(frozen=True)
class Note:
    """Something the merge resolved, recorded rather than raised."""

    code: str
    symbol: str
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "symbol": self.symbol, "detail": self.detail}


@dataclass(frozen=True)
class Universe:
    """Who is in, and who this cycle looks at.

    ``candidates`` and ``snapshot_age_days`` are exactly ``guard.py``'s
    ``ctx["universe"]`` surface, so this drops in without translation. The rest
    is what the merge produced on the way there and costs nothing to carry:
    ``sectors`` in particular saves `get_equity_fundamentals` on every
    candidate, since the holdings file already said so.
    """

    candidates: list[str]
    snapshot_age_days: int | None
    stale: bool
    reason: str
    members: list[str]
    sectors: dict[str, str]
    rotation: list[str]
    notes: list[Note]

    def as_dict(self) -> dict:
        return {
            "candidates": list(self.candidates),
            "snapshot_age_days": self.snapshot_age_days,
            "stale": self.stale,
            "reason": self.reason,
            "members": list(self.members),
            "sectors": dict(self.sectors),
            "rotation": list(self.rotation),
            "notes": [n.as_dict() for n in self.notes],
        }


def _by_symbol(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    return {row["symbol"]: dict(row) for row in rows or []}


def _merge_constituents(
    snapshot: Mapping[str, Any],
) -> tuple[dict[str, str | None], list[Note]]:
    """Reconcile the issuer file against the cross-check (§5.1).

    The issuer file is membership; the cross-check can only annotate it. The
    asymmetry is the point. A union would let a stale wiki row keep a delisted
    company inside the fence indefinitely, and an intersection would hide every
    index addition until the slower source noticed — both resolve the
    disagreement silently, which is the one thing §5.1 rules out.

    Sector is the exception worth making: it is not membership, so a name the
    issuer lists without one takes the cross-check's rather than going into the
    cycle sectorless, which `sizing.py` refuses outright.
    """
    holdings = _by_symbol(snapshot.get("holdings") or [])
    cross_check = _by_symbol(snapshot.get("cross_check") or [])

    sectors: dict[str, str | None] = {}
    notes: list[Note] = []

    for symbol, row in holdings.items():
        theirs = cross_check.get(symbol)
        mine = row.get("sector")

        if theirs is None:
            notes.append(
                Note(
                    CROSS_CHECK_MISSING,
                    symbol,
                    f"{symbol} is in the issuer holdings file and absent from "
                    "the cross-check; membership stands",
                )
            )
        elif mine and theirs.get("sector") and mine != theirs["sector"]:
            notes.append(
                Note(
                    SECTOR_DISAGREEMENT,
                    symbol,
                    f"{symbol} is {mine} in the issuer file and "
                    f"{theirs['sector']} in the cross-check; the issuer's wins",
                )
            )

        sectors[symbol] = mine or (theirs or {}).get("sector")

    for symbol in cross_check:
        if symbol not in holdings:
            notes.append(
                Note(
                    NOT_IN_ISSUER_FILE,
                    symbol,
                    f"{symbol} is in the cross-check only; it is not a member "
                    "until the issuer file carries it",
                )
            )

    return sectors, notes


def _sleeve(etfs: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """The §5.2 static list, flattened into one sector map and the rotation set.

    ETFs never reach `_merge_constituents`: they are in neither source, so a
    merge that saw them would report all fifteen as missing from both. They
    are members by declaration, which is what a file that changes once a decade
    is for.
    """
    sector = dict(etfs.get("sector") or {})
    broad = dict(etfs.get("broad") or {})
    return {**sector, **broad}, sorted(sector)


def _plural(count: int, noun: str) -> str:
    """``reason`` is read by a person in the runlog, not parsed by anything."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _age_days(snapshot: Mapping[str, Any], trading_day: str) -> int | None:
    """How old the snapshot is, in calendar days.

    Calendar and not sessions: §5.1's fetch is weekly and its 14-day limit is
    two of those, both wall-clock quantities. Counting sessions would make the
    limit drift by a day every long weekend.
    """
    as_of = snapshot.get("as_of")
    if as_of is None:
        return None
    return (date.fromisoformat(trading_day) - date.fromisoformat(as_of)).days


def _candidate_set(
    ctx: Mapping[str, Any], members: set[str], rotation: set[str], stale: bool
) -> tuple[list[str], list[Note]]:
    """§5.3's three-way union, each source admitted on its own terms.

    Positions first, and unconditionally: an adopted manual buy can be any
    symbol at all, and a fence that excluded it would leave a real position
    with no legal exit order. Guard has the same fallback at its own candidate
    check for exactly this reason, so the two agree by construction rather
    than by coincidence.
    """
    picked: set[str] = set()
    notes: list[Note] = []

    for position in ctx.get("positions") or []:
        symbol = position.get("symbol")
        picked.add(symbol)
        if symbol not in members:
            notes.append(
                Note(
                    ADOPTED_NON_MEMBER,
                    symbol,
                    f"{symbol} is held but not in the universe; adopted and "
                    "managed on identical terms (Invariant 7)",
                )
            )

    if stale:
        # §14: proceed on positions only. Everything below this line exists to
        # become an entry, and a stale snapshot is precisely the state in which
        # no entry may be placed — so the calls they would cost buy nothing.
        # They are not *denied* here; guard does that, on its own reading of
        # the same age. This only declines to pay for them.
        return sorted(picked), notes

    for trigger in ctx.get("triggers") or []:
        symbol = trigger.get("symbol")
        if symbol in members:
            picked.add(symbol)
        else:
            notes.append(
                Note(
                    TRIGGER_OUTSIDE_UNIVERSE,
                    symbol,
                    f"{trigger.get('play_id') or 'a play'} triggered on "
                    f"{symbol}, which is not a member; a play selects from the "
                    "universe and may not extend it (Invariant 3)",
                )
            )

    for symbol in ctx.get("macro_targets") or []:
        if symbol in rotation:
            picked.add(symbol)
        else:
            notes.append(
                Note(
                    MACRO_TARGET_NOT_IN_ROTATION,
                    symbol,
                    f"{symbol} is not one of the eleven sector ETFs; §6.4 "
                    "expresses macro through the sleeve and nothing else",
                )
            )

    return sorted(picked), notes


def sector_map(ctx: Mapping[str, Any]) -> dict[str, str]:
    """Every sector the issuer holdings file and the ETF sleeve know.

    Public since Dip Recovery phase 7, which needs the same map without the
    rest of `resolve` — that function also picks a candidate set, and Dip
    Recovery has a watchlist of its own instead. Factored out rather than
    reimplemented because a settled design decision says the issuer holdings
    file wins every source disagreement, and two derivations of "which
    sector is this" would let §4.4's cap depend on which side of the account
    is asking.
    """
    constituents, _ = _merge_constituents(ctx.get("snapshot") or {})
    etf_sectors, _ = _sleeve(ctx.get("etfs") or {})
    return {
        symbol: value
        for symbol, value in {**constituents, **etf_sectors}.items()
        if value is not None
    }


@dataclass(frozen=True)
class SectorRotation:
    """The operator's `edge_control.active_sectors`, read against the sleeve.

    Only ever *removes* names from new-money consideration — a rotation
    cannot add a symbol the fence did not already admit, so Invariant 3 is
    untouched. `filtering` is False for the two inert cases, `[]` (every
    sector active) and a list naming nothing known at all: a typo must not
    silently freeze the account, so an all-unmatched rotation filters nothing
    and says so in `unmatched` instead.

    A symbol passes when its sector *or* its sub-industry is active.
    `sub_map` is `sub_industry_map(snapshot)`, carried here so every caller
    keeps the one-argument `inactive` it had.
    """

    active: frozenset[str]
    unmatched: tuple[str, ...]
    filtering: bool
    # The eleven GICS names the sleeve (`config/etfs.json`'s `sector`) speaks
    # for. Anything outside it is never filtered.
    vocabulary: frozenset[str] = frozenset()
    active_subs: frozenset[str] = frozenset()
    sub_map: Mapping[str, str] = field(default_factory=dict)
    # The sector ETFs themselves (XLK...), judged by sector alone.
    sleeve: frozenset[str] = frozenset()

    def inactive(self, symbol: str, sectors: Mapping[str, str]) -> str | None:
        """What excludes `symbol` — its sub-industry, else its sector — or None.

        None (kept) for an unknown sector, for any sector outside the eleven
        (Broad Market, or a value an upstream rename introduced), for a symbol
        whose sector or sub-industry is active, and for a symbol whose
        sub-industry is unknown while sub-industries are being selected on —
        the same fail-open rule, one level down. A sector ETF has no
        sub-industry, so it passes only when its whole sector is active.
        """
        if not self.filtering:
            return None
        sector = sectors.get(symbol)
        if sector not in self.vocabulary or sector in self.active:
            return None
        sub = self.sub_map.get(symbol)
        if sub is not None and sub in self.active_subs:
            return None
        if self.active_subs and sub is None and symbol not in self.sleeve:
            # Sub-industries are being selected on and this member's is not
            # known (an older snapshot, a brand-new constituent): keep it.
            return None
        return sub or sector

    def as_dict(self) -> dict:
        return {
            "active_sectors": sorted(self.active),
            "active_sub_industries": sorted(self.active_subs),
            "unmatched": list(self.unmatched),
            "filtering": self.filtering,
            # False means sub-industry codes are active but the snapshot has
            # no sub-industries yet — they are then filtering nothing.
            "sub_industry_data": bool(self.sub_map),
        }


def _gics_lookup(etfs: Mapping[str, Any]) -> dict[str, str]:
    """Lower-cased GICS name *and* sleeve ticker -> canonical GICS name.

    Off the same §5.2 sleeve file `_sleeve` reads, so there is one
    authoritative spelling. Broad Market is deliberately absent: SPY/QQQ/IWM/
    DIA are the benchmark expression, not a sector bet, and no rotation
    filters them.
    """
    lookup: dict[str, str] = {}
    for ticker, sector in (etfs.get("sector") or {}).items():
        lookup[str(sector).lower()] = sector
        lookup[str(ticker).lower()] = sector
    return lookup


def _code_lookup(codes: Mapping[str, Any]) -> dict[str, tuple[set[str], set[str]]]:
    """Lower-cased code or sub-industry name -> `(sectors, sub_industries)`.
    """
    lookup: dict[str, tuple[set[str], set[str]]] = {}
    for code, sector in (codes.get("sectors") or {}).items():
        lookup[str(code).lower()] = ({sector}, set())
    subs = codes.get("sub_industries") or {}
    for code, entry in subs.items():
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        lookup[str(code).lower()] = (set(), {name})
        lookup[_norm(name)] = (set(), {name})
    for code, entry in (codes.get("baskets") or {}).items():
        parts = entry.get("includes") if isinstance(entry, Mapping) else entry
        names = set()
        for part in parts or []:
            sub = subs.get(part)
            names.add(sub.get("name") if isinstance(sub, Mapping) else sub)
        lookup[str(code).lower()] = (set(), {n for n in names if n})
    return lookup


def _norm(text: Any) -> str:
    return " ".join(str(text).split()).lower()


def sub_industry_map(snapshot: Mapping[str, Any]) -> dict[str, str]:
    """`{symbol: GICS sub-industry}` from the snapshot's cross-check rows.

    The issuer holdings file carries no classification at all, so — exactly
    as for sectors (`_merge_constituents`) — the Wikipedia cross-check is the
    source. Only members count: a cross-check-only name is not in the fence.
    """
    members = {row.get("symbol") for row in snapshot.get("holdings") or []}
    return {
        row["symbol"]: row["sub_industry"]
        for row in snapshot.get("cross_check") or []
        if row.get("symbol") in members and row.get("sub_industry")
    }


def sector_rotation(
    active_sectors: Any,
    etfs: Mapping[str, Any],
    codes: Mapping[str, Any] | None = None,
    sub_industries: Mapping[str, str] | None = None,
) -> SectorRotation:
    """`edge_control.active_sectors` in, a `SectorRotation` out. Pure.

    Entries match case-insensitively: a GICS sector name, a sleeve ticker
    (`XLE` is Energy), and — with `codes` (`config/rotation_codes.json`) — a
    sector code (`EN`), a sub-industry code (`CHIP`) or name
    ("Semiconductors"), or a basket (`SEMI`). A bare string is read as a
    one-name list, and anything else that is not a list is unmatched rather
    than guessed at.
    """
    vocabulary = frozenset((etfs.get("sector") or {}).values())
    sleeve = frozenset((etfs.get("sector") or {}).keys())
    sub_map = dict(sub_industries or {})
    if active_sectors is None:
        entries: list[Any] = []
    elif isinstance(active_sectors, str):
        entries = [active_sectors]
    elif isinstance(active_sectors, (list, tuple)):
        entries = list(active_sectors)
    else:
        return SectorRotation(frozenset(), (str(active_sectors),), False, vocabulary,
                              frozenset(), sub_map, sleeve)

    sectors_by_name = _gics_lookup(etfs)
    by_code = _code_lookup(codes or {})
    active: set[str] = set()
    active_subs: set[str] = set()
    unmatched: list[str] = []
    for entry in entries:
        key = _norm(entry)
        if key in sectors_by_name:
            active.add(sectors_by_name[key])
        elif key in by_code:
            named_sectors, named_subs = by_code[key]
            active |= {s for s in named_sectors if s in vocabulary}
            active_subs |= named_subs
        else:
            unmatched.append(str(entry))

    return SectorRotation(
        frozenset(active), tuple(sorted(set(unmatched))), bool(active or active_subs),
        vocabulary, frozenset(active_subs), sub_map, sleeve,
    )


def rotation_filter(
    rows: Sequence[Any], sectors: Mapping[str, str], rotation: SectorRotation
) -> tuple[list[Any], list[tuple[str, str]]]:
    """Split `rows` into `(kept, dropped)`; `dropped` is `(symbol, reason)`.

    `rows` are `{"symbol", ...}` dicts or bare tickers, returned as given.
    Held positions never reach this function — callers filter candidate
    lists only, never the book (Invariant 7).
    """
    kept: list[Any] = []
    dropped: list[tuple[str, str]] = []
    for row in rows:
        symbol = row.get("symbol") if isinstance(row, Mapping) else row
        sector = rotation.inactive(symbol, sectors) if symbol else None
        if sector is None:
            kept.append(row)
        else:
            dropped.append((symbol, f"{sector} is outside the active sector rotation"))
    return kept, dropped


def resolve(ctx: Mapping[str, Any]) -> Universe:
    """Snapshot in, universe out. Pure function of JSON (Invariant 2)."""
    snapshot = ctx.get("snapshot") or {}

    constituents, notes = _merge_constituents(snapshot)
    etf_sectors, rotation = _sleeve(ctx.get("etfs") or {})

    sectors = sector_map(ctx)

    age = _age_days(snapshot, ctx["trading_day"])
    max_age = int(ctx["config"]["universe_max_age_days"])
    # `age is None` reads as stale, exactly as guard reads it. A universe
    # nobody has ever fetched is not a fresh one.
    stale = age is None or age > max_age

    members = {*constituents, *etf_sectors}
    candidates, picked_notes = _candidate_set(ctx, members, set(rotation), stale)

    positions_only = f"{_plural(len(candidates), 'held position')} only"
    if age is None:
        reason = f"the universe has never been fetched; {positions_only}"
    elif stale:
        reason = (
            f"snapshot is {_plural(age, 'day')} old against a {max_age}-day "
            f"limit; {positions_only}, no entries"
        )
    else:
        reason = (
            f"{_plural(len(candidates), 'candidate')} from "
            f"{_plural(len(members), 'member')}, snapshot "
            f"{_plural(age, 'day')} old"
        )

    return Universe(
        candidates=candidates,
        snapshot_age_days=age,
        stale=stale,
        reason=reason,
        members=sorted(members),
        sectors=sectors,
        rotation=rotation,
        notes=notes + picked_notes,
    )
