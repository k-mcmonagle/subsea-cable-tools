# -*- coding: utf-8 -*-
"""Persist assessment verdicts as a styled GeoPackage line layer + registry rows.

The geometry for each verdict range is sliced from the route with
``RouteFrame.extract_segment`` so the styled layer overlays the real route.
Styling is a rule-based renderer: one toggleable child rule per method, and a
colour per status (green allowed / amber risk / red excluded), with a small
per-method offset so overlapping methods stay readable.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..processing.cable_lay_parsers import WKT_KEY
from ..qgis_compat import WKB_LINESTRING
from . import schema
from .rules_engine import AssessmentResult

RANGE_LAYER_FIELDS = [
    ("assessment_id", "str"),
    ("method", "str"),
    ("start_kp", "float"),
    ("end_kp", "float"),
    ("status", "str"),
    ("risk_level", "int"),
    ("dominant_rule", "str"),
    ("fired_rules", "str"),
]

_STATUS_COLORS = {
    schema.STATUS_ALLOWED: "#2ca02c",
    schema.STATUS_RISK: "#ff8c00",
    schema.STATUS_EXCLUDED: "#d62728",
}


def write_assessment_ranges(store, assessment_row: Dict, result: AssessmentResult,
                            route, rule_names: Optional[Dict[str, str]] = None) -> str:
    """Write the verdict ranges to the gpkg + wb_assessment_range registry.

    Returns the spatial layer name. Also stamps the assessment row (status,
    run_utc, ranges_layer) and saves it.
    """
    rule_names = rule_names or {}
    assessment_id = assessment_row["assessment_id"]
    layer_name = schema.assessment_ranges_layer_name(
        f"{assessment_row.get('name') or assessment_id}_{assessment_id[:8]}"
    )

    spatial_rows: List[Dict] = []
    registry_rows: List[Dict] = []
    for method, verdicts in result.per_method.items():
        for verdict in verdicts:
            dominant = rule_names.get(verdict.dominant_rule_id or "", "")
            fired = ", ".join(rule_names.get(rid, rid) for rid in verdict.fired_rule_ids)
            import json as _json
            registry_rows.append({
                "range_id": schema.new_id(),
                "assessment_id": assessment_id,
                "method": method,
                "start_kp": verdict.start_km,
                "end_kp": verdict.end_km,
                "status": verdict.status,
                "risk_level": verdict.risk_level,
                "fired_rules_json": _json.dumps(verdict.fired_rule_ids),
                "dominant_rule_id": verdict.dominant_rule_id or "",
                "notes": "",
            })
            geom = route.extract_segment(verdict.start_km, verdict.end_km)
            if geom is None or geom.isEmpty():
                continue
            spatial_rows.append({
                "assessment_id": assessment_id,
                "method": method,
                "start_kp": verdict.start_km,
                "end_kp": verdict.end_km,
                "status": verdict.status,
                "risk_level": verdict.risk_level,
                "dominant_rule": dominant,
                "fired_rules": fired,
                WKT_KEY: geom.asWkt(),
            })

    store.save_assessment_ranges(assessment_id, registry_rows)
    if spatial_rows:
        store.write_spatial_layer(
            layer_name, RANGE_LAYER_FIELDS, WKB_LINESTRING, spatial_rows)

    assessment_row = dict(assessment_row)
    assessment_row["ranges_layer"] = layer_name
    assessment_row["status"] = "current"
    assessment_row["run_utc"] = schema.utc_now_iso()
    store.save_assessment(assessment_row)
    return layer_name


def apply_assessment_style(layer, methods: List[str]) -> None:
    """Apply a per-method / per-status rule-based renderer to a range layer."""
    try:
        from qgis.core import QgsRuleBasedRenderer, QgsLineSymbol
    except ImportError:
        return
    if layer is None or not layer.isValid():
        return

    root = QgsRuleBasedRenderer.Rule(None)
    for m_index, method in enumerate(methods):
        offset = (m_index - (len(methods) - 1) / 2.0) * 1.2
        method_rule = QgsRuleBasedRenderer.Rule(None)
        method_rule.setLabel(method)
        method_rule.setFilterExpression(f"\"method\" = '{method}'")
        for status, color in _STATUS_COLORS.items():
            symbol = QgsLineSymbol.createSimple({"color": color, "width": "1.4"})
            try:
                symbol.symbolLayer(0).setOffset(offset)
            except Exception:
                pass
            child = QgsRuleBasedRenderer.Rule(symbol)
            child.setLabel(status)
            child.setFilterExpression(f"\"status\" = '{status}'")
            method_rule.appendChild(child)
        root.appendChild(method_rule)

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()
