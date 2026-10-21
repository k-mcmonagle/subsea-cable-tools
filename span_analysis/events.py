# -*- coding: utf-8 -*-
"""Events on the Span & Slack profile: what happens where along the route.

Sources, each placed at the measured KP the analysis uses:

* the analysed RPL's events (``Event`` and ``Remarks`` of its positions),
  sorted by the Workbench event rules into cable bodies, crossings,
  operations (PLDN, PLUP, burial), alter courses and the rest;
* what is in the model: bodies, protection and seabed features;
* Route Comments on the same route (points and KP ranges);
* the Burial Planner plan for the same RPL (PLDN / PLUP or start / end of
  burial, burial and skip sections, crossings and other hazards);
* the scenario's own markers.

Items made on another KP basis (another RPL, start KP or KP convention, or a
route edited since) are left out and counted in :attr:`EventSet.notes`, never
shifted. Events do not change the model: :func:`check_findings` compares the
RPL's bodies and crossings with it and returns information findings.

:func:`layout_labels` places labels on rows of a lane in screen pixels, so
they never overlap, whatever the zoom.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..cable_seabed.core import INFO, RangeFinding

SRC_RPL, SRC_MODEL, SRC_COMMENT, SRC_BURIAL, SRC_MARKER = "rpl", "model", "comment", "burial", "marker"
SOURCES = (
    (SRC_RPL, "RPL events", "The analysed RPL's events (Event column), sorted by the Workbench event rules.", True),
    (SRC_MODEL, "In the model (bodies, protection, features)", "What the analysis models: bodies, protection "
     "ranges and seabed features.", True),
    (SRC_COMMENT, "Route Comments", "Comments on this route from the project's comment register.", True),
    (SRC_BURIAL, "Burial plan", "PLDN / PLUP, burial and skip sections and crossings from the Burial Planner plan "
     "for this RPL.", True),
    (SRC_MARKER, "My markers", "Markers saved with this scenario (right-click the profile to add one).", True),
)
SOURCE_NAMES = {key: text for key, text, _tip, _on in SOURCES}

GROUP_BODIES, GROUP_CROSSINGS, GROUP_OPERATIONS, GROUP_ALTER, GROUP_OTHER = (
    "bodies", "crossings", "operations", "alter_courses", "other")
# Groups sort RPL and burial events (what is in the model, comments and markers always show with their source).
GROUPS = (
    (GROUP_BODIES, "Cable bodies (repeaters, joints, BUs)", True),
    (GROUP_CROSSINGS, "Crossings", True),
    (GROUP_OPERATIONS, "Operations (plough, burial)", True),
    (GROUP_ALTER, "Alter courses", False),
    (GROUP_OTHER, "Other events", True),
)

# Body events the model should carry as bodies (BMHs are ashore; transitions are cable sections).
CHECKED_BODIES = frozenset({"repeater", "bu", "equaliser", "joint"})
TYPE_LABELS = {
    "repeater": "Repeater", "bu": "Branching unit", "equaliser": "Equaliser", "joint": "Joint",
    "bmh": "Beach manhole", "transition": "Transition", "crossing": "Crossing", "route_branch": "Route branch",
    "operations": "Operations", "alter_course": "Alter course", "water_depth": "Water depth mark",
    "boundary": "Boundary", "other": "Event",
}
MATCH_M = 50.0          # an RPL event within this of a modelled item is that item
START_KP_TOL_KM = 0.0005

COLOURS = {
    GROUP_BODIES: (150, 105, 0), GROUP_CROSSINGS: (0, 121, 107), GROUP_OPERATIONS: (57, 73, 171),
    GROUP_ALTER: (117, 117, 117), GROUP_OTHER: (80, 80, 80),
    "comment": (21, 101, 192), "comment_closed": (150, 150, 150), "marker": (173, 20, 87),
    "feature": (121, 85, 72), "protection": (96, 125, 139),
    "burial": (46, 125, 50), "skip": (141, 110, 99), "insufficient_info": (150, 150, 150),
}
# Labels go to the lane in this order (lower first): what is in the model, then the RPL's bodies ...
PRIORITY = {GROUP_BODIES: 10, GROUP_CROSSINGS: 20, "comment": 25, "marker": 25, GROUP_OPERATIONS: 30,
            GROUP_OTHER: 50, GROUP_ALTER: 70}


@dataclass
class ProfileEvent:
    kp: float                          # measured KP (km), the analysis's
    kp_end: Optional[float]            # a KP range's other end (None: a point)
    source: str                        # SRC_*
    group: str                         # GROUP_* (RPL and burial events), else the source's own
    label: str
    detail: str                        # tooltip lines
    colour: Tuple[int, int, int]
    priority: int = 50
    kind: str = ""                     # subtype: repeater, crossing, PLDN, feature kind ...
    modelled: Optional[bool] = None    # RPL bodies and crossings: carried by the model (None: n/a)
    muted: bool = False                # drawn grey (a closed comment)
    key: str = ""                      # "comment:<id>", "marker:<n>" ... for click actions
    nearest: Optional[Tuple[str, float]] = None   # the nearest modelled item (label, m) when not modelled

    @property
    def is_range(self) -> bool:
        return self.kp_end is not None and abs(float(self.kp_end) - float(self.kp)) > 1e-7

    @property
    def lo(self) -> float:
        return min(self.kp, self.kp_end) if self.is_range else self.kp

    @property
    def hi(self) -> float:
        return max(self.kp, self.kp_end) if self.is_range else self.kp


@dataclass
class EventSet:
    events: List[ProfileEvent] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    burial_plans: List[Tuple[str, str]] = field(default_factory=list)   # (plan_id, name) for this RPL
    burial_plan: str = ""              # the plan shown


def _kp_text(kp: float) -> str:
    return f"KP {kp:.3f}"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
_FALLBACK_BODIES = (("repeater", r"\bREP|REPEATER|\bRPT"), ("bu", r"\bBU\b|BRANCHING"),
                    ("joint", r"JOINT|\bUJ\b|\bJB\b|\bJT\b|SPLICE"), ("equaliser", r"EQUALI[SZ]"))
_FALLBACK_ALTER = r"\bA\s*/\s*C\b|ALTER\s*COURSE"


def classify_event(text: str, classify: Callable) -> Tuple[str, str, bool]:
    """``(group, kind, matched by the Workbench rules)`` of an RPL event's text. Text no Workbench
    rule matches is tried against common names for bodies and alter courses (``A/C 3``, ``RPT 12``)."""
    import re

    c = classify(text)
    matched = bool(getattr(c, "matched", True))
    if getattr(c, "is_assembly", False):
        return GROUP_BODIES, c.body_type or "body", matched
    geo = getattr(c, "geo_type", "") or ""
    if matched:
        if geo == "crossing":
            return GROUP_CROSSINGS, "crossing", True
        if geo == "operations":
            return GROUP_OPERATIONS, "operations", True
        if geo == "alter_course":
            return GROUP_ALTER, "alter_course", True
        return GROUP_OTHER, geo or "other", True
    upper = text.upper()
    for kind, pattern in _FALLBACK_BODIES:
        if re.search(pattern, upper):
            return GROUP_BODIES, kind, False
    if re.search(_FALLBACK_ALTER, upper):
        return GROUP_ALTER, "alter_course", False
    return GROUP_OTHER, "other", True


def rpl_events(model, measured_kps: Sequence[float], classify: Callable) -> List[ProfileEvent]:
    """The RPL positions that carry an event, at their measured KPs."""
    out: List[ProfileEvent] = []
    for i, point in enumerate(model.points):
        text = str(point.event or "").strip()
        if not text or i >= len(measured_kps):
            continue
        kp = float(measured_kps[i])
        if not math.isfinite(kp):
            continue
        group, kind, matched = classify_event(text, classify)
        attrs = getattr(point, "attrs", None) or {}
        remarks = str(attrs.get("Remarks") or "").strip()
        lines = [f"RPL event '{text}' ({TYPE_LABELS.get(kind, kind)}"
                 + ("" if matched else ", by common names: no Workbench event rule matches it") + ")",
                 f"{_kp_text(kp)} (measured)" + (f"; printed KP {float(point.dist_cum_km):.3f}"
                                                  if point.dist_cum_km is not None else "")]
        pos = getattr(point, "pos_no", None)
        if pos not in (None, ""):
            lines.append(f"Position {pos}")
        if remarks:
            lines.append(f"Remarks: {remarks}")
        out.append(ProfileEvent(kp, None, SRC_RPL, group, text, "\n".join(lines), COLOURS[group],
                                PRIORITY.get(group, 50), kind, key=f"rpl:{i}"))
    return out


def model_events(result) -> List[ProfileEvent]:
    """Bodies, protection and seabed features of a result (all in the model)."""
    out: List[ProfileEvent] = []
    st = result.stations
    for k, b in enumerate(result.bodies):
        body = b.body
        lines = [f"{body.name} ({body.kind}), in the model", _kp_text(b.kp_km),
                 f"Weight in water {body.weight_kn:.2f} kN, rigid length {body.length_m:.1f} m"]
        if body.placement:
            lines.append(body.placement)
            if math.isfinite(body.anchor_kp_km):
                lines.append(f"Laid with 0.1% more or less slack than planned between the anchor and here, it "
                             f"lands about {abs(b.kp_km - body.anchor_kp_km):,.1f} m away")
        out.append(ProfileEvent(b.kp_km, None, SRC_MODEL, GROUP_BODIES, body.name, "\n".join(lines),
                                COLOURS[GROUP_BODIES], 5, "body", modelled=True, key=f"body:{k}"))
    index = st.protection_index
    for j, item in enumerate(st.protection):
        idx = np.nonzero(index == j)[0] if index is not None else []
        if not len(idx):
            continue
        lo, hi = float(st.kp_km[idx[0]]), float(st.kp_km[idx[-1]])
        out.append(ProfileEvent(lo, hi, SRC_MODEL, "protection", item.name,
                                f"{item.name} ({item.kind}), in the model\nKP {lo:.3f} to {hi:.3f}",
                                COLOURS["protection"], 40, "protection", modelled=True, key=f"protection:{j}"))
    for j, feature in enumerate(st.features):
        a, b = feature.extent_m
        lo, hi = feature.kp_km + a / 1000.0, feature.kp_km + b / 1000.0
        out.append(ProfileEvent(feature.kp_km, None, SRC_MODEL, "feature", feature.label(),
                                f"Seabed feature '{feature.label()}' ({feature.kind}), in the model\n"
                                f"{_kp_text(feature.kp_km)}, from KP {lo:.3f} to {hi:.3f}",
                                COLOURS["feature"], 8, feature.kind, modelled=True, key=f"feature:{j}"))
    return out


def marker_events(markers: Sequence[Dict]) -> List[ProfileEvent]:
    out = []
    for n, m in enumerate(markers or []):
        try:
            kp = float(m.get("kp"))
        except (TypeError, ValueError):
            continue
        end = m.get("kp_end")
        try:
            end = float(end) if end not in (None, "") else None
        except (TypeError, ValueError):
            end = None
        label = str(m.get("label") or "Marker")
        note = str(m.get("note") or "").strip()
        where = f"KP {min(kp, end):.3f} to {max(kp, end):.3f}" if end is not None else _kp_text(kp)
        out.append(ProfileEvent(kp, end, SRC_MARKER, "marker", label,
                                f"Marker '{label}' (this scenario)\n{where}" + (f"\n{note}" if note else ""),
                                COLOURS["marker"], PRIORITY["marker"], "marker", key=f"marker:{n}"))
    return out


@dataclass
class RouteIdentity:
    """The analysed route, as other tools record theirs."""

    rpl_id: str = ""
    layer_id: str = ""
    route_hash: str = ""
    start_kp_km: float = math.nan
    convention: str = ""               # e.g. geodesic:WGS84 (route_comments.route_ref.kp_convention_token)
    length_m: float = math.nan         # the whole route (not a KP window)
    kp_mode: str = ""                  # "cartesian" or "ellipsoidal" (Burial Planner terms)
    grid_crs: str = ""


def comment_events(rows: Sequence[Dict], route: RouteIdentity, closed: Sequence[str],
                   show_closed: bool) -> Tuple[List[ProfileEvent], List[str]]:
    """Route Comments on this route, on the same KP basis."""
    from ..route_comments import model as cm

    closed_set = {str(s).casefold() for s in closed}
    out: List[ProfileEvent] = []
    other_route = basis = edited = hidden = no_kp = 0
    for row in rows or []:
        same = ((route.rpl_id and str(row.get("rpl_id") or "") == route.rpl_id)
                or (route.layer_id and str(row.get("route_layer_id") or "") == route.layer_id)
                or (route.route_hash and str(row.get("route_hash") or "") == route.route_hash))
        if not same:
            other_route += 1
            continue
        start = row.get("start_kp_km")
        token = str(row.get("kp_convention") or "")
        if (start is None or not math.isfinite(route.start_kp_km)
                or abs(float(start) - route.start_kp_km) > START_KP_TOL_KM
                or (token and route.convention and token != route.convention)):
            basis += 1
            continue
        length = row.get("route_length_m")
        if length is not None and math.isfinite(route.length_m) and abs(float(length) - route.length_m) > 1.0:
            edited += 1
            continue
        kp = row.get("kp_km")
        if kp is None:
            no_kp += 1
            continue
        status = str(row.get("status") or "")
        is_closed = status.casefold() in closed_set
        if is_closed and not show_closed:
            hidden += 1
            continue
        kind = row.get("ref_kind") or ""
        end = row.get("kp_end_km") if kind == cm.REF_KP_RANGE else None
        ref = cm.ref_label(row.get("ref_no"))
        text = " ".join(str(row.get("text") or "").split())
        label = (ref + " " if ref else "") + (text[:40] + ("…" if len(text) > 40 else "") if text else "Comment")
        where = (f"KP {min(kp, end):.3f} to {max(kp, end):.3f}" if end is not None else _kp_text(float(kp)))
        if kind == cm.REF_LOCATION and row.get("dcc_m") is not None:
            where += f", {float(row['dcc_m']):,.0f} m off the route"
        lines = [f"Route Comment {ref}".strip() + f" ({status or 'no status'}"
                 + (f", {row.get('priority')}" if row.get("priority") else "") + ")", where]
        if text:
            lines.append(text if len(text) <= 300 else text[:300] + "…")
        if row.get("tags"):
            lines.append(f"Tags: {row['tags']}")
        colour = COLOURS["comment_closed" if is_closed else "comment"]
        out.append(ProfileEvent(float(kp), float(end) if end is not None else None, SRC_COMMENT, "comment", label,
                                "\n".join(lines), colour, PRIORITY["comment"], kind or "comment", muted=is_closed,
                                key=f"comment:{row.get('comment_id') or ''}"))
    notes = []
    if basis:
        notes.append(f"{basis} Route Comment(s) on this route were made with another start KP or KP convention: "
                     "not shown (their KPs would not match).")
    if edited:
        notes.append(f"{edited} Route Comment(s) were made before the route was edited (its length differs): not "
                     "shown. Check them in Route Comments.")
    if hidden:
        notes.append(f"{hidden} closed Route Comment(s) hidden (Events ▸ Closed comments shows them).")
    if no_kp:
        notes.append(f"{no_kp} Route Comment(s) at map locations away from the route: not shown.")
    return out, notes


def read_comment_rows(register_path: str) -> Tuple[List[Dict], List[str]]:
    """``(comments of every set, closed status names)`` from a comment register (read only)."""
    from ..route_comments import model as cm
    from ..route_comments.store import CommentStore, is_register

    if not register_path or not os.path.isfile(register_path) or not is_register(register_path):
        return [], []
    store = CommentStore(register_path)
    rows: List[Dict] = []
    closed: set = set()
    for set_row in store.list_sets():
        closed |= cm.closed_statuses(store.statuses_of(set_row))
        rows.extend(dict(r) for r in store.list_comments(set_row.get("set_id")))
    return rows, sorted(closed)


def read_burial(path: str, rpl_id: str) -> Dict:
    """The Burial Planner plans for ``rpl_id`` and their events, sections and hazards.

    A plain read: no schema change, and the file's journal mode is left as
    it is (the Burial Planner's own store would set it).
    """
    out = {"plans": [], "events": {}, "sections": {}, "hazards": {}}
    if not path or not os.path.isfile(path) or not rpl_id:
        return out
    conn = sqlite3.connect(path, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "bp_plan" not in tables:
            return out
        plans = []
        for row in conn.execute("SELECT * FROM bp_plan"):
            plan = dict(row)
            try:
                params = json.loads(plan.get("params_json") or "{}")
            except (TypeError, ValueError):
                params = {}
            datum = params.get("kp_datum") if isinstance(params, dict) else None
            datum = datum if isinstance(datum, dict) else {}
            if str(plan.get("rpl_id") or "") != rpl_id and str(datum.get("rpl_id") or "") != rpl_id:
                continue
            plan["_datum"] = datum
            plans.append(plan)
        out["plans"] = plans
        for table, key in (("bp_event", "events"), ("bp_section", "sections"), ("bp_hazard", "hazards")):
            if table not in tables:
                continue
            for plan in plans:
                rows = conn.execute(f"SELECT * FROM {table} WHERE plan_id = ?", (plan["plan_id"],)).fetchall()
                out[key][plan["plan_id"]] = [dict(r) for r in rows]
    finally:
        conn.close()
    return out


def choose_plan(plans: Sequence[Dict], wanted: str = "") -> Optional[Dict]:
    """The plan asked for, else an issued one, else the latest changed."""
    if not plans:
        return None
    if wanted:
        for plan in plans:
            if plan.get("plan_id") == wanted:
                return plan
    issued = [p for p in plans if str(p.get("status") or "") == "issued"]
    pool = issued or list(plans)
    return max(pool, key=lambda p: str(p.get("modified_utc") or ""))


def burial_events(plan: Dict, events: Sequence[Dict], sections: Sequence[Dict], hazards: Sequence[Dict],
                  route: RouteIdentity, current_fingerprint: str = "") -> Tuple[List[ProfileEvent], List[str]]:
    """One Burial Planner plan's events, sections and hazards, when it shares the analysis's KP basis."""
    from ..burial import events as bev
    from ..burial import schema as bs

    name = str(plan.get("name") or "Burial plan")
    datum = plan.get("_datum") or {}
    start = datum.get("start_kp")
    mode = "cartesian" if datum.get("mode") == "cartesian" else "ellipsoidal"
    reasons = []
    if start is None or not math.isfinite(route.start_kp_km) or \
            abs(float(start) - route.start_kp_km) > START_KP_TOL_KM:
        reasons.append("another start KP")
    if route.kp_mode and mode != route.kp_mode:
        reasons.append("another KP method (geodesic or grid)")
    elif mode == "cartesian" and route.grid_crs and datum.get("grid_crs") and \
            str(datum.get("grid_crs")) != route.grid_crs:
        reasons.append("another KP grid")
    snapshot = str(plan.get("rpl_fingerprint") or "")
    if snapshot and current_fingerprint:
        core = "|".join(snapshot.split("|")[:3])
        if core != "|".join(current_fingerprint.split("|")[:3]):
            reasons.append("the RPL has been edited since its events were made")
    if reasons:
        return [], [f"Burial plan '{name}' is not shown: " + "; ".join(reasons) + ". Open it in the Burial Planner "
                    "to bring it up to date."]
    method = str(plan.get("method") or "")
    labels = bev.event_labels(list(events), list(sections), method)
    out: List[ProfileEvent] = []
    for e in events:
        kp = e.get("kp")
        if kp is None:
            continue
        label = labels.get(str(e.get("event_id") or ""), str(e.get("event_type") or ""))
        status = str(e.get("status") or "")
        lines = [f"{label}, Burial Planner plan '{name}'", _kp_text(float(kp))]
        if status and status != "confirmed":
            lines.append(f"Status: {status}")
        if e.get("notes"):
            lines.append(str(e["notes"]))
        out.append(ProfileEvent(float(kp), None, SRC_BURIAL, GROUP_OPERATIONS, label, "\n".join(lines),
                                COLOURS[GROUP_OPERATIONS], PRIORITY[GROUP_OPERATIONS], label,
                                key=f"burial_event:{e.get('event_id')}"))
    for s in sections:
        a, b = s.get("start_kp"), s.get("end_kp")
        if a is None or b is None:
            continue
        kind = str(s.get("kind") or "")
        text = bs.section_kind_label(kind, s.get("method") or method)
        lines = [f"{text}, Burial Planner plan '{name}'", f"KP {min(a, b):.3f} to {max(a, b):.3f}"]
        if s.get("conclusion"):
            lines.append(str(s["conclusion"]))
        out.append(ProfileEvent(float(a), float(b), SRC_BURIAL, GROUP_OPERATIONS, text, "\n".join(lines),
                                COLOURS.get(kind, COLOURS[GROUP_OPERATIONS]), 60, kind,
                                key=f"burial_section:{s.get('section_id')}"))
    for h in hazards:
        kp = h.get("kp")
        if kp is None:
            continue
        end = h.get("end_kp")
        end = float(end) if end is not None and abs(float(end) - float(kp)) > 1e-7 else None
        crossing = bool(h.get("crossing"))
        group = GROUP_CROSSINGS if crossing else GROUP_OTHER
        label = str(h.get("label") or ("Crossing" if crossing else "Hazard"))
        lines = [f"{'Crossing' if crossing else 'Hazard'} '{label}', Burial Planner plan '{name}'",
                 f"KP {min(kp, end):.3f} to {max(kp, end):.3f}" if end is not None else _kp_text(float(kp))]
        if h.get("risk"):
            lines.append(f"Risk: {h['risk']}")
        if h.get("crossing_angle_deg") is not None:
            lines.append(f"Crossing angle {float(h['crossing_angle_deg']):.0f}°")
        out.append(ProfileEvent(float(kp), end, SRC_BURIAL, group, label, "\n".join(lines), COLOURS[group],
                                PRIORITY[group], "crossing" if crossing else "hazard",
                                key=f"burial_hazard:{h.get('hazard_id')}"))
    return out, []


# ---------------------------------------------------------------------------
# Choosing what shows, and checking against the model
# ---------------------------------------------------------------------------
def visible_events(events: Sequence[ProfileEvent], sources: Dict[str, bool], groups: Dict[str, bool],
                   kp_lo: float, kp_hi: float) -> Tuple[List[ProfileEvent], int]:
    """The events to draw: sources and groups on, inside the analysed KPs. An RPL event that a
    modelled item carries (within :data:`MATCH_M`) is drawn once, as that item.

    Returns ``(events, outside)`` with the count left outside the KP range.
    """
    shown: List[ProfileEvent] = []
    outside = 0
    model_on = sources.get(SRC_MODEL, True)
    for e in events:
        if not sources.get(e.source, True):
            continue
        if e.source in (SRC_RPL, SRC_BURIAL) and not groups.get(e.group, True):
            continue
        if e.hi < kp_lo - 1e-9 or e.lo > kp_hi + 1e-9:
            outside += 1
            continue
        if model_on and e.source == SRC_RPL and e.modelled:
            continue   # drawn as the modelled body or feature (its tooltip names the RPL event)
        shown.append(e)
    return shown, outside


def match_model(events: Sequence[ProfileEvent], tol_m: float = MATCH_M) -> List[ProfileEvent]:
    """Copies of ``events`` with the RPL's bodies and the crossings (RPL or burial) marked as
    carried by the model or not, and each match noted on both."""
    events = [replace(e) for e in events]
    bodies = [e for e in events if e.source == SRC_MODEL and e.kind == "body"]
    features = [e for e in events if e.source == SRC_MODEL and e.group == "feature" and e.kind != "depression"]
    for e in events:
        if e.source not in (SRC_RPL, SRC_BURIAL):
            continue
        if e.source == SRC_RPL and e.group == GROUP_BODIES and e.kind in CHECKED_BODIES:
            pool = bodies
        elif e.group == GROUP_CROSSINGS:
            pool = features
        else:
            continue
        best, dist = None, math.inf
        for m in pool:
            d = _distance_m(e, m)
            if d < dist:
                best, dist = m, d
        if best is not None and dist <= tol_m:
            e.modelled = True
            e.detail += f"\nIn the model as '{best.label}' ({dist:.0f} m away)"
            best.detail += f"\n{SOURCE_NAMES[e.source]}: '{e.label}' at {_kp_text(e.kp)} ({dist:.0f} m away)"
        else:
            e.modelled = False
            near = (f"; the nearest, '{best.label}', is {dist:,.0f} m away" if best is not None and dist < 5000
                    else "")
            e.detail += f"\nNot in the model: nothing modelled within {tol_m:.0f} m{near}"
            e.nearest = (best.label, dist) if best is not None else None
    return events


def _distance_m(a: ProfileEvent, b: ProfileEvent) -> float:
    """Metres between two events (0 when one lies inside the other's range)."""
    if a.hi >= b.lo and b.hi >= a.lo:
        return 0.0
    return 1000.0 * min(abs(a.lo - b.hi), abs(b.lo - a.hi))


def check_findings(events: Sequence[ProfileEvent], kp_lo: float, kp_hi: float,
                   tol_m: float = MATCH_M) -> List[RangeFinding]:
    """Information findings for RPL bodies with no body in the model and crossings (RPL or burial)
    with no seabed feature, inside the analysed KPs (call :func:`match_model` first)."""
    out: List[RangeFinding] = []
    for e in events:
        if e.modelled is not False or e.hi < kp_lo or e.lo > kp_hi:
            continue
        nearest = e.nearest
        near = (f" (the nearest, '{nearest[0]}', is {nearest[1]:,.0f} m away)"
                if nearest is not None and nearest[1] < 5000 else "")
        if e.group == GROUP_BODIES:
            text = (f"The RPL lists '{e.label}' ({TYPE_LABELS.get(e.kind, e.kind)}) here, but no body is modelled "
                    f"within {tol_m:.0f} m{near}: its weight and housing are not in this analysis. Add it under "
                    "Bodies, or take the bodies from the assembly fit.")
        else:
            where = "The RPL" if e.source == SRC_RPL else "The burial plan"
            text = (f"{where} has a crossing here ('{e.label}'), but no seabed feature is entered within "
                    f"{tol_m:.0f} m{near}. If the crossed pipeline or cable stands above the seabed, enter it under "
                    "Additional features: gridded bathymetry often smooths a pipe away.")
        out.append(RangeFinding("event_check", INFO, e.lo, e.hi, text))
    return out


# ---------------------------------------------------------------------------
# Label layout (screen pixels)
# ---------------------------------------------------------------------------
@dataclass
class LabelSlot:
    tier: int = -1          # -1: no room (the tick still shows)
    left: float = 0.0
    right: float = 0.0


def layout_labels(items: Sequence[Tuple[float, float, int]], width: float, tiers: int,
                  gap: float = 6.0) -> List[LabelSlot]:
    """Place labels on ``tiers`` rows without overlap.

    ``items``: ``(x, label width, priority)`` in pixels. Lower priority goes
    first, then left to right. Each label tries, row by row, centred on its
    x, then starting at it, then ending at it (kept inside ``0 .. width``).
    """
    slots = [LabelSlot() for _ in items]
    rows: List[Tuple[List[float], List[float]]] = [([], []) for _ in range(max(int(tiers), 0))]
    order = sorted(range(len(items)), key=lambda k: (items[k][2], items[k][0]))
    for k in order:
        x, w, _p = items[k]
        if w <= 0 or not math.isfinite(x):
            continue
        candidates = [x - w / 2.0, x - 2.0, x + 2.0 - w]
        placed = False
        for tier, (lefts, rights) in enumerate(rows):
            for left in candidates:
                left = min(max(left, 0.0), max(width - w, 0.0))
                right = left + w
                j = bisect.bisect_left(lefts, left)
                if j > 0 and rights[j - 1] + gap > left:
                    continue
                if j < len(lefts) and lefts[j] < right + gap:
                    continue
                lefts.insert(j, left)
                rights.insert(j, right)
                slots[k] = LabelSlot(tier, left, right)
                placed = True
                break
            if placed:
                break
    return slots


def range_rows(spans: Sequence[Tuple[float, float]], rows: int) -> List[int]:
    """A row for each ``(lo, hi)`` range so that ranges on a row do not overlap (the last row takes the rest)."""
    ends = [-math.inf] * max(int(rows), 1)
    out = [0] * len(spans)
    for k in sorted(range(len(spans)), key=lambda k: spans[k][0]):
        lo, hi = spans[k]
        row = next((r for r, end in enumerate(ends) if lo >= end), len(ends) - 1)
        ends[row] = max(ends[row], hi)
        out[k] = row
    return out
