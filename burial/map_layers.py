# -*- coding: utf-8 -*-
"""Per-plan spatial layers (sections + events), symbology and project sync.

Geometry is sliced from the plan's RPL via ``RouteFrame`` (the
``assessment_output.py`` approach) and written to the plan's GeoPackage as
EPSG:4326 layers. Layers are managed exclusively by the tool: added
read-only, refreshed in place (never remove/re-add), styled with a
rule-based renderer — burial solid, skip dashed and insufficient-information
grey — directly on the source RPL geometry.

Also home to the layer-resolution helpers: registered ``bp_input`` rows are
re-resolved through ``workbench/project_layers.py`` normalised-path
comparison (never raw ``source()`` equality), and content fingerprints for
cache/staleness come from the normalised source + timestamp + feature count.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsMapLayer,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from ..processing.cable_lay_parsers import WKT_KEY, gpkg_layer_uri
from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench.project_layers import layer_name_from_source, normalised_path
from . import events as ev
from . import schema

BURIAL_GROUP = "Burial Planner"


# -- layer resolution --------------------------------------------------------


def resolve_input_layer(project: Optional[QgsProject], input_row: Dict
                        ) -> Optional[QgsMapLayer]:
    """Resolve a registered bp_input row to a live layer.

    Order: project layer id hint -> normalised-path source match across the
    project -> direct open from the stored source (layer not loaded). Returns
    None when nothing works; callers degrade to a per-rule warning.
    """
    project = project or QgsProject.instance()
    hint = input_row.get("layer_id_hint") or ""
    if hint:
        layer = project.mapLayer(hint)
        if layer is not None and layer.isValid():
            return layer
    source = input_row.get("layer_source") or ""
    if source:
        want = normalised_path(source.split("|")[0])
        for layer in project.mapLayers().values():
            got = normalised_path(str(layer.source()).split("|")[0])
            if got == want and str(layer.source()).split("|")[1:] == source.split("|")[1:]:
                if layer.isValid():
                    return layer
        # Not in the project: open directly from the provider source.
        layer = QgsVectorLayer(source, input_row.get("layer_name") or "bp_input", "ogr")
        if layer.isValid():
            return layer
        raster = QgsRasterLayer(source, input_row.get("layer_name") or "bp_input")
        if raster.isValid():
            return raster
    return None


def layer_fingerprint(layer: Optional[QgsMapLayer]) -> str:
    """Content fingerprint: normalised source + timestamp + feature count."""
    if layer is None:
        return ""
    source = normalised_path(str(layer.source()).split("|")[0])
    suffix = "|".join(str(layer.source()).split("|")[1:])
    stamp = ""
    try:
        ts = layer.dataProvider().dataTimestamp()
        if ts and ts.isValid():
            stamp = ts.toString("yyyy-MM-ddTHH:mm:ss")
    except Exception:
        stamp = ""
    if not stamp:
        try:
            path = str(layer.source()).split("|")[0]
            if os.path.exists(path):
                stamp = str(int(os.path.getmtime(path)))
        except Exception:
            stamp = ""
    count = ""
    if isinstance(layer, QgsVectorLayer):
        try:
            count = str(layer.featureCount())
        except Exception:
            count = ""
    return f"{source}|{suffix}|{stamp}|{count}"


def rpl_fingerprint(rpl_row: Optional[Dict], gpkg_path: str = "") -> str:
    """Stale-detection fingerprint for a Workbench RPL revision."""
    if not rpl_row:
        return ""
    return "|".join([
        str(rpl_row.get("rpl_id") or ""),
        str(rpl_row.get("modified_utc") or ""),
        str(rpl_row.get("lines_layer") or ""),
        normalised_path(gpkg_path) if gpkg_path else "",
    ])


# -- writing -----------------------------------------------------------------


def write_plan_layers(store, plan: Dict, sections: Sequence[Dict],
                      events: Sequence[Dict], route) -> Tuple[str, str]:
    """Write/overwrite the plan's sections + events layers; returns names."""
    method = plan.get("method") or ""
    base_args = (plan.get("name") or "plan", plan.get("rev_label") or "",
                 plan.get("plan_id") or "")
    sections_name = schema.sections_layer_name(*base_args)
    events_name = schema.events_layer_name(*base_args)

    section_rows: List[Dict] = []
    for section in sections:
        geom = route.extract_segment(float(section.get("start_kp") or 0.0),
                                     float(section.get("end_kp") or 0.0)) if route else None
        if geom is None or geom.isEmpty():
            continue
        section_rows.append({
            "section_id": section.get("section_id") or "",
            "plan_id": section.get("plan_id") or "",
            "kind": section.get("kind") or "",
            "start_kp": section.get("start_kp"),
            "end_kp": section.get("end_kp"),
            "length_km": section.get("length_km"),
            "state": section.get("state") or "",
            "conclusion": schema.CONCLUSION_LABELS.get(
                section.get("conclusion") or "", section.get("conclusion") or ""),
            "confidence": section.get("confidence") or "",
            "reasons": section.get("reason_json") or "",
            "notes": section.get("notes") or "",
            WKT_KEY: geom.asWkt(),
        })

    event_rows: List[Dict] = []
    for event in events:
        point = route.point_at_kp(float(event.get("kp") or 0.0), clamp=True) if route else None
        if point is None:
            continue
        event_rows.append({
            "event_id": event.get("event_id") or "",
            "plan_id": event.get("plan_id") or "",
            "seq": int(event.get("seq") or 0),
            "event_type": event.get("event_type") or "",
            "label": ev.event_label(event.get("event_type") or "", method),
            "kp": event.get("kp"),
            "lat": event.get("lat"),
            "lon": event.get("lon"),
            "depth_m": event.get("depth_m"),
            "source": event.get("source") or "",
            "status": event.get("status") or "",
            "locked": int(event.get("locked") or 0),
            "notes": event.get("notes") or "",
            WKT_KEY: f"POINT ({point.x()} {point.y()})",
        })

    store.write_spatial_layer(sections_name, schema.SECTIONS_LAYER_FIELDS,
                              WKB_LINESTRING, section_rows)
    store.write_spatial_layer(events_name, schema.EVENTS_LAYER_FIELDS,
                              WKB_POINT, event_rows)
    return sections_name, events_name


# -- styling -----------------------------------------------------------------

_SECTION_STYLES = {
    schema.SECTION_BURIAL: {"color": "#1b7f3b", "width": "1.8", "style": "solid",
                            "label": "Burial"},
    schema.SECTION_SKIP: {"color": "#d62728", "width": "1.4", "style": "dash",
                          "label": "Skip"},
    schema.SECTION_INSUFFICIENT: {"color": "#9e9e9e", "width": "1.4", "style": "dash",
                                  "label": "Insufficient Information"},
}


def apply_sections_style(layer) -> None:
    try:
        from qgis.core import QgsLineSymbol, QgsRuleBasedRenderer
    except ImportError:
        return
    if layer is None or not layer.isValid():
        return
    root = QgsRuleBasedRenderer.Rule(None)
    for kind, style in _SECTION_STYLES.items():
        symbol = QgsLineSymbol.createSimple({
            "color": style["color"], "width": style["width"],
            "line_style": style["style"],
        })
        child = QgsRuleBasedRenderer.Rule(symbol)
        child.setLabel(style["label"])
        child.setFilterExpression(f"\"kind\" = '{kind}'")
        root.appendChild(child)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def apply_events_style(layer) -> None:
    """Distinct start/end symbols; candidate hollow, confirmed filled,
    conflict red; labelled ``<label> KP xx.xxx``."""
    try:
        from qgis.core import (
            QgsMarkerSymbol,
            QgsPalLayerSettings,
            QgsRuleBasedRenderer,
            QgsTextFormat,
            QgsVectorLayerSimpleLabeling,
        )
    except ImportError:
        return
    if layer is None or not layer.isValid():
        return

    def marker(name: str, fill: str, outline: str) -> "QgsMarkerSymbol":
        return QgsMarkerSymbol.createSimple({
            "name": name, "color": fill, "outline_color": outline,
            "outline_width": "0.4", "size": "3.4",
        })

    root = QgsRuleBasedRenderer.Rule(None)
    for event_type, shape in ((schema.EVENT_BURIAL_START, "triangle"),
                              (schema.EVENT_BURIAL_END, "triangle")):
        type_rule = QgsRuleBasedRenderer.Rule(None)
        type_rule.setLabel("Start" if event_type == schema.EVENT_BURIAL_START else "End")
        type_rule.setFilterExpression(f"\"event_type\" = '{event_type}'")
        angle = "180" if event_type == schema.EVENT_BURIAL_START else "0"
        for status, fill, outline in (
                (schema.EVENT_STATUS_CANDIDATE, "255,255,255,0", "#1b7f3b"),
                (schema.EVENT_STATUS_CONFIRMED, "#1b7f3b", "#0e4d22"),
                (schema.EVENT_STATUS_CONFLICT, "#d62728", "#7a1416")):
            symbol = marker(shape, fill, outline)
            try:
                symbol.symbolLayer(0).setAngle(float(angle))
            except Exception:
                pass
            child = QgsRuleBasedRenderer.Rule(symbol)
            child.setLabel(status)
            child.setFilterExpression(f"\"status\" = '{status}'")
            type_rule.appendChild(child)
        root.appendChild(type_rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))

    try:
        settings = QgsPalLayerSettings()
        settings.fieldName = "\"label\" || ' KP ' || format_number(\"kp\", 3)"
        settings.isExpression = True
        text_format = QgsTextFormat()
        font = text_format.font()
        font.setPointSize(8)
        text_format.setFont(font)
        settings.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
    except Exception:
        pass
    layer.triggerRepaint()


# -- project sync ------------------------------------------------------------


def burial_group(project: Optional[QgsProject] = None, create: bool = True):
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(BURIAL_GROUP)
    if group is None and create:
        group = root.insertGroup(0, BURIAL_GROUP)
    return group


def find_layer(project: QgsProject, gpkg_path: str, layer_name: str
               ) -> Optional[QgsVectorLayer]:
    if not layer_name:
        return None
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsVectorLayer) \
                and layer_name_from_source(layer.source(), gpkg_path) == layer_name:
            return layer
    return None


def _ensure_layer(project: QgsProject, gpkg_path: str, layer_name: str,
                  style_fn) -> Optional[QgsVectorLayer]:
    existing = find_layer(project, gpkg_path, layer_name)
    if existing is not None and existing.isValid():
        existing.dataProvider().forceReload()
        existing.triggerRepaint()
        return existing
    layer = QgsVectorLayer(gpkg_layer_uri(gpkg_path, layer_name), layer_name, "ogr")
    if not layer.isValid():
        return None
    group = burial_group(project)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    try:
        layer.setReadOnly(True)  # users edit only through the tool
    except Exception:
        pass
    style_fn(layer)
    return layer


def ensure_plan_layers(project: Optional[QgsProject], gpkg_path: str, plan: Dict
                       ) -> Tuple[Optional[QgsVectorLayer], Optional[QgsVectorLayer]]:
    """Find-or-add the plan's sections + events layers (sections beneath)."""
    project = project or QgsProject.instance()
    base_args = (plan.get("name") or "plan", plan.get("rev_label") or "",
                 plan.get("plan_id") or "")
    sections = _ensure_layer(project, gpkg_path,
                             schema.sections_layer_name(*base_args),
                             apply_sections_style)
    events = _ensure_layer(project, gpkg_path,
                           schema.events_layer_name(*base_args),
                           apply_events_style)
    return sections, events


def remove_plan_layers(project: Optional[QgsProject], gpkg_path: str, plan: Dict) -> None:
    project = project or QgsProject.instance()
    base_args = (plan.get("name") or "plan", plan.get("rev_label") or "",
                 plan.get("plan_id") or "")
    for name in (schema.sections_layer_name(*base_args),
                 schema.events_layer_name(*base_args)):
        layer = find_layer(project, gpkg_path, name)
        if layer is not None:
            project.removeMapLayer(layer.id())
