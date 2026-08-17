# -*- coding: utf-8 -*-
"""Assembly dataclasses, catenary JSON round-trip, event classification, and
extract-from-RPL.

An assembly is the ordered physical build of a cable (or a rope/rigging
string): sections with a length and unit properties, and bodies (joints,
repeaters, branching units — or shackles and links for rigging) at points in
the cable domain.

Catenary interchange uses the V2 dialog's JSON entry format
(``{"type": "segment"|"body", "name", "length_m", "q_water_npm", ...}``)
plus the V3 hydro keys (``diameter_m``, ``cd_normal``, ``cd_tangential``) so
an assembly can be pasted straight into either calculator.

Pure Python — no QGIS imports — so it runs headless.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import schema

KIND_SECTION = "section"
KIND_BODY = "body"

ASSEMBLY_KIND_CABLE = "cable"
ASSEMBLY_KIND_RIGGING = "rigging"


@dataclass
class AssemblyItem:
    kind: str  # section | body
    name: str = ""
    length_m: float = 0.0
    q_water_npm: Optional[float] = None
    q_air_npm: Optional[float] = None
    point_load_kN: Optional[float] = None
    friction_mu: Optional[float] = None
    bending_stiffness_kNm2: Optional[float] = None
    min_bend_radius_m: Optional[float] = None
    diameter_m: Optional[float] = None
    cd_normal: Optional[float] = None
    cd_tangential: Optional[float] = None
    cable_type: str = ""
    cable_code: str = ""
    fiber_pair: str = ""
    color_hex: str = ""
    remarks: str = ""
    item_id: str = field(default_factory=schema.new_id)

    @property
    def is_section(self) -> bool:
        return self.kind == KIND_SECTION


@dataclass
class Assembly:
    name: str = ""
    kind: str = ASSEMBLY_KIND_CABLE  # cable | rigging
    description: str = ""
    source: str = "manual"
    source_ref: str = ""
    items: List[AssemblyItem] = field(default_factory=list)
    assembly_id: str = field(default_factory=schema.new_id)

    def total_length_m(self) -> float:
        return sum(i.length_m or 0.0 for i in self.items if i.is_section)

    def cable_dist_starts_m(self) -> List[float]:
        """Cumulative cable distance (m) at the start of each item."""
        starts: List[float] = []
        cursor = 0.0
        for item in self.items:
            starts.append(cursor)
            if item.is_section:
                cursor += item.length_m or 0.0
        return starts

    def bodies_with_positions_m(self) -> List[Tuple[AssemblyItem, float]]:
        starts = self.cable_dist_starts_m()
        return [(item, starts[i]) for i, item in enumerate(self.items) if not item.is_section]


# ---------------------------------------------------------------------------
# Store row round-trip (wb_assembly / wb_assembly_item)
# ---------------------------------------------------------------------------
_ITEM_FLOAT_FIELDS = (
    "length_m", "q_water_npm", "q_air_npm", "point_load_kN", "friction_mu",
    "bending_stiffness_kNm2", "min_bend_radius_m", "diameter_m", "cd_normal",
    "cd_tangential",
)
_ITEM_STR_FIELDS = ("name", "cable_type", "cable_code", "fiber_pair", "color_hex", "remarks")


def assembly_to_rows(assembly: Assembly) -> Tuple[Dict, List[Dict]]:
    header = {
        "assembly_id": assembly.assembly_id,
        "name": assembly.name,
        "kind": assembly.kind,
        "description": assembly.description,
        "source": assembly.source,
        "source_ref": assembly.source_ref,
        "total_cable_len_m": assembly.total_length_m(),
    }
    starts = assembly.cable_dist_starts_m()
    items = []
    for i, item in enumerate(assembly.items):
        row = {"item_id": item.item_id, "kind": item.kind, "seq": i,
               "cable_dist_start_m": starts[i]}
        for name in _ITEM_STR_FIELDS:
            row[name] = getattr(item, name)
        for name in _ITEM_FLOAT_FIELDS:
            row[name] = getattr(item, name)
        items.append(row)
    return header, items


def assembly_from_rows(header: Dict, item_rows: Sequence[Dict]) -> Assembly:
    items = []
    for row in sorted(item_rows, key=lambda r: int(r.get("seq") or 0)):
        kwargs = {"kind": row.get("kind") or KIND_SECTION}
        if row.get("item_id"):
            kwargs["item_id"] = row["item_id"]
        for name in _ITEM_STR_FIELDS:
            kwargs[name] = row.get(name) or ""
        for name in _ITEM_FLOAT_FIELDS:
            value = row.get(name)
            kwargs[name] = float(value) if value is not None else None
        if kwargs.get("length_m") is None:
            kwargs["length_m"] = 0.0
        items.append(AssemblyItem(**kwargs))
    return Assembly(
        name=header.get("name") or "",
        kind=header.get("kind") or ASSEMBLY_KIND_CABLE,
        description=header.get("description") or "",
        source=header.get("source") or "manual",
        source_ref=header.get("source_ref") or "",
        items=items,
        assembly_id=header.get("assembly_id") or schema.new_id(),
    )


# ---------------------------------------------------------------------------
# Catenary (V2/V3) JSON round-trip
# ---------------------------------------------------------------------------
def to_catenary_json(assembly: Assembly) -> str:
    """Serialise to the V2 dialog's assembly JSON (with V3 hydro keys)."""
    data = []
    for item in assembly.items:
        if item.is_section:
            entry = {
                "type": "segment",
                "name": item.name or "Cable",
                "length_m": item.length_m or 0.0,
                "q_water_npm": item.q_water_npm if item.q_water_npm is not None else 0.0,
                "q_air_npm": item.q_air_npm if item.q_air_npm is not None else 0.0,
            }
            for key, value in (
                ("friction_mu", item.friction_mu),
                ("bending_stiffness_kNm2", item.bending_stiffness_kNm2),
                ("min_bend_radius_m", item.min_bend_radius_m),
                ("diameter_m", item.diameter_m),
                ("cd_normal", item.cd_normal),
                ("cd_tangential", item.cd_tangential),
            ):
                if value is not None:
                    entry[key] = value
        else:
            entry = {
                "type": "body",
                "name": item.name or "Body",
                "point_load_kN": item.point_load_kN if item.point_load_kN is not None else 0.0,
            }
        if item.color_hex:
            entry["color"] = item.color_hex
        data.append(entry)
    return json.dumps(data, indent=2)


def from_catenary_json(raw: str, name: str = "Imported assembly") -> Assembly:
    """Parse V2 `assembly_table_json` / V3 assembly JSON into an Assembly."""
    data = json.loads(raw or "[]")
    if isinstance(data, dict):
        data = data.get("assembly", [])
    if not isinstance(data, list):
        raise ValueError("Catenary assembly JSON must be a list of entries")

    def _opt_float(entry: Dict, *keys: str) -> Optional[float]:
        for key in keys:
            if key in entry and entry.get(key) is not None:
                try:
                    return float(entry[key])
                except (TypeError, ValueError):
                    return None
        return None

    items: List[AssemblyItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        kind_raw = str(entry.get("type", entry.get("kind", "segment"))).strip().lower()
        is_body = kind_raw.startswith("body")
        if is_body:
            items.append(AssemblyItem(
                kind=KIND_BODY,
                name=str(entry.get("name", "Body")),
                length_m=0.0,
                point_load_kN=_opt_float(entry, "point_load_kN", "load_kN") or 0.0,
                color_hex=str(entry.get("color", entry.get("color_hex", "")) or ""),
            ))
        else:
            items.append(AssemblyItem(
                kind=KIND_SECTION,
                name=str(entry.get("name", "Cable")),
                length_m=_opt_float(entry, "length_m", "length") or 0.0,
                q_water_npm=_opt_float(entry, "q_water_npm", "q_water", "weight_water_npm"),
                q_air_npm=_opt_float(entry, "q_air_npm", "q_air", "weight_air_npm"),
                friction_mu=_opt_float(entry, "friction_mu", "mu"),
                bending_stiffness_kNm2=_opt_float(
                    entry, "bending_stiffness_kNm2", "bending_stiffness_knm2", "EI_kNm2", "ei"),
                min_bend_radius_m=_opt_float(
                    entry, "min_bend_radius_m", "mbr_m", "MBR_m", "minimum_bend_radius_m"),
                diameter_m=_opt_float(entry, "diameter_m", "diameter"),
                cd_normal=_opt_float(entry, "cd_normal", "Cd_normal"),
                cd_tangential=_opt_float(entry, "cd_tangential", "Cd_tangential"),
                color_hex=str(entry.get("color", entry.get("color_hex", "")) or ""),
            ))
    return Assembly(name=name, source="catenary_json", items=items)


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------
@dataclass
class EventClassification:
    """What an RPL point's event says the point is.

    Every point has a place on the map; the two natures record what else it
    is, and they can BOTH be true ("JT-3 / AC12" — a joint that currently
    coincides with an alter-course position): if the assembly changes, the
    joint moves with it while the geographic reference stays put.
    """

    category: str            # body | geographic | both
    body_type: str = ""      # assembly subtype (joint | repeater | bu | ...)
    geo_type: str = ""       # geographic subtype (crossing | boundary | ...)
    is_assembly: bool = False
    is_geographic: bool = False
    matched_pattern: str = ""
    matched: bool = False    # False => defaulted (never silently a body)

    @property
    def label(self) -> str:
        if self.is_assembly and self.is_geographic:
            return "Assembly + geographic"
        if self.is_assembly:
            return "Assembly"
        return "Geographic"

    @property
    def subtype(self) -> str:
        parts = [p for p in (self.body_type, self.geo_type) if p]
        return " + ".join(parts)


def _rule_nature(rule: Dict) -> str:
    category = str(rule.get("category") or "").strip().lower()
    if category == schema.CATEGORY_BODY:
        return schema.CATEGORY_BODY
    # legacy "installation" rows (and anything unknown) read as geographic
    return schema.CATEGORY_GEOGRAPHIC


class EventClassifier:
    """Classifies RPL point Event text using ordered regex rules.

    Rules are ``{"pattern", "category", "body_type", "priority"}`` dicts
    (wb_event_rule rows). ALL rules are evaluated: a body match makes the
    point an assembly component, a geographic match makes it a geographic
    reference, and one event text can be both — each nature's subtype comes
    from its own best-priority match. Unmatched or blank events default to
    ``geographic`` with ``matched=False`` so callers can flag them — an
    unmatched event must never silently become an assembly body.
    """

    def __init__(self, rules: Sequence[Dict]):
        compiled = []
        for rule in sorted(rules, key=lambda r: int(r.get("priority") or 0)):
            pattern = rule.get("pattern") or ""
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            compiled.append((rx, rule))
        self._rules = compiled

    @classmethod
    def with_defaults(cls) -> "EventClassifier":
        rows = [
            {"pattern": p, "category": c, "body_type": b, "priority": pr}
            for p, c, b, pr in schema.DEFAULT_EVENT_RULES
        ]
        return cls(rows)

    def classify(self, event_text: Optional[str]) -> EventClassification:
        text = (event_text or "").strip()
        body_rule = None
        geo_rule = None
        if text:
            for rx, rule in self._rules:
                if not rx.search(text):
                    continue
                if _rule_nature(rule) == schema.CATEGORY_BODY:
                    if body_rule is None:
                        body_rule = rule
                elif geo_rule is None:
                    geo_rule = rule
                if body_rule is not None and geo_rule is not None:
                    break
        if body_rule is None and geo_rule is None:
            return EventClassification(
                category=schema.CATEGORY_GEOGRAPHIC, is_geographic=True,
                matched=False)
        if body_rule is not None and geo_rule is not None:
            category = schema.CATEGORY_BOTH
        elif body_rule is not None:
            category = schema.CATEGORY_BODY
        else:
            category = schema.CATEGORY_GEOGRAPHIC
        return EventClassification(
            category=category,
            body_type=(body_rule or {}).get("body_type") or "",
            geo_type=(geo_rule or {}).get("body_type") or "",
            is_assembly=body_rule is not None,
            is_geographic=geo_rule is not None,
            matched_pattern=((body_rule or geo_rule) or {}).get("pattern") or "",
            matched=True,
        )


# ---------------------------------------------------------------------------
# Extract-from-RPL
# ---------------------------------------------------------------------------
GROUP_BY_CABLE_TYPE = "cable_type"      # new section on cable type/code/fibre change or body
GROUP_BETWEEN_BODIES = "between_bodies"  # one section between consecutive bodies


def classify_events(model, classifier: EventClassifier) -> List[Dict]:
    """One review entry per non-blank point event (for the review dialog).

    Entries: {seq, pos_no, event, category, body_type, matched}. The dialog
    lets the user override ``category`` before building the assembly.
    """
    review: List[Dict] = []
    for point in model.points:
        if (point.event or "").strip():
            cls = classifier.classify(point.event)
            review.append({
                "seq": point.seq,
                "pos_no": point.pos_no,
                "event": point.event,
                "category": cls.category,
                "body_type": cls.body_type,
                "geo_type": cls.geo_type,
                "matched": cls.matched,
            })
    return review


def build_assembly_from_rpl(
    model,
    classifications: Dict[int, str],
    name: str = "",
    grouping: str = GROUP_BY_CABLE_TYPE,
) -> Assembly:
    """Build an Assembly from an RPL model using per-event classifications.

    ``classifications`` maps point ``seq`` -> category; only points whose
    category is ``body`` (or ``both`` — an assembly component that currently
    coincides with a geographic reference) become body items, and every body
    splits the run of segments into sections. Section grouping:

    - GROUP_BY_CABLE_TYPE: consecutive segments sharing
      (CableType, CableCode, FiberPair) merge into one section (a change of
      type also starts a new section);
    - GROUP_BETWEEN_BODIES: all segments between two consecutive bodies merge
      into one section, named after the dominant cable type in the run.

    Section lengths are the summed cable distance (route + slack).
    """
    items: List[AssemblyItem] = []

    def section_key(seg) -> Tuple[str, str, str]:
        return (
            str(seg.attrs.get("CableType") or ""),
            str(seg.attrs.get("CableCode") or ""),
            str(seg.attrs.get("FiberPair") or ""),
        )

    open_section: Optional[AssemblyItem] = None
    open_key: Optional[Tuple[str, str, str]] = None
    # per open section under between-bodies grouping: cable length per type key
    run_lengths: Dict[Tuple[str, str, str], float] = {}

    def finish_run_section():
        """Name a between-bodies section after its dominant cable type."""
        nonlocal run_lengths
        if open_section is not None and run_lengths:
            dominant = max(run_lengths.items(), key=lambda kv: kv[1])[0]
            open_section.cable_type, open_section.cable_code, open_section.fiber_pair = dominant
            label = " / ".join(v for v in dominant if v)
            if label:
                open_section.name = label
        run_lengths = {}

    for i, point in enumerate(model.points):
        category = classifications.get(point.seq)
        is_body = category in (schema.CATEGORY_BODY, schema.CATEGORY_BOTH)
        if is_body and (point.event or "").strip():
            finish_run_section()
            open_section = None
            open_key = None
            items.append(AssemblyItem(
                kind=KIND_BODY,
                name=point.event.strip(),
                length_m=0.0,
            ))

        if i < len(model.segments):
            seg = model.segments[i]
            key = section_key(seg)
            cable_m = (seg.cable_dist_km or seg.dist_km or 0.0) * 1000.0
            same_section = open_section is not None and (
                grouping == GROUP_BETWEEN_BODIES or key == open_key
            )
            if same_section:
                open_section.length_m += cable_m
            else:
                finish_run_section()
                open_section = AssemblyItem(
                    kind=KIND_SECTION,
                    name=" / ".join(v for v in key if v) or f"Section {len(items) + 1}",
                    length_m=cable_m,
                    cable_type=key[0],
                    cable_code=key[1],
                    fiber_pair=key[2],
                )
                open_key = key
                items.append(open_section)
            run_lengths[key] = run_lengths.get(key, 0.0) + cable_m

    finish_run_section()
    return Assembly(name=name or "Extracted assembly", source="rpl_extract", items=items)


def extract_from_rpl(model, classifier: EventClassifier, name: str = "",
                     grouping: str = GROUP_BY_CABLE_TYPE) -> Tuple[Assembly, List[Dict]]:
    """Classify events with ``classifier`` and build the assembly in one go.

    Convenience wrapper around :func:`classify_events` +
    :func:`build_assembly_from_rpl`; the interactive dialog calls the two
    halves separately so the user can override classifications.
    """
    review = classify_events(model, classifier)
    classifications = {entry["seq"]: entry["category"] for entry in review}
    assembly = build_assembly_from_rpl(model, classifications, name=name, grouping=grouping)
    return assembly, review
