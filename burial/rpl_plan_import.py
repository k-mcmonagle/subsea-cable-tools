"""Import a burial plan from the events on a registered RPL (no Qt).

Most RPLs already carry the burial plan as point events: ``PLDN`` /
``PLUP`` for the plough, ``Start PLB`` / ``End PLB`` for post-lay burial,
sometimes ``Start surface lay`` / ``End skip``. A row can carry more than
one (``PLUP / Start PLB`` is a tool transition at one KP). This module:

1. ``detect_tokens`` — reads the boundary tokens in a cell of text, in the
   order they are written, from a broad vocabulary of phrasings.
2. ``walk`` — walks the RPL positions in the plan's travel direction as a
   state machine: a start opens a section, the matching end closes it, a
   start of another tool while one is open is a tool change. Everything
   between burial sections is a skip. Oddities (an end with no start, a
   start that is never closed…) become per-row issues, never exceptions.
3. ``auto_swap`` — whether the events read the wrong way round for the
   plan's direction (e.g. an RPL written from the other landing).
4. ``paint`` — the "these rows are plough" edit: boundary tokens that make
   the selected span one section while the rest stays as it was.
5. ``build_result`` — the same :class:`plan_import.ImportResult` the table
   import produces, so review, overlay and commit are shared.

RPLs without boundary events may carry the plan per segment instead, in
the ProtectionMethod column ("Plough 1.0 m", "PLB", "Surface laid").
``protection_tokens`` turns each change of method between consecutive
segments into the same boundary tokens, so both sources share steps 2–5.

Positions are placed on the plan route by the caller (by seabed position,
so an RPL other than the plan's own translates correctly); rows carry that
plan KP in ``RplRow.kp``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import plan_import as pi
from . import schema

START = "start"
END = "end"

M_PLOUGH = schema.METHOD_PLOUGH
M_TRENCHER = schema.METHOD_TRENCHER
M_MFE = schema.METHOD_MFE
M_BURIAL = "burial"      # burial with no tool named: the plan default tool
M_SKIP = "skip"
BURIAL_METHODS = (M_PLOUGH, M_TRENCHER, M_MFE, M_BURIAL)
METHODS = BURIAL_METHODS + (M_SKIP,)

METHOD_LABELS = {
    M_PLOUGH: "Plough",
    M_TRENCHER: "PLB / trencher",
    M_MFE: "MFE",
    M_BURIAL: "Burial (tool not stated)",
    M_SKIP: "Skip",
}
# The same, as they read mid-sentence ("the plough section").
SECTION_NAMES = {M_PLOUGH: "plough", M_TRENCHER: "PLB", M_MFE: "MFE",
                 M_BURIAL: "burial", M_SKIP: "skip"}
_TOKEN_LABELS = {
    (START, M_PLOUGH): "PLDN", (END, M_PLOUGH): "PLUP",
    (START, M_TRENCHER): "Start PLB", (END, M_TRENCHER): "End PLB",
    (START, M_MFE): "Start MFE", (END, M_MFE): "End MFE",
    (START, M_BURIAL): "Start burial", (END, M_BURIAL): "End burial",
    (START, M_SKIP): "Start skip", (END, M_SKIP): "End skip",
}

LEVEL_WARN = "warn"
LEVEL_INFO = "info"

_KP_TOL = 5e-7
# Positions further than this from the plan route are flagged (the same
# tolerance the geometry re-referencing uses).
OFFSET_TOL_M = 25.0


@dataclass(frozen=True)
class Token:
    kind: str       # START / END
    method: str     # one of METHODS

    @property
    def label(self) -> str:
        return _TOKEN_LABELS[(self.kind, self.method)]

    def swapped(self) -> "Token":
        return Token(END if self.kind == START else START, self.method)


@dataclass
class RplRow:
    seq: int
    pos_no: Optional[int] = None
    event: str = ""
    remarks: str = ""
    stated_kp: Optional[float] = None   # KP printed on the RPL
    kp: Optional[float] = None          # on the plan route (None = not placed)
    offset_m: Optional[float] = None    # distance from the plan route
    protection: str = ""                # ProtectionMethod of the segment to the next row

    @property
    def name(self) -> str:
        return f"Pos {self.pos_no}" if self.pos_no is not None else f"row {self.seq + 1}"

    @property
    def number(self) -> int:
        return int(self.pos_no) if self.pos_no is not None else self.seq + 1

    def text(self, fields: Sequence[str] = ("event", "remarks")) -> str:
        return " / ".join(v for v in (getattr(self, f, "") or "" for f in fields) if v.strip())


@dataclass
class Issue:
    level: str
    text: str


@dataclass
class Span:
    lo: float
    hi: float
    method: str
    first: int      # row index opening the span
    last: int       # row index closing it


@dataclass
class Walk:
    spans: List[Span] = field(default_factory=list)
    after: Dict[int, str] = field(default_factory=dict)   # state after each row
    issues: Dict[int, List[Issue]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def add(self, index: int, level: str, text: str) -> None:
        self.issues.setdefault(index, []).append(Issue(level, text))

    def count(self, level: str = LEVEL_WARN) -> int:
        return sum(1 for items in self.issues.values() for i in items if i.level == level)


# ---------------------------------------------------------------- vocabulary
# Cells are normalised to lower-case words separated by single spaces
# ("PL-DN" → "pl dn", "Post-lay burial" → "post lay burial").
_BURIAL_SUFFIX = r"(?:\s+burial)?"
_NOUNS = {
    M_PLOUGH: r"(?:plough|plow)(?:ing)?" + _BURIAL_SUFFIX,
    M_TRENCHER: (r"(?:plib|plb|post\s?lay(?:\s+(?:inspection\s+and\s+)?burial)?"
                 r"|jet(?:ting)?|trench(?:ing|er)?|rov\s+(?:burial|jet(?:ting)?))"
                 + _BURIAL_SUFFIX),
    M_MFE: r"(?:mfe|mass\s+flow(?:\s+excavat\w*)?)" + _BURIAL_SUFFIX,
    M_BURIAL: r"(?:burial|bury(?:ing)?|cable\s+burial)",
    M_SKIP: (r"(?:skip|surface\s+la(?:y|id)|no\s+burial|non\s+burial|unburied)"
             r"(?:\s+(?:section|zone|area))?"),
}
_START_VERB = r"(?:start(?:s|ed|ing)?|begin(?:s|ning)?|commence(?:s|d|ment)?|resume(?:s|d)?)"
_END_VERB = (r"(?:end(?:s|ed)?|stop(?:s|ped)?|finish(?:es|ed)?|cease(?:s|d)?"
             r"|complete(?:s|d)?|terminate(?:s|d)?)")
_GLUE = r"(?:\s+(?:of|the|to|with))*\s+"
# Abbreviations and tool-specific phrasings.
_SPECIAL = (
    (START, M_PLOUGH, r"pl\s?(?:dn|down)|plough\s+(?:down|deploy(?:ed|ment)?|landed|lowered"
                      r"|touch\s?down)|plow\s+(?:down|deployed)"
                      r"|(?:deploy|lower|land)\s+(?:the\s+)?plough"),
    (END, M_PLOUGH, r"pl\s?up|plough\s+(?:up|recover(?:ed|y)?|lift(?:ed)?|raised)"
                    r"|plow\s+(?:up|recovered)|(?:recover|lift|raise)\s+(?:the\s+)?plough"),
    (START, M_TRENCHER, r"s\s?o?\s?plb|trencher\s+(?:down|deploy(?:ed|ment)?|launch(?:ed)?)"),
    (END, M_TRENCHER, r"e\s?o?\s?plb|trencher\s+(?:up|recover(?:ed|y)?)"),
    (START, M_BURIAL, r"s\s?o\s?b"),
    (END, M_BURIAL, r"e\s?o\s?b"),
)
_CROSSING = re.compile(r"(?<![a-z0-9])(?:crossing|xing|x\s+ing)(?![a-z0-9])")


def _compile() -> List[Tuple[str, str, "re.Pattern"]]:
    edge_l, edge_r = r"(?<![a-z0-9])", r"(?![a-z0-9])"
    out = []
    for kind, method, pattern in _SPECIAL:
        out.append((kind, method, re.compile(edge_l + "(?:" + pattern + ")" + edge_r)))
    for method, noun in _NOUNS.items():
        for kind, verb in ((START, _START_VERB), (END, _END_VERB)):
            out.append((kind, method, re.compile(edge_l + verb + _GLUE + noun + edge_r)))
            out.append((kind, method, re.compile(edge_l + noun + r"\s+" + verb + edge_r)))
    return out


_PATTERNS = _compile()


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).split())


def detect_tokens(text: str) -> List[Token]:
    """Boundary tokens in ``text``, in the order written (longest match wins)."""
    norm = normalise(text)
    if not norm:
        return []
    hits = []
    for kind, method, pattern in _PATTERNS:
        for match in pattern.finditer(norm):
            hits.append((match.start(), -(match.end() - match.start()), match.end(),
                         Token(kind, method)))
    hits.sort(key=lambda h: (h[0], h[1]))
    out: List[Token] = []
    reach = -1
    for start, _neg, end, token in hits:
        if start < reach:
            continue
        reach = end
        if not out or out[-1] != token:
            out.append(token)
    return out


def is_crossing(text: str) -> bool:
    return _CROSSING.search(normalise(text)) is not None


_PROTECTION_SKIP = ("skip", "no burial", "not buried", "unburied", "non burial", "no bury",
                    "surface lay", "surface laid", "surface", "exposed", "none", "nil", "n a")


def classify_protection(value: str) -> str:
    """Method (or ``M_SKIP``) for a segment's ProtectionMethod text.

    Skip wording wins ("No burial", "Surface laid", "Not buried"); then the
    tool named ("Plough 1.0 m", "PLB", "Jetting", "MFE"); then plain burial
    wording ("Buried", "Burial 1.5 m"). Anything else — blank, rock
    placement, mattresses, articulated pipe — is a skip (no burial).
    """
    text = normalise(value)
    if not text:
        return M_SKIP
    if any(pi._has_word(text, w) for w in _PROTECTION_SKIP):
        return M_SKIP
    for words, method in ((("plough", "plow", "ploughed", "plowed", "ploughing"), M_PLOUGH),
                          (("mfe", "mass flow"), M_MFE),
                          (("plb", "plib", "post lay", "postlay", "jet", "jetted", "jetting",
                            "trench", "trenched", "trencher", "trenching", "rov"), M_TRENCHER),
                          (("bury", "burial", "buried", "burying"), M_BURIAL)):
        if any(pi._has_word(text, w) for w in words):
            return method
    return M_SKIP


def protection_values(rows: Sequence[RplRow]) -> List[Tuple[str, int]]:
    """``[(value, segment count)]`` in route order (blank values left out)."""
    counts: Dict[str, int] = {}
    for row in rows[:-1]:
        value = (row.protection or "").strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    return list(counts.items())


def has_burial_protection(rows: Sequence[RplRow]) -> bool:
    return any(classify_protection(v) != M_SKIP for v, _n in protection_values(rows))


def protection_tokens(rows: Sequence[RplRow],
                      value_map: Optional[Dict[str, str]] = None) -> Dict[int, List[Token]]:
    """Boundary tokens where the segments' protection method changes.

    ``rows`` are in RPL order; row *k* carries the segment from *k* to
    *k + 1*. ``value_map`` (value → method, casefolded keys) overrides
    :func:`classify_protection`. Consecutive segments of the same method
    form one section; skip needs no tokens (gaps are skips).
    """
    value_map = {k.casefold(): v for k, v in (value_map or {}).items()}
    out: Dict[int, List[Token]] = {}
    prev = M_SKIP
    for index, row in enumerate(rows):
        value = (row.protection or "").strip() if index < len(rows) - 1 else ""
        cur = value_map.get(value.casefold()) or classify_protection(value)
        if cur not in METHODS:
            cur = M_SKIP
        if cur != prev:
            tokens = []
            if prev != M_SKIP:
                tokens.append(Token(END, prev))
            if cur != M_SKIP:
                tokens.append(Token(START, cur))
            out[index] = tokens
        prev = cur
    return out


def auto_tokens(rows: Sequence[RplRow],
                fields: Sequence[str] = ("event", "remarks")) -> Dict[int, List[Token]]:
    """``{row index: tokens}`` read from the chosen text fields (event first)."""
    out: Dict[int, List[Token]] = {}
    for index, row in enumerate(rows):
        tokens: List[Token] = []
        for name in fields:
            for token in detect_tokens(getattr(row, name, "") or ""):
                if token not in tokens:
                    tokens.append(token)
        if tokens:
            out[index] = tokens
    return out


def effective_tokens(auto: Dict[int, List[Token]], overrides: Dict[int, List[Token]],
                     swap: bool = False) -> Dict[int, List[Token]]:
    """Auto tokens (start/end swapped and reversed within a row when
    ``swap``) with the user's per-row overrides on top, taken as-is."""
    out: Dict[int, List[Token]] = {}
    for index, tokens in auto.items():
        out[index] = [t.swapped() for t in reversed(tokens)] if swap else list(tokens)
    for index, tokens in overrides.items():
        out[index] = list(tokens)
    return {i: t for i, t in out.items() if t}


def travel_order(rows: Sequence[RplRow], direction: int = 1) -> List[int]:
    sign = -1.0 if int(direction or 1) < 0 else 1.0
    placed = [i for i, r in enumerate(rows) if r.kp is not None]
    return sorted(placed, key=lambda i: (sign * float(rows[i].kp), sign * rows[i].seq))


def _same(a: str, b: str) -> bool:
    return a == b or M_BURIAL in (a, b)


# ---------------------------------------------------------------- the walk
def walk(rows: Sequence[RplRow], tokens: Dict[int, List[Token]], direction: int = 1) -> Walk:
    """Pair the boundary tokens into sections along the plan's travel direction."""
    out = Walk()
    for index, items in tokens.items():
        if items and rows[index].kp is None:
            out.add(index, LEVEL_WARN, "Could not be placed on the plan route — its "
                                       "events are left out.")
    order = travel_order(rows, direction)
    burial: Optional[List] = None   # [method, kp, row index]
    skip: Optional[List] = None     # [kp, row index]
    resume = ""

    def close(kind_method: str, opened: List, kp: float, index: int) -> None:
        start_kp, first = opened[-2], opened[-1]
        lo, hi = sorted((float(start_kp), float(kp)))
        if hi - lo > _KP_TOL:
            out.spans.append(Span(lo, hi, kind_method, first, index))

    for index in order:
        row = rows[index]
        kp = float(row.kp)
        items = tokens.get(index, [])
        for n, token in enumerate(items):
            later_start = any(t.kind == START and t.method != M_SKIP for t in items[n + 1:])
            if token.method == M_SKIP:
                if token.kind == START:
                    if skip is not None:
                        out.add(index, LEVEL_WARN, f"Start skip while the skip from "
                                f"{rows[skip[1]].name} is still open — ignored.")
                        continue
                    if burial is not None:
                        resume = burial[0]
                        close(burial[0], burial, kp, index)
                        burial = None
                    skip = [kp, index]
                else:
                    if skip is None:
                        out.add(index, LEVEL_WARN, "End skip with no open skip — ignored.")
                        continue
                    close(M_SKIP, skip, kp, index)
                    skip = None
                    if resume and not later_start and burial is None:
                        burial = [resume, kp, index]
                    resume = ""
            elif token.kind == START:
                if burial is not None:
                    open_label = SECTION_NAMES[burial[0]]
                    if _same(burial[0], token.method):
                        out.add(index, LEVEL_WARN, f"{token.label} while the {open_label} "
                                f"section from {rows[burial[2]].name} is still open — ignored.")
                        continue
                    out.add(index, LEVEL_WARN, f"{token.label} while the {open_label} section "
                            f"from {rows[burial[2]].name} is still open (no "
                            f"{Token(END, burial[0]).label}) — read as a tool change here.")
                    close(burial[0], burial, kp, index)
                if skip is not None:
                    close(M_SKIP, skip, kp, index)
                    skip = None
                resume = ""
                burial = [token.method, kp, index]
            else:
                if burial is not None:
                    method = burial[0]
                    if not _same(method, token.method):
                        out.add(index, LEVEL_WARN, f"{token.label} closes the "
                                f"{SECTION_NAMES[method]} section opened at "
                                f"{rows[burial[2]].name}.")
                    elif method == M_BURIAL:
                        method = token.method      # "Start burial … PLUP" is plough
                    close(method, burial, kp, index)
                    burial = None
                elif skip is not None:
                    out.add(index, LEVEL_WARN, f"{token.label} inside a skip — ignored.")
                else:
                    out.add(index, LEVEL_WARN, f"{token.label} with no open section — ignored.")
        out.after[index] = burial[0] if burial is not None else (M_SKIP if skip is not None else "")

    if order:
        last = order[-1]
        if burial is not None:
            out.add(burial[2], LEVEL_WARN, f"{Token(START, burial[0]).label} is never closed — "
                    f"the section runs to the last RPL position ({rows[last].name}).")
            close(burial[0], burial, float(rows[last].kp), last)
        if skip is not None:
            close(M_SKIP, skip, float(rows[last].kp), last)
    # A crossing inside a burial section is worth a second look.
    for index in order:
        if tokens.get(index) or out.after.get(index) not in (M_PLOUGH, M_TRENCHER, M_MFE, M_BURIAL):
            continue
        if is_crossing(rows[index].text()):
            out.add(index, LEVEL_INFO, f"Crossing inside a "
                    f"{SECTION_NAMES[out.after[index]]} section — check whether a "
                    "lift (PLUP / PLDN) is missing.")
    return out


def auto_swap(rows: Sequence[RplRow], auto: Dict[int, List[Token]], direction: int = 1) -> bool:
    """True when the events pair up better with starts and ends swapped
    (events written for the opposite lay direction, or labelled the wrong way)."""
    if not auto:
        return False
    plain = walk(rows, effective_tokens(auto, {}, False), direction).count()
    flipped = walk(rows, effective_tokens(auto, {}, True), direction).count()
    return flipped < plain


def _state_before(order: List[int], position: int, after: Dict[int, str]) -> str:
    return after.get(order[position - 1], "") if position > 0 else ""


def paint(rows: Sequence[RplRow], tokens: Dict[int, List[Token]], selected: Iterable[int],
          method: str, direction: int = 1) -> Dict[int, List[Token]]:
    """Overrides making the selected rows' span one ``method`` section.

    The first and last selected rows (in travel order) get the boundary
    tokens, every row between loses its tokens, and the sections either
    side are closed and re-opened so the rest of the plan is unchanged.
    Raises ValueError for fewer than two placed rows.
    """
    order = travel_order(rows, direction)
    chosen: Set[int] = set(selected)
    positions = [p for p, index in enumerate(order) if index in chosen]
    if len(positions) < 2:
        raise ValueError("Select at least two rows: the first and last of the section.")
    first_pos, last_pos = positions[0], positions[-1]
    first, last = order[first_pos], order[last_pos]
    if abs(float(rows[first].kp) - float(rows[last].kp)) <= _KP_TOL:
        raise ValueError("The selected rows are at the same KP.")
    state = walk(rows, tokens, direction).after
    before = _state_before(order, first_pos, state)
    after = state.get(last, "")
    updates: Dict[int, List[Token]] = {order[p]: [] for p in range(first_pos + 1, last_pos)}
    head: List[Token] = []
    if before and before != method:
        head.append(Token(END, before))
    if before != method:
        head.append(Token(START, method))
    tail: List[Token] = []
    if after != method:
        tail.append(Token(END, method))
        if after:
            tail.append(Token(START, after))
    updates[first] = head
    updates[last] = tail
    return updates


def set_token(current: Sequence[Token], token: Token) -> List[Token]:
    """``current`` with ``token`` replacing any token of the same kind
    (ends first, then starts: the order a transition reads in)."""
    kept = [t for t in current if t.kind != token.kind]
    items = kept + [token]
    return [t for t in items if t.kind == END] + [t for t in items if t.kind == START]


# ---------------------------------------------------------------- the result
def _clip(lo: float, hi: float, window: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    lo, hi = max(lo, window[0]), min(hi, window[1])
    return (lo, hi) if hi - lo > _KP_TOL else None


def _gaps(covered: List[Tuple[float, float]], window: Tuple[float, float]
          ) -> List[Tuple[float, float]]:
    out = []
    cursor = window[0]
    for lo, hi in sorted(covered):
        if lo > cursor + _KP_TOL:
            out.append((cursor, lo))
        cursor = max(cursor, hi)
    if window[1] > cursor + _KP_TOL:
        out.append((cursor, window[1]))
    return out


def build_result(rows: Sequence[RplRow], result_walk: Walk, tool_map: Dict[str, str],
                 window: Tuple[float, float], direction: int = 1,
                 protection_notes: bool = False) -> pi.ImportResult:
    """Burial and skip ranges inside ``window`` (clipped to it) as an
    :class:`plan_import.ImportResult`. The whole window is covered — gaps
    are skips — so an overlay import replaces exactly the window.
    ``protection_notes`` notes each burial section's ProtectionMethod
    values (when the plan was read from that column)."""
    result = pi.ImportResult()
    lo_w, hi_w = sorted(float(v) for v in window)
    if hi_w - lo_w <= _KP_TOL:
        result.errors.append("The import window is empty: set a KP range.")
        return result
    win = (lo_w, hi_w)
    notes = list(result_walk.notes)
    burial_spans = [s for s in result_walk.spans if s.method != M_SKIP]
    skip_spans = [s for s in result_walk.spans if s.method == M_SKIP]
    if not burial_spans and skip_spans:
        # Only skips are marked: everything else is burial with the plan default tool.
        notes.append("The RPL marks only skips, so every stretch outside them is read "
                     "as burial with the plan default tool.")
        first_row = skip_spans[0].first
        for lo, hi in _gaps([(s.lo, s.hi) for s in skip_spans], win):
            burial_spans.append(Span(lo, hi, M_BURIAL, first_row, first_row))
    outside = 0
    for span in burial_spans + skip_spans:
        clipped = _clip(span.lo, span.hi, win)
        if clipped is None:
            outside += 1
            continue
        start_row = rows[span.first]
        action = pi.ACTION_SKIP if span.method == M_SKIP else pi.ACTION_BURY
        tool_id = tool_map.get(span.method, "") if action == pi.ACTION_BURY else ""
        note = start_row.text() if span.method == M_SKIP else ""
        if protection_notes and span.method != M_SKIP:
            lo_i, hi_i = sorted((span.first, span.last))
            values = [(rows[i].protection or "").strip() for i in range(lo_i, hi_i)]
            values = list(dict.fromkeys(v for v in values if v))
            note = ("Protection: " + "; ".join(values)) if values else ""
        result.ranges.append(pi.ImportRange(start_row.number, clipped[0], clipped[1], action,
                                            tool_id, note))
    for lo, hi in _gaps([(r.start_kp, r.end_kp) for r in result.ranges], win):
        result.ranges.append(pi.ImportRange(0, lo, hi, pi.ACTION_SKIP))
    if outside:
        result.warnings.append(f"{outside} section(s) lie outside the import window "
                               f"KP {schema.format_kp(lo_w)}–{schema.format_kp(hi_w)} "
                               "and were left out.")
    for index in sorted(result_walk.issues, key=lambda i: rows[i].kp if rows[i].kp is not None
                        else float("inf")):
        row = rows[index]
        where = row.name + (f" (KP {schema.format_kp(row.kp)})" if row.kp is not None else "")
        for issue in result_walk.issues[index]:
            if issue.level == LEVEL_WARN:
                result.warnings.append(f"{where}: {issue.text}")
    far = [r for r in rows if r.offset_m is not None and r.offset_m > OFFSET_TOL_M
           and r.kp is not None and lo_w - _KP_TOL <= r.kp <= hi_w + _KP_TOL]
    if far:
        worst = max(r.offset_m for r in far)
        result.warnings.append(
            f"{len(far)} RPL position(s) in the window lie more than {OFFSET_TOL_M:.0f} m from "
            f"the plan route (up to {worst:.0f} m): the routes differ there, so check "
            "those events' KPs.")
    result.warnings = notes + result.warnings
    return pi.finalise(result, direction)
