# -*- coding: utf-8 -*-
"""Disposable EPSG:4326 map layers for persisted Installation Paths."""

from __future__ import annotations

from typing import Dict, Optional

from qgis.core import QgsProject

from ..processing.cable_lay_parsers import WKT_KEY
from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from . import map_layers, path_data, schema


def _base_args(plan: Dict):
    return (plan.get("name") or "plan", plan.get("rev_label") or "",
            plan.get("plan_id") or "")


def apply_tool_path_style(layer) -> None:
    try:
        from qgis.core import QgsLineSymbol, QgsRuleBasedRenderer
    except ImportError:
        return
    if layer is None or not layer.isValid():
        return
    root = QgsRuleBasedRenderer.Rule(None)
    for status, color, style, label in (
            ("current", "#00a6d6", "solid", "Current tool path"),
            ("stale", "#8c8c8c", "dash", "Stale tool path")):
        symbol = QgsLineSymbol.createSimple({
            "color": color, "width": "1.15", "line_style": style})
        rule = QgsRuleBasedRenderer.Rule(symbol)
        rule.setLabel(label)
        rule.setFilterExpression(f'"status" = \'{status}\'')
        root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def apply_barge_track_style(layer) -> None:
    try:
        from qgis.core import QgsLineSymbol, QgsRuleBasedRenderer
    except ImportError:
        return
    if layer is None or not layer.isValid():
        return
    root = QgsRuleBasedRenderer.Rule(None)
    for status, color, style, label in (
            ("current", "#7a3db8", "dash", "Current barge track"),
            ("stale", "#a28eb5", "dot", "Stale barge track")):
        symbol = QgsLineSymbol.createSimple({
            "color": color, "width": "0.95", "line_style": style})
        rule = QgsRuleBasedRenderer.Rule(symbol)
        rule.setLabel(label)
        rule.setFilterExpression(f'"status" = \'{status}\'')
        root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def apply_path_issues_style(layer) -> None:
    try:
        from qgis.core import QgsMarkerSymbol, QgsRuleBasedRenderer
    except ImportError:
        return
    if layer is None or not layer.isValid():
        return
    root = QgsRuleBasedRenderer.Rule(None)
    for status, color, size, label in (
            ("ok", "#35a853", "2.2", "Resolved course change"),
            ("review", "#f28e2b", "3.2", "Review course change"),
            ("error", "#d62728", "3.5", "Path error")):
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle", "color": color,
            "outline_color": "#ffffff", "outline_width": "0.25",
            "size": size})
        rule = QgsRuleBasedRenderer.Rule(symbol)
        rule.setLabel(label)
        rule.setFilterExpression(f'"status" = \'{status}\'')
        root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    try:
        layer.setLabelsEnabled(False)
    except Exception:
        pass
    layer.triggerRepaint()


def write_path_layers(store, plan: Dict, result: Optional[Dict],
                      state: Optional[Dict[str, str]] = None) -> None:
    """Rebuild all three map layers from the authoritative registry row."""
    result = result or {}
    state = state or {"tool": "missing", "barge": "missing"}
    args = _base_args(plan)
    path_id = result.get("path_id") or ""
    plan_id = plan.get("plan_id") or ""
    summary = path_data.parse_json_field(result, "summary_json", {})
    config = path_data.parse_json_field(result, "config_json", {})

    tool_rows = []
    if result.get("tool_path_wkt"):
        tool_rows.append({
            "path_id": path_id, "plan_id": plan_id,
            "mode": config.get("mode") or summary.get("mode") or "",
            "tool": summary.get("tool") or "",
            "radius_m": summary.get("radius_m"),
            "length_m": summary.get("length_m"),
            "max_offset_m": summary.get("max_offset_m"),
            "rms_offset_m": summary.get("rms_offset_m"),
            "status": state.get("tool") or "stale",
            "generated_utc": result.get("generated_utc") or "",
            WKT_KEY: result.get("tool_path_wkt"),
        })
    store.write_spatial_layer(schema.tool_path_layer_name(*args),
                              schema.TOOL_PATH_LAYER_FIELDS,
                              WKB_LINESTRING, tool_rows)

    barge_rows = []
    if result.get("barge_track_wkt"):
        barge_rows.append({
            "path_id": path_id, "plan_id": plan_id,
            "layback": summary.get("layback_name") or "",
            "length_m": summary.get("barge_length_m"),
            "min_radius_m": summary.get("barge_min_radius_m"),
            "status": state.get("barge") or "stale",
            "generated_utc": result.get("generated_utc") or "",
            WKT_KEY: result.get("barge_track_wkt"),
        })
    store.write_spatial_layer(schema.barge_track_layer_name(*args),
                              schema.BARGE_TRACK_LAYER_FIELDS,
                              WKB_LINESTRING, barge_rows)

    issue_rows = []
    diagnostics = path_data.parse_json_field(result, "diagnostics_json", [])
    for item in diagnostics if isinstance(diagnostics, list) else []:
        try:
            lon, lat = float(item.get("lon")), float(item.get("lat"))
        except (TypeError, ValueError):
            continue
        issue_rows.append({
            "path_id": path_id, "plan_id": plan_id,
            "control_no": item.get("control_no"), "kp": item.get("kp"),
            "turn_deg": item.get("turn_deg"), "side": item.get("side") or "",
            "solution": item.get("solution") or "",
            "radius_m": item.get("radius_m"),
            "depth_m": item.get("depth_m"),
            "miss_m": item.get("miss_m"),
            "max_offset_m": item.get("max_offset_m"),
            "depth_diff_m": item.get("depth_diff_m"),
            "status": item.get("status") or "ok",
            "message": item.get("message") or "",
            WKT_KEY: f"POINT ({lon} {lat})",
        })
    store.write_spatial_layer(schema.path_issues_layer_name(*args),
                              schema.PATH_ISSUES_LAYER_FIELDS,
                              WKB_POINT, issue_rows)


def ensure_path_layers(project: Optional[QgsProject], gpkg_path: str,
                       plan: Dict, reload: bool = True):
    project = project or QgsProject.instance()
    args = _base_args(plan)
    tool = map_layers._ensure_layer(
        project, gpkg_path, schema.tool_path_layer_name(*args),
        apply_tool_path_style, schema.TOOL_PATH_LAYER_FIELDS, reload=reload,
        plan=plan)
    barge = map_layers._ensure_layer(
        project, gpkg_path, schema.barge_track_layer_name(*args),
        apply_barge_track_style, schema.BARGE_TRACK_LAYER_FIELDS,
        reload=reload, plan=plan)
    issues = map_layers._ensure_layer(
        project, gpkg_path, schema.path_issues_layer_name(*args),
        apply_path_issues_style, schema.PATH_ISSUES_LAYER_FIELDS,
        reload=reload, plan=plan)
    return tool, barge, issues


def set_path_visibility(project: Optional[QgsProject], gpkg_path: str,
                        plan: Dict, part: str, visible: bool) -> None:
    project = project or QgsProject.instance()
    names = {
        "tool": schema.tool_path_layer_name,
        "barge": schema.barge_track_layer_name,
        "issues": schema.path_issues_layer_name,
    }
    name_fn = names.get(part)
    if name_fn is None:
        return
    layer = map_layers.find_layer(project, gpkg_path,
                                  name_fn(*_base_args(plan)))
    if layer is None:
        return
    node = project.layerTreeRoot().findLayer(layer.id())
    if node is not None:
        node.setItemVisibilityChecked(bool(visible))

