# -*- coding: utf-8 -*-
"""Risk Profile logic — risk evaluation, carry-over and summaries.

Pure python (no QGIS imports) so the risk rules are unit-testable under
plain Python; the QGIS feature scan lives in ``risk_scan.py``. Hazards are
plain dicts shaped like ``bp_hazard`` rows.

A check's criteria (``bp_risk_check.config_json``) assign risk two ways,
and the effective auto risk is the *most severe* of the two:

- proximity bands: ``band_high_m`` / ``band_medium_m`` / ``band_low_m`` —
  a feature whose nearest approach is within the band gets that level
  (0 disables a band; crossings are offset 0 and hit the tightest band);
- attribute rules: ordered ``attribute_rules`` over one feature attribute,
  each either an exact value match or a numeric range — first match wins.

``default_risk`` applies when a feature is inside the search corridor but
no band or attribute rule fires. No engineering values are shipped — every
band, rule and default is user-entered, with a source-reference field.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from . import schema


def check_config(check_row: Dict) -> Dict:
    try:
        config = json.loads(check_row.get("config_json") or "{}")
    except (ValueError, TypeError):
        config = {}
    return config if isinstance(config, dict) else {}


def risk_max(*levels: str) -> str:
    """The most severe of the given levels ('' = unassigned sorts lowest)."""
    best = schema.RISK_UNASSIGNED
    for level in levels:
        level = level or ""
        if schema.RISK_ORDER.get(level, 0) > schema.RISK_ORDER.get(best, 0):
            best = level
    return best


def proximity_risk(config: Dict, offset_m: Optional[float]) -> str:
    """Risk from the nearest-approach bands; '' when no band matches."""
    if offset_m is None:
        return schema.RISK_UNASSIGNED
    offset = max(0.0, float(offset_m))
    for key, level in (("band_high_m", schema.RISK_HIGH),
                       ("band_medium_m", schema.RISK_MEDIUM),
                       ("band_low_m", schema.RISK_LOW)):
        try:
            band = float(config.get(key) or 0.0)
        except (TypeError, ValueError):
            band = 0.0
        if band > 0 and offset <= band + 1e-9:
            return level
    return schema.RISK_UNASSIGNED


def _rule_matches(rule: Dict, value) -> bool:
    if "match" in rule:
        return str(value).strip().casefold() == \
            str(rule.get("match") or "").strip().casefold()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    minimum = rule.get("min")
    maximum = rule.get("max")
    if minimum is not None and number < float(minimum) - 1e-12:
        return False
    if maximum is not None and number > float(maximum) + 1e-12:
        return False
    return minimum is not None or maximum is not None


def attribute_risk(config: Dict, attributes: Dict) -> str:
    """Risk from the check's attribute rules; '' when none match."""
    attribute = (config.get("attribute") or "").strip()
    rules = config.get("attribute_rules") or []
    if not attribute or not rules:
        return schema.RISK_UNASSIGNED
    if attribute not in attributes:
        return schema.RISK_UNASSIGNED
    value = attributes.get(attribute)
    if value is None:
        return schema.RISK_UNASSIGNED
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _rule_matches(rule, value):
            level = rule.get("risk") or ""
            if level in schema.RISK_ORDER:
                return level
    return schema.RISK_UNASSIGNED


def evaluate_risk(config: Dict, offset_m: Optional[float],
                  attributes: Optional[Dict] = None) -> str:
    """Effective auto risk: max(proximity, attribute), else default_risk."""
    level = risk_max(proximity_risk(config, offset_m),
                     attribute_risk(config, attributes or {}))
    if level:
        return level
    default = config.get("default_risk") or ""
    return default if default in schema.RISK_ORDER else schema.RISK_UNASSIGNED


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
    """Rule dict -> the editor's text form."""
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


def sort_hazards(hazards: Sequence[Dict]) -> List[Dict]:
    def key(hazard: Dict) -> Tuple[float, int, str]:
        try:
            kp = float(hazard.get("kp") or 0.0)
        except (TypeError, ValueError):
            kp = 0.0
        severity = -schema.RISK_ORDER.get(hazard.get("risk") or "", 0)
        return (kp, severity, str(hazard.get("hazard_id") or ""))

    return sorted(hazards, key=key)


def hazard_spans(hazards: Sequence[Dict], point_halfwidth_km: float = 0.025
                 ) -> List[Tuple[float, float, str]]:
    """(start_km, end_km, risk) spans for the overview strip.

    Point hazards get a small ± halfwidth so they stay visible at route
    scale; range hazards use their true extent.
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
    # Severe spans last so they draw on top of milder overlapping ones.
    spans.sort(key=lambda s: schema.RISK_ORDER.get(s[2], 0))
    return spans


def summarise_hazards(hazards: Sequence[Dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {level: 0 for level in
                              [schema.RISK_UNASSIGNED] + schema.RISK_LEVELS}
    open_count = 0
    for hazard in hazards:
        level = hazard.get("risk") or ""
        counts[level if level in counts else schema.RISK_UNASSIGNED] += 1
        if (hazard.get("status") or schema.HAZARD_STATUS_OPEN) \
                == schema.HAZARD_STATUS_OPEN:
            open_count += 1
    counts["open"] = open_count
    counts["total"] = len(list(hazards))
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
