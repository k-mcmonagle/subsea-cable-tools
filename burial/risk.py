# -*- coding: utf-8 -*-
"""Risk Profile logic — risk evaluation, carry-over and summaries.

Pure python (no QGIS imports) so the risk rules are unit-testable under
plain Python; the QGIS feature scan lives in ``risk_scan.py``. Hazards are
plain dicts shaped like ``bp_hazard`` rows.

A check's criteria (``bp_risk_check.config_json``) assign risk two ways,
and the effective auto risk is the *most severe* of the two:

- route contact and proximity bands: ``on_route_risk`` applies to an
  intersection. Independently, ``band_<level>_m`` assigns
  a level to features within that distance (0 disables a band). The
  highest scoring match on the plan's scale wins;
- attribute rules: ordered ``attribute_rules`` over one feature attribute,
  each an exact value match, a numeric range (with explicit inclusive /
  exclusive bounds) or a QGIS expression over the whole feature — first
  match wins. The condition vocabulary lives in ``attribute_rules.py``;
  expression rules are evaluated by the QGIS-side scan and passed in as
  ``expression_hits``.

``default_risk`` applies when a feature is inside the search corridor but
no band or attribute rule fires. No engineering values are shipped — every
band, rule and default is user-entered, with a source-reference field.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from . import attribute_rules, schema
from .risk_scale import DEFAULT_SCALE


def check_config(check_row: Dict) -> Dict:
    try:
        config = json.loads(check_row.get("config_json") or "{}")
    except (ValueError, TypeError):
        config = {}
    return config if isinstance(config, dict) else {}


def risk_max(*levels: str, scale=DEFAULT_SCALE) -> str:
    """The most severe of the given levels ('' = unassigned sorts lowest)."""
    best = schema.RISK_UNASSIGNED
    for level in levels:
        level = level or ""
        if scale.rank(level) > scale.rank(best):
            best = level
    return best


def proximity_risk(config: Dict, offset_m: Optional[float], scale=DEFAULT_SCALE) -> str:
    """Risk from the nearest-approach bands; '' when no band matches.

    Offsets are signed (+ starboard / − port); bands apply to the magnitude.
    """
    if offset_m is None:
        return schema.RISK_UNASSIGNED
    offset = abs(float(offset_m))
    for level in reversed(scale.levels):
        try:
            band = float(config.get(f"band_{level}_m") or 0.0)
        except (TypeError, ValueError):
            band = 0.0
        if band > 0 and offset <= band + 1e-9:
            return level
    return schema.RISK_UNASSIGNED


def _rule_matches(rule: Dict, value) -> bool:
    """Value / range rule test (kept for callers of the old name)."""
    return attribute_rules.rule_matches(rule, value)


def attribute_risk(config: Dict, attributes: Dict,
                   expression_hits: Optional[Sequence[bool]] = None,
                   scale=DEFAULT_SCALE) -> str:
    """Risk from the check's attribute rules; '' when none match.

    Value and range rules read ``config['attribute']`` from ``attributes``
    (a missing or null value never fires). Expression rules use
    ``expression_hits`` — one pre-evaluated bool per expression rule, in
    rule order (see ``attribute_rules.expression_texts``) — so they work
    even with no attribute chosen. First match wins, in rule order.
    """
    attribute = (config.get("attribute") or "").strip()
    rules = [rule for rule in (config.get("attribute_rules") or [])
             if isinstance(rule, dict)]
    if not rules:
        return schema.RISK_UNASSIGNED
    has_value = bool(attribute) and attribute in attributes \
        and not attribute_rules.is_null(attributes.get(attribute))
    value = attributes.get(attribute) if has_value else None
    expression_index = 0
    for rule in rules:
        if attribute_rules.rule_kind(rule) == attribute_rules.KIND_EXPRESSION:
            hit = None
            if expression_hits is not None \
                    and expression_index < len(expression_hits):
                hit = expression_hits[expression_index]
            expression_index += 1
            fired = attribute_rules.rule_matches(rule, None, hit)
        else:
            fired = has_value and attribute_rules.rule_matches(rule, value)
        if fired:
            level = rule.get("risk") or ""
            if level in scale.labels:
                return level
    return schema.RISK_UNASSIGNED


def evaluate_risk(config: Dict, offset_m: Optional[float],
                  attributes: Optional[Dict] = None,
                  expression_hits: Optional[Sequence[bool]] = None,
                  *, intersects=False, scale=DEFAULT_SCALE) -> str:
    """Highest scoring spatial or attribute match, otherwise the check default."""
    level = risk_max(config.get("on_route_risk") if intersects else "",
                     proximity_risk(config, offset_m, scale),
                     attribute_risk(config, attributes or {},
                                    expression_hits, scale), scale=scale)
    if level:
        return level
    default = config.get("default_risk") or ""
    return default if default in scale.labels else schema.RISK_UNASSIGNED


def used_levels(checks, hazards):
    """References that must stay resolvable if a level is renamed or removed."""
    used = set()
    for hazard in hazards:
        used.update((hazard.get("risk"), hazard.get("auto_risk")))
    for check in checks:
        config = check_config(check)
        used.update((config.get("on_route_risk"), config.get("default_risk")))
        used.update(rule.get("risk") for rule in (config.get("attribute_rules") or []) if isinstance(rule, dict))
        used.update(key[5:-2] for key, value in config.items()
                    if key.startswith("band_") and key.endswith("_m") and value)
    return used - {None, ""}


def measured_offset(hazard):
    """Offset in metres for a spatial feature. KP-only records have no measurement."""
    ref = str(hazard.get("feature_ref") or "")
    if hazard.get("source") == schema.HAZARD_SOURCE_MANUAL or ref.startswith(("row:", "ac#")):
        return None
    return hazard.get("offset_m")


def interaction(hazard):
    if hazard.get("source") == schema.HAZARD_SOURCE_MANUAL:
        return "Manual"
    ref = str(hazard.get("feature_ref") or "")
    if ref.startswith("row:"):
        return "KP table"
    if ref.startswith("ac#"):
        return "Route turn"
    if int(hazard.get("crossing") or 0):
        if hazard.get("end_kp") is not None and float(hazard["end_kp"]) != float(hazard.get("kp") or 0):
            return "Overlap"
        return "Crossing" if hazard.get("crossing_angle_deg") is not None else "Touches route"
    offset = measured_offset(hazard)
    if offset is None:
        return ""
    return "On route" if float(offset) == 0 else "Nearby"


# ---------------------------------------------------------------------------
# Attribute-rule text form ("ROCK", "1.5-3", "-3", "2-") used by the editor
# ---------------------------------------------------------------------------


def parse_attribute_rule(text: str, risk: str) -> Optional[Dict]:
    """One editor row -> rule dict; None for a blank row.

    ``a-b`` parses as a numeric range, with either side blank for open
    (``2-`` = at least 2, ``-3`` = up to 3). Anything that does not parse
    as a range — including a lone number — is an exact value match.
    Ranges with negative bounds are not supported (use an open side).
    """
    text = (text or "").strip()
    if not text or risk not in schema.RISK_ORDER or not risk:
        return None
    if "-" in text:
        low_text, _sep, high_text = text.partition("-")
        try:
            low = float(low_text) if low_text.strip() else None
            high = float(high_text) if high_text.strip() else None
        except ValueError:
            low = high = None
        if low is not None or high is not None:
            rule: Dict = {"risk": risk}
            if low is not None:
                rule["min"] = low
            if high is not None:
                rule["max"] = high
            return rule
    return {"match": text, "risk": risk}


def format_attribute_rule(rule: Dict) -> str:
    """Rule dict -> the legacy ``a-b`` text form (inclusive bounds).

    Kept for older callers; the editor now uses a structured table (see
    ``attribute_rules.describe_rule`` for a readable summary).
    """
    if "expression" in rule:
        return str(rule.get("expression") or "")
    if "match" in rule:
        return str(rule.get("match") or "")
    low = rule.get("min")
    high = rule.get("max")
    low_text = f"{float(low):g}" if low is not None else ""
    high_text = f"{float(high):g}" if high is not None else ""
    return f"{low_text}-{high_text}"


# ---------------------------------------------------------------------------
# Register maintenance
# ---------------------------------------------------------------------------


def carry_over_hazards(new_hazards: Sequence[Dict],
                       previous: Sequence[Dict]) -> List[Dict]:
    """Re-scan results with the user's review carried over.

    Matched by (check_id, feature_ref): status, notes and a user-set risk
    survive a re-scan (the hazard keeps its id so the change log diffs
    cleanly); the auto risk is always refreshed from the new scan.
    """
    prev_by_key = {}
    for hazard in previous:
        key = (str(hazard.get("check_id") or ""),
               str(hazard.get("feature_ref") or ""))
        if key[1]:
            prev_by_key[key] = hazard
    merged: List[Dict] = []
    for hazard in new_hazards:
        row = dict(hazard)
        key = (str(row.get("check_id") or ""),
               str(row.get("feature_ref") or ""))
        prev = prev_by_key.get(key)
        if prev is not None:
            row["hazard_id"] = prev.get("hazard_id") or row.get("hazard_id")
            row["status"] = prev.get("status") or row.get("status")
            if prev.get("notes"):
                row["notes"] = prev.get("notes")
            if (prev.get("risk_source") or "") == schema.RISK_SOURCE_USER:
                row["risk"] = prev.get("risk") or ""
                row["risk_source"] = schema.RISK_SOURCE_USER
        merged.append(row)
    return merged


def sort_hazards(hazards: Sequence[Dict], scale=DEFAULT_SCALE) -> List[Dict]:
    def key(hazard: Dict) -> Tuple[float, int, str]:
        try:
            kp = float(hazard.get("kp") or 0.0)
        except (TypeError, ValueError):
            kp = 0.0
        severity = -scale.rank(hazard.get("risk") or "")
        return (kp, severity, str(hazard.get("hazard_id") or ""))

    return sorted(hazards, key=key)


def merge_risk_spans(spans: Sequence[Tuple[float, float, str]], scale=DEFAULT_SCALE
                     ) -> List[Tuple[float, float, str]]:
    """Merge overlapping/adjacent spans of the same risk level.

    Dense feature fields (thousands of boulder picks) otherwise produce one
    span per hazard, which makes strip painting and per-check fire bars
    scale with the register instead of with the route.
    """
    by_level: Dict[str, List[Tuple[float, float]]] = {}
    for start, end, level in spans:
        by_level.setdefault(level or "", []).append((start, end))
    merged: List[Tuple[float, float, str]] = []
    for level, ranges in by_level.items():
        ranges.sort()
        current_start, current_end = ranges[0]
        for start, end in ranges[1:]:
            if start <= current_end + 1e-9:
                current_end = max(current_end, end)
            else:
                merged.append((current_start, current_end, level))
                current_start, current_end = start, end
        merged.append((current_start, current_end, level))
    # Severe spans last so they draw on top of milder overlapping ones.
    merged.sort(key=lambda s: (scale.rank(s[2]), s[0]))
    return merged


def hazard_spans(hazards: Sequence[Dict], point_halfwidth_km: float = 0.025, scale=DEFAULT_SCALE
                 ) -> List[Tuple[float, float, str]]:
    """(start_km, end_km, risk) spans for the overview strip.

    Point hazards get a small ± halfwidth so they stay visible at route
    scale; range hazards use their true extent. Same-level overlapping
    spans are merged so the count stays bounded by route coverage.
    """
    spans: List[Tuple[float, float, str]] = []
    for hazard in hazards:
        try:
            start = float(hazard.get("kp") or 0.0)
            end_value = hazard.get("end_kp")
            end = float(end_value) if end_value is not None else start
        except (TypeError, ValueError):
            continue
        lo, hi = min(start, end), max(start, end)
        if hi - lo < point_halfwidth_km:
            centre = (lo + hi) / 2.0
            lo = centre - point_halfwidth_km
            hi = centre + point_halfwidth_km
        spans.append((lo, hi, hazard.get("risk") or ""))
    return merge_risk_spans(spans, scale)


def summarise_hazards(hazards: Sequence[Dict], scale=DEFAULT_SCALE) -> Dict[str, int]:
    counts: Dict[str, int] = dict.fromkeys(scale.labels, 0)
    open_count = 0
    for hazard in hazards:
        level = hazard.get("risk") or ""
        counts[level if level in counts else schema.RISK_UNASSIGNED] += 1
        if (hazard.get("status") or schema.HAZARD_STATUS_OPEN) \
                == schema.HAZARD_STATUS_OPEN:
            open_count += 1
    counts["open"] = open_count
    counts["total"] = len(hazards)
    return counts


def new_hazard_row(plan_id: str, check_id: str, feature_ref: str,
                   label: str, kp: float, end_kp: Optional[float],
                   offset_m: float, crossing: bool,
                   crossing_angle_deg: Optional[float],
                   lat: Optional[float], lon: Optional[float],
                   auto_risk: str, attributes: Optional[Dict] = None,
                   source: str = schema.HAZARD_SOURCE_CHECK,
                   notes: str = "") -> Dict:
    return {
        "hazard_id": schema.new_id(),
        "plan_id": plan_id,
        "check_id": check_id,
        "feature_ref": feature_ref,
        "label": label or "",
        "kp": float(kp),
        "end_kp": float(end_kp) if end_kp is not None else float(kp),
        "offset_m": float(offset_m or 0.0),
        "crossing": 1 if crossing else 0,
        "crossing_angle_deg": (float(crossing_angle_deg)
                               if crossing_angle_deg is not None else None),
        "lat": lat,
        "lon": lon,
        "risk": auto_risk or "",
        "auto_risk": auto_risk or "",
        "risk_source": schema.RISK_SOURCE_AUTO,
        "status": schema.HAZARD_STATUS_OPEN,
        "attributes_json": json.dumps(attributes or {}, default=str),
        "source": source,
        "notes": notes or "",
    }
