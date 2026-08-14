# -*- coding: utf-8 -*-
"""Per-plan spatial layers (sections + events), symbology and project sync.

Geometry is sliced from the plan's RPL via ``RouteFrame`` (the
``assessment_output.py`` approach) and written to the plan's GeoPackage as
EPSG:4326 layers. Layers are managed exclusively by the tool: added
read-only, refreshed in place (never remove/re-add), styled with a
rule-based renderer — burial solid green, skip solid red and
insufficient-information dashed grey — directly on the source RPL geometry.

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
from . import tools as tools_mod

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


def depth_config_fingerprint(project: Optional[QgsProject], depth_config) -> str:
    """Combined content fingerprint of every configured bathymetry layer.

    One formula shared by threshold-rule cache keys and the persisted plan
    profile's currency check, so "the samples are current" and "the rule
    cache is current" can never disagree.
    """
    project = project or QgsProject.instance()
    return "|".join(
        layer_fingerprint(project.mapLayer(layer_id))
        for layer_id in depth_config.raster_layer_ids
    ) + "|" + "|".join(
        layer_fingerprint(project.mapLayer(entry.get("layer_id", "")))
        for entry in depth_config.contour_layers)


def min_raster_cell_size_m(project: Optional[QgsProject], depth_config
                           ) -> Optional[float]:
    """Smallest cell size (m) among the configured bathymetry rasters.

    Sampling finer than the raster cell re-reads the same cell — cost
    without content — so the profile step's Auto mode follows this.
    Geographic rasters are converted with a cos(latitude) approximation at
    the layer's extent centre (a sampling-step choice, not a measurement).
    Returns None when no usable raster is configured (e.g. contours only).
    """
    import math

    project = project or QgsProject.instance()
    best: Optional[float] = None
    for layer_id in getattr(depth_config, "raster_layer_ids", []) or []:
        layer = project.mapLayer(layer_id)
        if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
            continue
        try:
            upp_x = abs(float(layer.rasterUnitsPerPixelX()))
            upp_y = abs(float(layer.rasterUnitsPerPixelY()))
        except Exception:
            continue
        if upp_x <= 0 and upp_y <= 0:
            continue
        try:
            geographic = layer.crs().isGeographic()
        except Exception:
            geographic = False
        if geographic:
            try:
                lat = math.radians(layer.extent().center().y())
            except Exception:
                lat = 0.0
            candidates = [upp_x * 111320.0 * max(math.cos(lat), 0.087),
                          upp_y * 110540.0]
        else:
            candidates = [upp_x, upp_y]
        cell = min(c for c in candidates if c > 0)
        if best is None or cell < best:
            best = cell
    return best


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
                      events: Sequence[Dict], route,
                      hazards: Optional[Sequence[Dict]] = None,
                      risk_checks: Optional[Sequence[Dict]] = None,
                      tools: Optional[Sequence[Dict]] = None
                      ) -> Tuple[str, str]:
    """Write/overwrite the plan's sections + events (+ hazards) layers."""
    method = plan.get("method") or ""
    base_args = (plan.get("name") or "plan", plan.get("rev_label") or "",
                 plan.get("plan_id") or "")
    sections_name = schema.sections_layer_name(*base_args)
    events_name = schema.events_layer_name(*base_args)

    refs = schema.section_refs(sections,
                               int(plan.get("direction") or 1), method)
    section_rows: List[Dict] = []
    for section in sections:
        geom = route.extract_segment(float(section.get("start_kp") or 0.0),
                                     float(section.get("end_kp") or 0.0)) if route else None
        if geom is None or geom.isEmpty():
            continue
        section_rows.append({
            "section_ref": refs.get(str(section.get("section_id") or ""), ""),
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
            "tool": tools_mod.section_tool_display(section, plan, tools or []),
            "skip_handling": schema.SKIP_HANDLING_LABELS.get(
                section.get("skip_handling") or "", "")
            if section.get("kind") == schema.SECTION_SKIP else "",
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

    if hazards is not None:
        check_names = {str(c.get("check_id") or ""): (c.get("name") or "")
                       for c in (risk_checks or [])}
        hazard_rows: List[Dict] = []
        for hazard in hazards:
            lat, lon = hazard.get("lat"), hazard.get("lon")
            if (lat is None or lon is None) and route is not None:
                try:
                    start = float(hazard.get("kp") or 0.0)
                    end = float(hazard.get("end_kp") or start)
                except (TypeError, ValueError):
                    continue
                point = route.point_at_kp((start + end) / 2.0, clamp=True)
                if point is None:
                    continue
                lat, lon = point.y(), point.x()
            if lat is None or lon is None:
                continue
            hazard_rows.append({
                "hazard_id": hazard.get("hazard_id") or "",
                "plan_id": hazard.get("plan_id") or "",
                "label": hazard.get("label") or "",
                "check": check_names.get(str(hazard.get("check_id") or ""),
                                         "manual"),
                "kp": hazard.get("kp"),
                "end_kp": hazard.get("end_kp"),
                "offset_m": hazard.get("offset_m"),
                "crossing": int(hazard.get("crossing") or 0),
                "crossing_angle_deg": hazard.get("crossing_angle_deg"),
                "risk": hazard.get("risk") or "",
                "status": hazard.get("status") or "",
                "source": hazard.get("source") or "",
                "notes": hazard.get("notes") or "",
                WKT_KEY: f"POINT ({lon} {lat})",
            })
        store.write_spatial_layer(schema.hazards_layer_name(*base_args),
                                  schema.HAZARDS_LAYER_FIELDS,
                                  WKB_POINT, hazard_rows)
    return sections_name, events_name


# -- styling -----------------------------------------------------------------

_SECTION_STYLES = {
    schema.SECTION_BURIAL: {"color": "#1b7f3b", "width": "1.8", "style": "solid",
                            "label": "Burial"},
    schema.SECTION_SKIP: {"color": "#d62728", "width": "1.4", "style": "solid",
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


_RISK_COLORS = {
    schema.RISK_HIGH: "#d62728",
    schema.RISK_MEDIUM: "#ff8c00",
    schema.RISK_LOW: "#e0b000",
    schema.RISK_UNASSIGNED: "#909090",
}


def apply_hazards_style(layer) -> None:
    """Risk-coloured markers; crossings ring-outlined; labelled by name."""
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
    root = QgsRuleBasedRenderer.Rule(None)
    for level, color in _RISK_COLORS.items():
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle", "color": color,
            "outline_color": "#40282828", "outline_width": "0.3",
            "size": "3.0",
        })
        child = QgsRuleBasedRenderer.Rule(symbol)
        child.setLabel(schema.RISK_LABELS.get(level, level or "Unassigned"))
        child.setFilterExpression(f"\"risk\" = '{level}'")
        root.appendChild(child)
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
        layer.setLabelsEnabled(False)  # off by default; user can enable
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
                  style_fn, expected_fields=None) -> Optional[QgsVectorLayer]:
    existing = find_layer(project, gpkg_path, layer_name)
    if existing is not None and existing.isValid():
        # A loaded layer caches its field map. When the tool's layer schema
        # gains a column (e.g. section_ref, skip_handling) the cached map can
        # go stale after the table is rewritten, silently breaking the
        # rule-based renderer's field lookups ("kind" no longer resolves, so
        # e.g. skip lines stop drawing). Re-point the layer at its source to
        # rebuild the field map in place — layer id, tree position and
        # project references all survive.
        wanted = {name for name, _type in (expected_fields or [])}
        have = set(existing.fields().names())
        if wanted and not wanted.issubset(have):
            try:
                existing.setDataSource(gpkg_layer_uri(gpkg_path, layer_name),
                                       layer_name, "ogr")
                from qgis.core import QgsMessageLog

                from ..qgis_compat import MESSAGE_INFO
                QgsMessageLog.logMessage(
                    f"Rebuilt the stale field map of plan layer "
                    f"'{layer_name}' (schema gained columns).",
                    "Burial Planner", MESSAGE_INFO)
            except Exception:
                pass
        existing.dataProvider().reloadData()
        existing.updateExtents()
        # These are tool-owned, read-only presentation layers. Reapply their
        # style so fixes (notably removal of the old line offset) also reach
        # layers already saved in an open project.
        style_fn(existing)
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
                             apply_sections_style,
                             expected_fields=schema.SECTIONS_LAYER_FIELDS)
    events = _ensure_layer(project, gpkg_path,
                           schema.events_layer_name(*base_args),
                           apply_events_style,
                           expected_fields=schema.EVENTS_LAYER_FIELDS)
    _ensure_layer(project, gpkg_path,
                  schema.hazards_layer_name(*base_args),
                  apply_hazards_style,
                  expected_fields=schema.HAZARDS_LAYER_FIELDS)
    return sections, events


def remove_plan_layers(project: Optional[QgsProject], gpkg_path: str, plan: Dict) -> None:
    project = project or QgsProject.instance()
    base_args = (plan.get("name") or "plan", plan.get("rev_label") or "",
                 plan.get("plan_id") or "")
    for name in (schema.sections_layer_name(*base_args),
                 schema.events_layer_name(*base_args),
                 schema.hazards_layer_name(*base_args)):
        layer = find_layer(project, gpkg_path, name)
        if layer is not None:
            project.removeMapLayer(layer.id())


# -- project-open self-healing ----------------------------------------------


def _burial_layer_name(source: str) -> str:
    """The gpkg layer name when ``source`` looks like a burial plan layer."""
    for part in str(source or "").split("|")[1:]:
        key, sep, value = part.partition("=")
        if sep and key.lower() == "layername" and value.startswith("bp_") \
                and (value.endswith("_sections") or value.endswith("_events")
                     or value.endswith("_hazards")):
            return value
    return ""


def discover_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    """Find the project's burial-plans GeoPackage without creating one.

    Mirrors the Workbench recovery order: the saved project entry, the same
    basename beside a relocated project file, then the conventional default
    path. Returns None when no valid registry is found.
    """
    from .store import (
        BurialStore,
        default_project_gpkg_path,
        project_gpkg_path,
    )

    project = project or QgsProject.instance()

    def is_store(path: Optional[str]) -> bool:
        if not path:
            return False
        try:
            return BurialStore(path).exists()
        except Exception:
            return False

    saved = project_gpkg_path(project)
    if is_store(saved):
        return saved
    project_file = project.fileName() or ""
    folder = os.path.dirname(os.path.abspath(project_file)) if project_file else ""
    if saved and folder:
        relocated = os.path.join(folder, os.path.basename(saved))
        if is_store(relocated):
            return relocated
    fallback = default_project_gpkg_path(project)
    if is_store(fallback):
        return fallback
    return None


def restore_burial_layers(project: Optional[QgsProject] = None) -> int:
    """Repair broken burial plan layers after a project opens. Never raises.

    Runs on ``projectRead`` without requiring the dock: any ``bp_*``
    sections/events layer whose source no longer resolves (moved GeoPackage,
    stale relative path, ...) is pointed back at the project's discovered
    burial-plans GeoPackage and re-styled. Repair only — plan layers are
    added when a plan is opened in the dock, never here.
    Returns the number of layers repaired.
    """
    try:
        project = project or QgsProject.instance()
        broken = [layer for layer in project.mapLayers().values()
                  if isinstance(layer, QgsVectorLayer) and not layer.isValid()
                  and _burial_layer_name(layer.source())]
        if not broken:
            return 0
        gpkg_path = discover_gpkg_path(project)
        if not gpkg_path:
            return 0
        from ..workbench.project_layers import repair_layer
        from ..processing.cable_lay_parsers import open_gpkg_layer

        touched = 0
        for layer in broken:
            name = _burial_layer_name(layer.source())
            if open_gpkg_layer(gpkg_path, name) is None:
                continue
            if repair_layer(layer, gpkg_path, name):
                if name.endswith("_sections"):
                    style_fn = apply_sections_style
                elif name.endswith("_hazards"):
                    style_fn = apply_hazards_style
                else:
                    style_fn = apply_events_style
                style_fn(layer)
                try:
                    layer.setReadOnly(True)
                except Exception:
                    pass
                touched += 1
        return touched
    except Exception:
        return 0
